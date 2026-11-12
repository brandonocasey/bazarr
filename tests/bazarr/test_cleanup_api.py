import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from collections import deque
from threading import Event, Thread, current_thread

import pytest

from app.database import (database, select, delete, update, TableMovies, TableMoviesSubtitles, TableHistoryMovie,
                          TableSubtitleCleanupOperations, TableShows, TableEpisodes, TableEpisodesSubtitles)
from app.jobs_queue import jobs_queue
from subtitles.cleanup import service, store
from tests.bazarr import test_cleanup_media as media_fixtures

media = media_fixtures.media


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_signal_subtitle(path, activity):
    def stamp(index):
        seconds, milliseconds = divmod(index * 10, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f'{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}'

    cues = []
    start = None
    for index, active in enumerate([*activity, False]):
        if active and start is None:
            start = index
        elif not active and start is not None:
            cues.append(f'{len(cues) + 1}\n{stamp(start)} --> {stamp(index)}\nCaption\n')
            start = None
    path.write_text('\n'.join(cues))


@pytest.fixture
def authenticated_client(api_client, api_key):
    return api_client, {'X-API-KEY': api_key}


@pytest.fixture
def library(authenticated_client, tmp_path, monkeypatch, media):
    from app.config import settings
    from app.get_args import args

    import webrtcvad

    video = tmp_path / 'Cleanup verification.mkv'
    aligned = tmp_path / 'Cleanup verification.en.srt'
    lower = tmp_path / 'Cleanup verification.en.hi.srt'
    shutil.copyfile(media[0], video)
    pcm = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-copyts', '-i', str(video), '-map', '0:a:0',
                          '-af', 'aresample=16000:async=1:first_pts=0', '-ac', '1', '-ar', '16000',
                          '-t', '4', '-f', 's16le', 'pipe:1'], check=True, capture_output=True).stdout
    detector = webrtcvad.Vad(2)
    activity = [detector.is_speech(pcm[index:index + 320].ljust(320, b'\0'), 16000)
                for index in range(0, len(pcm), 320)]
    assert any(activity) and not all(activity)
    _write_signal_subtitle(aligned, activity)
    _write_signal_subtitle(lower, [not active for active in activity])
    config = tmp_path / 'app-state'
    config.mkdir()
    monkeypatch.setattr(args, 'config_dir', str(config))
    for key, value in [('use_embedded_subs', True), ('ignore_pgs_subs', False),
                       ('ignore_vobsub_subs', False), ('subfolder', 'current')]:
        monkeypatch.setattr(settings.general, key, value)
    media_id = 170001
    database.add(TableMovies(radarrId=media_id, title='Cleanup verification', path=str(video),
                             tmdbId=str(media_id), file_size=video.stat().st_size, movie_file_id=media_id))
    database.commit()
    database.add_all([
        TableMoviesSubtitles(radarrId=media_id, path=str(aligned), language='en', forced=False, hi=False),
        TableMoviesSubtitles(radarrId=media_id, path=str(lower), language='en', forced=False, hi=True),
        TableHistoryMovie(radarrId=media_id, action=1, description='Recorded download match', score=90,
                          score_out_of=180, video_path=str(video), subtitles_path=str(aligned)),
        TableHistoryMovie(radarrId=media_id, action=4, description='Synthetic upload maximum', score=180,
                          score_out_of=180, video_path=str(video), subtitles_path=str(lower)),
    ])
    database.commit()
    client, headers = authenticated_client
    yield client, headers, media_id, video, aligned, lower
    operation_ids = database.execute(select(TableSubtitleCleanupOperations.id)
                                     .where(TableSubtitleCleanupOperations.media_id == media_id)).scalars().all()
    jobs_queue.jobs_pending_queue = type(jobs_queue.jobs_pending_queue)(
        job for job in jobs_queue.jobs_pending_queue if job.kwargs.get('operation_id') not in operation_ids)
    database.execute(delete(TableSubtitleCleanupOperations).where(TableSubtitleCleanupOperations.media_id == media_id))
    database.execute(delete(TableHistoryMovie).where(TableHistoryMovie.radarrId == media_id))
    database.execute(delete(TableMoviesSubtitles).where(TableMoviesSubtitles.radarrId == media_id))
    database.execute(delete(TableMovies).where(TableMovies.radarrId == media_id))
    database.commit()


def _run(operation):
    job = next(job for job in jobs_queue.jobs_pending_queue
               if job.kwargs.get('operation_id') == operation['id'])
    assert jobs_queue._run_job(job_instance=job)
    return store.get_operation(operation['id'])


def _preview(library):
    client, headers, media_id, *_ = library
    response = client.post('/api/subtitles/cleanup/preview', headers=headers,
                           json={'media_type': 'movie', 'media_id': media_id})
    assert response.status_code == 202, response.json
    operation = _run(response.json)
    assert operation['status'] == 'ready', operation['error']
    return operation


def _apply(library, operation):
    client, headers, *_ = library
    keepers = {group['key']: group['recommended_keeper'] for group in operation['groups']}
    assert all(keepers.values())
    response = client.post(f'/api/subtitles/cleanup/{operation["id"]}/apply', headers=headers,
                           json={'preview_token': operation['preview_token'], 'keepers': keepers})
    assert response.status_code == 202, response.json
    return _run(response.json)


def test_completed_journal_recovery_invalidates_unchanged_file_selection(library):
    from utilities.media_write_lock import media_write_lock, media_write_state

    _, _, _, video, aligned, lower = library
    completed = _apply(library, _preview(library))
    completed['status'] = 'failed'
    store.update_operation(completed)
    before = _sha(video)
    selection = media_write_state(str(video))
    with media_write_lock(str(video)) as recovered:
        assert recovered != selection
        assert store.get_operation(completed['id'])['status'] == 'complete'
    assert _sha(video) == before


def test_rolled_back_journal_allows_fresh_extraction_retries(library, monkeypatch):
    from app.jobs_queue import JobCanceled
    from subtitles import embedded
    from subtitles.cleanup import transactions
    from subtitles.cleanup.models import PreparedFile
    from tests.bazarr.test_embedded_cleanup_selection import ProbeReached

    _, _, media_id, video, aligned, lower = library
    operation = _preview(library)
    before = {path: _sha(path) for path in (video, aligned, lower)}

    def cancel():
        raise JobCanceled('canceled before replacement')

    directory = Path(operation['backup_path'])
    with pytest.raises(JobCanceled):
        transactions.commit_changes(directory, [PreparedFile(str(lower), None)],
                                     [transactions.fingerprint(str(lower))], cancel)
    assert transactions.read_journal(directory)['phase'] == 'rolled_back'
    operation['status'] = 'failed'
    store.update_operation(operation)
    track = TableMoviesSubtitles(radarrId=media_id, embedded_track_id=2, language='en', forced=False, hi=False)
    database.add(track)
    database.commit()
    subtitle_id = track.id

    def probe(path):
        assert path == str(video)
        raise ProbeReached('fresh extraction reached media probing')

    monkeypatch.setattr(embedded, 'FFprobeVideoContainer', probe)
    for _ in range(2):
        with pytest.raises(ProbeReached):
            embedded.extract_embedded_subtitle(subtitle_id, 'movie', job_id=999999)
    assert {path: _sha(path) for path in (video, aligned, lower)} == before


def test_preview_auth_validation_and_indexed_identity(library):
    client, headers, media_id, *_ = library
    assert client.post('/api/subtitles/cleanup/preview', json={}).status_code == 401
    for payload in ({}, {'media_type': 'file', 'media_id': media_id},
                    {'media_type': 'movie', 'media_id': True},
                    {'media_type': 'movie', 'media_id': media_id, 'audio_stream_index': -1}):
        assert client.post('/api/subtitles/cleanup/preview', headers=headers, json=payload).status_code == 400
    assert client.post('/api/subtitles/cleanup/preview', headers=headers,
                       json={'media_type': 'movie', 'media_id': 99999999}).status_code == 404


def test_real_api_cleanup_and_restore_preserve_sources_and_inventory(library):
    client, headers, _, video, aligned, lower = library
    before = {path: _sha(path) for path in (video, aligned, lower)}
    operation = _preview(library)
    tracks = operation['analysis']['tracks']
    assert any(track['source'] == 'embedded' for track in tracks)
    english = [group for group in operation['groups'] if group['language'] == 'eng' and not group['forced']]
    assert len(english) == 1
    assert set(english[0]['track_keys']) == {track['key'] for track in tracks if track['language'] == 'eng'}
    assert next(track['download_score'] for track in tracks if track['path'] == str(aligned)) == 50
    assert next(track['download_score'] for track in tracks if track['path'] == str(lower)) is None
    assert operation['backup_path']
    completed = _apply(library, operation)
    assert completed['status'] == 'complete', completed['error']
    assert not list((video.parent / 'app-state' / 'cache' / 'subtitle-cleanup').rglob('prepared-*'))
    assert _sha(aligned) == before[aligned]
    assert not lower.exists()
    assert _sha(video) != before[video]
    response = client.post(f'/api/subtitles/cleanup/{operation["id"]}/restore', headers=headers,
                           json={'preview_token': operation['preview_token']})
    assert response.status_code == 202
    restored = _run(response.json)
    assert restored['status'] == 'restored', restored['error']
    assert {path: _sha(path) for path in before} == before
    assert set(restored['result']['retained_track_keys']) == {track['key'] for track in tracks}
    assert database.execute(select(TableMoviesSubtitles).where(TableMoviesSubtitles.path == str(lower))).first()


def test_wrong_token_and_lower_scoring_keeper_never_queue_mutation(library):
    client, headers, *_ = library
    operation = _preview(library)
    payload = {'preview_token': 'not-the-confirmed-preview',
               'keepers': {group['key']: group['recommended_keeper'] for group in operation['groups']}}
    url = f'/api/subtitles/cleanup/{operation["id"]}/apply'
    assert client.post(url, headers=headers, json=payload).status_code == 409
    payload['preview_token'] = operation['preview_token']
    lower = next(track for track in operation['analysis']['tracks'] if track['score'] == -1)
    group = next(group for group in operation['groups'] if lower['key'] in group['track_keys'])
    payload['keepers'][group['key']] = lower['key']
    assert client.post(url, headers=headers, json=payload).status_code == 400
    assert store.get_operation(operation['id'])['status'] == 'ready'


@pytest.mark.parametrize('change', ['contents', 'new-file'])
def test_stale_file_or_new_candidate_prevents_all_mutations(library, change):
    _, _, _, video, aligned, lower = library
    operation = _preview(library)
    video_hash = _sha(video)
    if change == 'contents':
        lower.write_text('A subtitle changed after preview')
    else:
        (video.parent / 'Cleanup verification.fr.srt').write_text(aligned.read_text())
    edited_hash = _sha(lower)
    failed = _apply(library, operation)
    assert failed['status'] == 'failed'
    assert 'changed' in failed['error']
    assert _sha(video) == video_hash
    assert _sha(lower) == edited_hash


def test_restore_refuses_later_download_without_changing_cleaned_video(library):
    client, headers, _, video, _, lower = library
    operation = _preview(library)
    completed = _apply(library, operation)
    assert completed['status'] == 'complete'
    video_hash = _sha(video)
    lower.write_text('Later download must survive restoration')
    response = client.post(f'/api/subtitles/cleanup/{operation["id"]}/restore', headers=headers,
                           json={'preview_token': operation['preview_token']})
    refused = _run(response.json)
    assert refused['status'] == 'complete'
    assert 'later changes' in refused['error']
    assert _sha(video) == video_hash
    assert lower.read_text() == 'Later download must survive restoration'


def test_removed_pending_job_does_not_leave_operation_polling_forever(library):
    client, headers, media_id, *_ = library
    response = client.post('/api/subtitles/cleanup/preview', headers=headers,
                           json={'media_type': 'movie', 'media_id': media_id})
    operation = response.json
    assert jobs_queue.remove_job_from_pending_queue(operation['job_id'])
    response = client.get(f'/api/subtitles/cleanup/{operation["id"]}', headers=headers)
    assert response.json['status'] == 'failed'
    assert 'canceled or interrupted' in response.json['error']


def test_queued_preview_cannot_change_its_locked_media_identity(library):
    client, headers, media_id, video, aligned, lower = library
    response = client.post('/api/subtitles/cleanup/preview', headers=headers,
                           json={'media_type': 'movie', 'media_id': media_id})
    assert response.status_code == 202
    before = {path: _sha(path) for path in (video, aligned, lower)}
    renamed = video.with_name('Renamed verification.mkv')
    shutil.copyfile(video, renamed)
    database.execute(update(TableMovies).where(TableMovies.radarrId == media_id).values(path=str(renamed)))
    for source in (aligned, lower):
        target = source.with_name(source.name.replace('Cleanup verification', 'Renamed verification'))
        shutil.copyfile(source, target)
        database.execute(update(TableMoviesSubtitles).where(TableMoviesSubtitles.path == str(source))
                         .values(path=str(target)))
        before[target] = _sha(target)
    before[renamed] = _sha(renamed)
    database.commit()
    failed = _run(response.json)
    assert failed['status'] == 'failed'
    assert 'changed before analysis' in failed['error']
    recovery_key = database.scalar(select(TableSubtitleCleanupOperations.video_path)
                                   .where(TableSubtitleCleanupOperations.id == failed['id']))
    assert failed['video_path'] == str(video)
    assert recovery_key == os.path.normcase(str(video))
    assert failed['analysis'] is None and not Path(failed['backup_path']).exists()
    assert {path: _sha(path) for path in before} == before


@pytest.mark.parametrize('transition', ['apply', 'restore'])
def test_polling_during_job_handoff_cannot_cancel_confirmed_work(library, monkeypatch, transition):
    client, headers, *_ = library
    operation = _preview(library)
    if transition == 'restore':
        operation = _apply(library, operation)
        assert operation['status'] == 'complete'
    feed = jobs_queue.feed_jobs_pending_queue
    observed = []

    def poll_before_enqueuing(*args, **kwargs):
        response = client.get(f'/api/subtitles/cleanup/{operation["id"]}', headers=headers)
        observed.append(response.json['status'])
        return feed(*args, **kwargs)

    monkeypatch.setattr(jobs_queue, 'feed_jobs_pending_queue', poll_before_enqueuing)
    if transition == 'apply':
        completed = _apply(library, operation)
    else:
        response = client.post(f'/api/subtitles/cleanup/{operation["id"]}/restore', headers=headers,
                               json={'preview_token': operation['preview_token']})
        completed = _run(response.json)
    assert observed == ['applying' if transition == 'apply' else 'restoring']
    assert completed['status'] == ('complete' if transition == 'apply' else 'restored'), completed['error']


def test_immediate_retry_of_refused_restore_cannot_strand_operation(library, monkeypatch):
    client, headers, _, _, _, lower = library
    operation = _apply(library, _preview(library))
    lower.write_text('Later subtitles must survive')
    update = store.update_operation
    retries = []
    url = f'/api/subtitles/cleanup/{operation["id"]}/restore'
    body = {'preview_token': operation['preview_token']}

    def retry_before_worker_exits(current, *args, **kwargs):
        result = update(current, *args, **kwargs)
        if current['status'] == 'complete' and current['error'] and not retries:
            retries.append(None)
            retries[0] = client.post(url, headers=headers, json=body).status_code
        return result

    monkeypatch.setattr(store, 'update_operation', retry_before_worker_exits)
    refused = _run(client.post(url, headers=headers, json=body).json)
    assert retries == [409]
    assert refused['status'] == 'complete'
    assert client.get(f'/api/subtitles/cleanup/{operation["id"]}', headers=headers).json['status'] == 'complete'
    again = client.post(url, headers=headers, json=body)
    assert again.status_code == 202
    assert _run(again.json)['status'] == 'complete'
    assert lower.read_text() == 'Later subtitles must survive'


def test_failed_queue_publication_cannot_run_an_abandoned_restore(library, monkeypatch):
    client, headers, _, video, _, lower = library
    operation = _apply(library, _preview(library))
    cleaned = _sha(video)
    feed = jobs_queue.feed_jobs_pending_queue

    def fail_after_enqueuing(*args, **kwargs):
        feed(*args, **kwargs)
        raise OSError('Queue event publication failed')

    monkeypatch.setattr(jobs_queue, 'feed_jobs_pending_queue', fail_after_enqueuing)
    url = f'/api/subtitles/cleanup/{operation["id"]}/restore'
    body = {'preview_token': operation['preview_token']}
    response = client.post(url, headers=headers, json=body)
    assert response.status_code == 409
    assert store.get_operation(operation['id'])['status'] == 'complete'
    drained = _run(operation)
    assert drained['status'] == 'complete'
    assert _sha(video) == cleaned and not lower.exists()
    monkeypatch.setattr(jobs_queue, 'feed_jobs_pending_queue', feed)
    assert _run(client.post(url, headers=headers, json=body).json)['status'] == 'restored'


@pytest.mark.parametrize('action', ['scheduler', 'move_top', 'move_bottom'])
def test_queue_transfer_is_invisible_to_cleanup_polling(library, monkeypatch, action):
    client, headers, *_ = library
    operation = _preview(library)
    keepers = {group['key']: group['recommended_keeper'] for group in operation['groups']}
    response = client.post(f'/api/subtitles/cleanup/{operation["id"]}/apply', headers=headers,
                           json={'preview_token': operation['preview_token'], 'keepers': keepers})
    assert response.status_code == 202
    removed, resume, listing, done = Event(), Event(), Event(), Event()
    reserved, observed, errors = [], [], []
    pending = jobs_queue.jobs_pending_queue
    list_jobs = jobs_queue.list_jobs_from_queue

    class PausedTransfer(deque):
        def remove(self, job):
            super().remove(job)
            removed.set()
            assert resume.wait(5)

    def reserve():
        try:
            if action == 'scheduler':
                reserved.append(jobs_queue._reserve_next_runnable_job())
            else:
                with client.application.test_client() as other:
                    reserved.append(other.post('/api/system/jobs', headers=headers,
                                               data={'id': response.json['job_id'], 'action': action}).status_code)
        except Exception as error:
            errors.append(error)

    def read():
        try:
            observed.append(service.get_operation(operation['id'])['status'])
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    def signal_listing(*args, **kwargs):
        listing.set()
        return list_jobs(*args, **kwargs)

    monkeypatch.setattr(jobs_queue, 'jobs_pending_queue', PausedTransfer(pending))
    monkeypatch.setattr(jobs_queue, 'list_jobs_from_queue', signal_listing)
    scheduler, poll = Thread(target=reserve), Thread(target=read)
    scheduler.start()
    try:
        assert removed.wait(5)
        poll.start()
        assert listing.wait(5)
        read_during_transfer = done.wait(0.2)
    finally:
        resume.set()
        scheduler.join(5)
        if poll.ident is not None:
            poll.join(5)
    assert not errors
    assert not scheduler.is_alive() and not poll.is_alive()
    assert reserved and reserved[0] is not None
    if action == 'scheduler':
        jobs_queue._run_job(job_instance=reserved[0], reserved=True)
    else:
        assert reserved == [204]
        _run(store.get_operation(operation['id']))
    assert not read_during_transfer
    assert observed == ['applying']
    assert store.get_operation(operation['id'])['status'] == 'complete'


def test_force_start_never_disappears_from_cleanup_job_inventory(library, monkeypatch):
    client, headers, *_ = library
    operation = _preview(library)
    keepers = {group['key']: group['recommended_keeper'] for group in operation['groups']}
    response = client.post(f'/api/subtitles/cleanup/{operation["id"]}/apply', headers=headers,
                           json={'preview_token': operation['preview_token'], 'keepers': keepers})
    assert response.status_code == 202
    released, resume = Event(), Event()
    real_lock = jobs_queue._queue_lock
    statuses, errors = [], []

    class PauseAfterUnlock:
        paused = False

        def __enter__(self):
            real_lock.acquire()
            return self

        def __exit__(self, *exception):
            real_lock.release()
            if current_thread() is starter and not self.paused:
                self.paused = True
                released.set()
                assert resume.wait(5)

    def force_start():
        try:
            with client.application.test_client() as other:
                statuses.append(other.post('/api/system/jobs', headers=headers,
                                           data={'id': response.json['job_id'], 'action': 'force_start'}).status_code)
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(jobs_queue, '_queue_lock', PauseAfterUnlock())
    starter = Thread(target=force_start)
    starter.start()
    try:
        assert released.wait(5)
        observed = client.get(f'/api/subtitles/cleanup/{operation["id"]}', headers=headers).json['status']
    finally:
        resume.set()
        starter.join(10)
    assert not starter.is_alive() and not errors
    assert statuses == [204]
    assert observed == 'applying'
    assert store.get_operation(operation['id'])['status'] == 'complete'


@pytest.mark.parametrize('replaced_directory', [False, True])
def test_restart_removes_only_owned_prepared_outputs_after_process_death(library, replaced_directory):
    from tests.bazarr.test_cleanup_transactions import _CHILD_BOOTSTRAP

    client, headers, _, video, aligned, lower = library
    operation = _preview(library)
    before = {path: _sha(path) for path in (video, aligned, lower)}
    keepers = {group['key']: group['recommended_keeper'] for group in operation['groups']}
    response = client.post(f'/api/subtitles/cleanup/{operation["id"]}/apply', headers=headers,
                           json={'preview_token': operation['preview_token'], 'keepers': keepers})
    assert response.status_code == 202
    config = video.parent / 'app-state'
    script = _CHILD_BOOTSTRAP + '''
from app.get_args import args
from subtitles.cleanup import media,service
args.config_dir=sys.argv[2]
prepare=media.prepare_outputs
def crash_after_prepare(*args,**kwargs):
    prepare(*args,**kwargs);os._exit(25)
media.prepare_outputs=crash_after_prepare
service.run_apply(sys.argv[1])
'''
    child = subprocess.run([sys.executable, '-c', script, operation['id'], str(config)],
                           timeout=60, capture_output=True, text=True)
    assert child.returncode == 25, child.stderr
    interrupted = store.get_operation(operation['id'])
    workspace = Path(interrupted['staging_workspace']['path'])
    assert workspace.is_dir() and list(workspace.rglob('prepared-*'))
    if replaced_directory:
        workspace.rename(workspace.with_name('captured-output-moved'))
        workspace.mkdir()
        (workspace / 'later.txt').write_text('Later data must survive')
    decoy = workspace.parent / 'apply-unrelated'
    decoy.mkdir()
    (decoy / 'keep.txt').write_text('Unrelated output')
    assert jobs_queue.remove_job_from_pending_queue(response.json['job_id'])
    service.recover_interrupted_operations()
    assert store.get_operation(operation['id'])['status'] == 'failed'
    if replaced_directory:
        assert (workspace / 'later.txt').read_text() == 'Later data must survive'
        assert 'identity changed' in store.get_operation(operation['id'])['error']
    else:
        assert not workspace.exists()
    assert (decoy / 'keep.txt').read_text() == 'Unrelated output'
    assert {path: _sha(path) for path in before} == before


def test_unindexed_bitmap_ties_unscored_and_forced_languages_need_exact_choices(library, media):
    client, headers, _, video, aligned, _ = library
    variants = {
        'en.copy.srt': aligned.read_bytes(),
        'en.broken.srt': b'Undecodable captions remain visible for a manual choice',
        'fr.forced.srt': aligned.read_bytes(),
        'hi.srt': aligned.read_bytes(),
        'en.sup': media[3].read_bytes(),
    }
    for suffix, contents in variants.items():
        video.with_suffix('.' + suffix).write_bytes(contents)
    operation = _preview(library)
    tracks = operation['analysis']['tracks']
    english = next(group for group in operation['groups'] if group['key'] == 'eng:0')
    assert english['requires_selection'] and english['recommended_keeper'] is None
    bitmap = next(track for track in tracks if track['path'].endswith('.sup'))
    broken = next(track for track in tracks if track['path'].endswith('.broken.srt'))
    hindi = next(track for track in tracks if track['path'].endswith('.hi.srt') and track['language'] == 'hin')
    assert bitmap['source'] == 'external' and bitmap['score'] is not None
    assert broken['score'] is None and broken['error']
    assert not hindi['hearing_impaired']
    assert any(group['key'] == 'fra:1' for group in operation['groups'])
    url = f'/api/subtitles/cleanup/{operation["id"]}/apply'
    assert client.post(url, headers=headers, json={'preview_token': operation['preview_token'],
                                                  'keepers': {}}).status_code == 400
    keepers = {group['key']: group['recommended_keeper'] for group in operation['groups']}
    keepers['eng:0'] = next(track['key'] for track in tracks if track['path'] == str(aligned))
    response = client.post(url, headers=headers, json={'preview_token': operation['preview_token'], 'keepers': keepers})
    assert response.status_code == 202
    completed = _run(response.json)
    assert completed['status'] == 'complete', completed['error']
    assert aligned.exists() and video.with_suffix('.hi.srt').exists() and video.with_suffix('.fr.forced.srt').exists()
    assert not video.with_suffix('.en.sup').exists() and not video.with_suffix('.en.broken.srt').exists()


def test_shared_sidecar_owner_refuses_cleanup_before_mutation(library):
    client, headers, media_id, video, _, lower = library
    other_id = media_id + 1
    other_video = video.parent / 'Different video.mkv'
    shutil.copyfile(video, other_video)
    database.add(TableMovies(radarrId=other_id, title='Shared subtitle owner', path=str(other_video),
                             tmdbId=str(other_id), movie_file_id=other_id))
    database.commit()
    database.add(TableMoviesSubtitles(radarrId=other_id, path=str(lower), language='en', forced=False, hi=True))
    database.commit()
    before = {path: _sha(path) for path in (video, other_video, lower)}
    try:
        response = client.post('/api/subtitles/cleanup/preview', headers=headers,
                               json={'media_type': 'movie', 'media_id': media_id})
        failed = _run(response.json)
        assert failed['status'] == 'failed' and 'another video' in failed['error']
        assert {path: _sha(path) for path in before} == before
    finally:
        database.execute(delete(TableMoviesSubtitles).where(TableMoviesSubtitles.radarrId == other_id))
        database.execute(delete(TableMovies).where(TableMovies.radarrId == other_id))
        database.commit()


def test_episode_cleanup_restores_inventory_without_touching_movie_decoy(library):
    client, headers, movie_id, movie, aligned, lower = library
    episode_id = movie_id + 10
    directory = movie.parent / 'Same title series'
    directory.mkdir()
    episode = directory / movie.name
    first, second = directory / aligned.name, directory / lower.name
    for source, target in ((movie, episode), (aligned, first), (lower, second)):
        shutil.copyfile(source, target)
    before = {path: _sha(path) for path in (movie, aligned, lower, episode, first, second)}
    database.add(TableShows(sonarrSeriesId=episode_id, title='Cleanup verification', path=str(directory)))
    database.commit()
    database.add(TableEpisodes(sonarrEpisodeId=episode_id, sonarrSeriesId=episode_id, title='Cleanup verification',
                               path=str(episode), season=1, episode=1, episode_file_id=episode_id,
                               file_size=episode.stat().st_size))
    database.commit()
    database.add_all([TableEpisodesSubtitles(sonarrEpisodeId=episode_id, sonarrSeriesId=episode_id,
                                             path=str(path), language='en', forced=False, hi=hearing_impaired)
                      for path, hearing_impaired in ((first, False), (second, True))])
    database.commit()
    try:
        response = client.post('/api/subtitles/cleanup/preview', headers=headers,
                               json={'media_type': 'series', 'media_id': episode_id})
        assert response.status_code == 202
        operation = _run(response.json)
        assert operation['status'] == 'ready', operation['error']
        completed = _apply(library, operation)
        assert completed['status'] == 'complete' and completed['error'] is None
        assert not second.exists()
        assert {path: _sha(path) for path in (movie, aligned, lower)} == {
            path: before[path] for path in (movie, aligned, lower)}
        response = client.post(f'/api/subtitles/cleanup/{operation["id"]}/restore', headers=headers,
                               json={'preview_token': operation['preview_token']})
        restored = _run(response.json)
        assert restored['status'] == 'restored' and restored['error'] is None
        assert {path: _sha(path) for path in before} == before
        assert database.execute(select(TableEpisodesSubtitles).where(TableEpisodesSubtitles.path == str(second))).first()
    finally:
        database.execute(delete(TableSubtitleCleanupOperations).where(
            TableSubtitleCleanupOperations.media_type == 'series', TableSubtitleCleanupOperations.media_id == episode_id))
        database.execute(delete(TableEpisodesSubtitles).where(TableEpisodesSubtitles.sonarrEpisodeId == episode_id))
        database.execute(delete(TableEpisodes).where(TableEpisodes.sonarrEpisodeId == episode_id))
        database.execute(delete(TableShows).where(TableShows.sonarrSeriesId == episode_id))
        database.commit()
