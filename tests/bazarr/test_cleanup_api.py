import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest

from app.database import (database, select, delete, TableMovies, TableMoviesSubtitles, TableHistoryMovie,
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
def library(api_client, api_key, tmp_path, monkeypatch, media):
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
    yield api_client, {'X-API-KEY': api_key}, media_id, video, aligned, lower
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
