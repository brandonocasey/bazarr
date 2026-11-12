import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from utilities import media_write_lock as coordination


class RecoveryRequired(BaseException):
    pass


@pytest.fixture
def guard(monkeypatch, tmp_path):
    from app.get_args import args

    (tmp_path / 'config' / 'config').mkdir(parents=True)
    monkeypatch.setattr(args, 'config_dir', str(tmp_path / 'config'))
    monkeypatch.setattr(coordination, '_guards', [])
    return coordination.register_media_write_guard


@pytest.fixture
def media(memory_session, monkeypatch, tmp_path):
    import app.database as app_database
    from app.database import TableMovies, TableMoviesSubtitles

    video = tmp_path / 'Movie ü; v1.2.mkv'
    subtitle = tmp_path / 'Movie ü; v1.2.en.srt'
    video.write_bytes(b'video')
    subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\nHello!\n')
    monkeypatch.setattr(app_database, 'database', memory_session)
    memory_session.add(TableMovies(radarrId=7, path=str(video), title='Movie', year='2026', tmdbId='7'))
    memory_session.commit()
    external = TableMoviesSubtitles(radarrId=7, path=str(subtitle), language='en', forced=False, hi=False)
    embedded = TableMoviesSubtitles(radarrId=7, embedded_track_id=2, language='en', forced=False, hi=False)
    memory_session.add_all([external, embedded])
    memory_session.commit()
    return video, subtitle, embedded.id


def test_nested_recovery_runs_once_before_postprocessing(guard, tmp_path):
    from subtitles.post_processing import postprocessing

    video = tmp_path / 'Movie.mkv'
    alias = tmp_path / 'alias.mkv'
    alias.symlink_to(video)
    output = tmp_path / 'sequence'

    def recover(path):
        assert path == str(video)
        with coordination.media_write_lock(str(alias)):
            output.write_text('recovered')

    guard(recover)
    with coordination.media_write_lock(str(video)):
        postprocessing([sys.executable, '-c',
                        'from pathlib import Path; import sys; p=Path(sys.argv[1]); '
                        'p.write_text(p.read_text()+";written")', str(output)], str(alias))
    assert output.read_text() == 'recovered;written'


def test_guard_failure_and_writer_exception_release_lock(guard, tmp_path):
    video = str(tmp_path / 'Movie.mkv')
    attempts = []

    def recover(path):
        attempts.append(path)
        if len(attempts) == 1:
            raise RuntimeError('recovery failed')

    guard(recover)
    with pytest.raises(RuntimeError, match='recovery failed'):
        with coordination.media_write_lock(video):
            pytest.fail('writer started before recovery completed')
    with pytest.raises(ValueError, match='writer failed'):
        with coordination.media_write_lock(video):
            raise ValueError('writer failed')
    with ThreadPoolExecutor(max_workers=1) as pool:
        def write():
            with coordination.media_write_lock(video):
                return 'released'
        assert pool.submit(write).result(timeout=5) == 'released'
    assert len(attempts) == 3


def test_simultaneous_processes_serialize_actual_postprocessor(guard, tmp_path):
    from app.get_args import args

    video = str(tmp_path / 'Movie.mkv')
    output = tmp_path / 'sequence'
    started = tmp_path / 'started'
    script = '''
import os, sys
os.environ['NO_CLI'] = 'true'
import bazarr.app.libs
from app.get_args import args
from subtitles.post_processing import postprocessing
args.config_dir = sys.argv[1]
from pathlib import Path
Path(sys.argv[4]).write_text('started')
postprocessing([sys.executable, '-c', 'from pathlib import Path; import sys; p=Path(sys.argv[1]); p.write_text(p.read_text()+";child")', sys.argv[3]], sys.argv[2])
'''
    environment = dict(os.environ)
    environment['PYTHONPATH'] = os.pathsep.join((str(Path.cwd()), str(Path.cwd() / 'bazarr')))
    with coordination.media_write_lock(video):
        output.write_text('parent')
        process = subprocess.Popen([sys.executable, '-c', script, args.config_dir, video, str(output), str(started)],
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # The child announces immediately before the real writer entry point.
            for _ in range(500):
                if started.exists():
                    break
                threading.Event().wait(0.01)
            assert started.exists(), 'child did not reach writer'
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.25)
            assert output.read_text() == 'parent'
        except BaseException:
            process.kill()
            process.communicate()
            raise
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 0, (stdout, stderr)
    assert output.read_text() == 'parent;child'


def test_subtitle_owner_identity_rejects_decoy_and_accepts_alias(media, tmp_path):
    video, subtitle, _ = media
    alias = tmp_path / 'alias.srt'
    alias.symlink_to(subtitle)
    assert coordination.subtitle_media_path(str(alias), str(video)) == str(video)
    with pytest.raises(OSError, match='does not belong'):
        coordination.subtitle_media_path(str(subtitle), str(tmp_path / 'Other.mkv'))
    with pytest.raises(OSError, match='not indexed'):
        coordination.subtitle_media_path(str(tmp_path / 'Other.en.srt'), str(video))


def test_fresh_sidecars_require_indexed_video_and_configured_location(media, monkeypatch):
    from app.config import settings

    video, _, _ = media
    monkeypatch.setattr(settings.general, 'subfolder', 'relative')
    monkeypatch.setattr(settings.general, 'subfolder_custom', 'captions')
    destination = video.parent / 'captions'
    fresh = destination / (video.stem + '.fr.srt')
    assert coordination.subtitle_media_path(str(fresh), str(video), allow_unindexed=True) == str(video)
    assert not destination.exists(), 'ownership validation must remain read-only'
    assert coordination.subtitle_media_path(str(video.parent / fresh.name), str(video),
                                            allow_unindexed=True) == str(video)
    for decoy in (video.parent / 'unconfigured' / fresh.name, destination / 'Other.fr.srt'):
        with pytest.raises(OSError, match='not indexed'):
            coordination.subtitle_media_path(str(decoy), str(video), allow_unindexed=True)


@pytest.mark.parametrize('route', ['mods', 'sync', 'translation', 'delete', 'extraction'])
def test_real_writer_stops_before_first_effect_when_recovery_fails(route, guard, media, memory_session, monkeypatch):
    video, subtitle, embedded_id = media
    before = subtitle.read_bytes()

    def recover(path):
        assert path == str(video)
        raise RecoveryRequired('recover journal before mutation')

    guard(recover)
    with pytest.raises(RecoveryRequired):
        if route == 'mods':
            from subtitles.tools.mods import subtitles_apply_mods
            subtitles_apply_mods('en', str(subtitle), ['remove_HI'], str(video))
        elif route == 'sync':
            from subtitles.tools.subsyncer import SubSyncer
            SubSyncer().sync(str(video), str(subtitle), 'en', False, False, '60', False, False)
        elif route == 'translation':
            from subtitles.tools.translate.main import translate_subtitles_file
            translate_subtitles_file(str(video), str(subtitle), 'en', 'fr', False, False,
                                     'movie', None, None, 7, None, job_id=1)
        elif route == 'delete':
            from subtitles.tools import delete
            monkeypatch.setattr(delete, 'database', memory_session)
            delete.delete_subtitles('movie', 'en', False, False, str(video), str(subtitle), radarr_id=7)
        else:
            from subtitles import embedded
            monkeypatch.setattr(embedded, 'database', memory_session)
            monkeypatch.setattr(embedded.jobs_queue, 'update_job_name', lambda **kwargs: None)
            embedded.extract_embedded_subtitle(embedded_id, 'movie', job_id=1)
    assert subtitle.read_bytes() == before


def test_generate_save_runs_recovery_before_save(guard):
    from tests.bazarr.test_download_blacklist_on_failure import _run_generate_subtitles

    guard(lambda path: (_ for _ in ()).throw(RecoveryRequired(path)))
    with pytest.raises(RecoveryRequired):
        _run_generate_subtitles(RuntimeError('save must never start'))


def test_manual_download_save_runs_recovery_before_save(guard, monkeypatch, media):
    from subtitles import manual
    from subzero.language import Language

    video, _, _ = media
    subtitle = SimpleNamespace(language=Language('eng'), format='srt', is_valid=lambda: True)
    monkeypatch.setattr(manual.subtitle_cache, 'get', lambda key: subtitle)
    monkeypatch.setattr(manual, 'get_video', lambda *args, **kwargs: SimpleNamespace(original_path=str(video)))
    monkeypatch.setattr(manual, 'download_subtitles', lambda *args: None)
    monkeypatch.setattr(manual, '_get_pool', lambda *args: None)
    guard(lambda path: (_ for _ in ()).throw(RecoveryRequired(path)))
    with pytest.raises(RecoveryRequired):
        manual.manual_download_subtitle(str(video), None, 'False', 'False', 'cached', 'provider', None,
                                        'Movie', 'movie', False, 1, job_id=1)


def test_upload_save_runs_recovery_before_save(guard, media, memory_session, monkeypatch):
    from subtitles import upload

    video, subtitle, _ = media
    monkeypatch.setattr(upload, 'database', memory_session)
    monkeypatch.setattr(upload, 'get_profiles_list', lambda *args, **kwargs: {})
    monkeypatch.setattr(upload, 'get_audio_profile_languages', lambda *args: None)
    guard(lambda path: (_ for _ in ()).throw(RecoveryRequired(path)))
    with pytest.raises(RecoveryRequired):
        upload.manual_upload_subtitle(str(video), 'en', False, False, 'movie', BytesIO(subtitle.read_bytes()),
                                      'caption.srt', None, job_id=1, radarrId=7)


def test_queued_sync_and_translation_do_not_acquire_writer_lock(guard, monkeypatch):
    from subtitles import sync
    from subtitles.tools.translate.main import translate_subtitles_file

    jobs = []
    monkeypatch.setattr(sync.settings.subsync, 'use_subsync', True)
    monkeypatch.setattr(sync.jobs_queue, 'add_job_from_function', lambda *args, **kwargs: jobs.append(args))
    guard(lambda path: pytest.fail('queue submission acquired a media lock'))
    assert sync.sync_subtitles('/unindexed.mkv', '/unindexed.srt', 'en', False, False, 100) is False
    translate_subtitles_file('/unindexed.mkv', '/unindexed.srt', 'en', 'fr', False, False,
                             'movie', None, None, 7, None)
    assert len(jobs) == 2


def test_fresh_sidecar_cannot_target_video_or_follow_outside_symlink(media, tmp_path, monkeypatch):
    from app.config import settings

    video, _, _ = media
    monkeypatch.setattr(settings.general, 'subfolder', 'current')
    monkeypatch.setattr(settings.general, 'subfolder_custom', '')
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / (video.stem + '.fr.srt')
    target.write_text('decoy')
    link = video.with_suffix('.fr.srt')
    link.symlink_to(target)
    for path in (video, link, video.with_suffix('.exe')):
        with pytest.raises(OSError, match='not indexed'):
            coordination.subtitle_media_path(str(path), str(video), allow_unindexed=True)
    assert video.read_bytes() == b'video'
    assert target.read_text() == 'decoy'


def test_unrelated_media_can_write_while_another_media_is_locked(guard, tmp_path):
    from subtitles.post_processing import postprocessing

    output = tmp_path / 'other-written'
    with ThreadPoolExecutor(max_workers=1) as pool:
        with coordination.media_write_lock(str(tmp_path / 'first.mkv')):
            task = pool.submit(postprocessing,
                               [sys.executable, '-c', 'from pathlib import Path; import sys; '
                                'Path(sys.argv[1]).write_text("other")', str(output)],
                               str(tmp_path / 'second.mkv'))
            task.result(timeout=5)
    assert output.read_text() == 'other'


def test_process_death_releases_lock_for_real_writer(guard, tmp_path):
    from app.get_args import args
    from subtitles.post_processing import postprocessing

    video = str(tmp_path / 'Movie.mkv')
    ready = tmp_path / 'ready'
    output = tmp_path / 'written'
    script = """
import os, sys, time
os.environ['NO_CLI'] = 'true'
import bazarr.app.libs
from app.get_args import args
from utilities.media_write_lock import media_write_lock
from pathlib import Path
args.config_dir = sys.argv[1]
with media_write_lock(sys.argv[2]):
    Path(sys.argv[3]).write_text('ready')
    time.sleep(30)
"""
    environment = dict(os.environ)
    environment['PYTHONPATH'] = os.pathsep.join((str(Path.cwd()), str(Path.cwd() / 'bazarr')))
    process = subprocess.Popen([sys.executable, '-c', script, args.config_dir, video, str(ready)],
                               env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(500):
            if ready.exists():
                break
            threading.Event().wait(0.01)
        assert ready.exists(), 'child did not acquire lock'
    finally:
        process.kill()
        process.communicate(timeout=5)
    postprocessing([sys.executable, '-c', 'from pathlib import Path; import sys; '
                    'Path(sys.argv[1]).write_text("written")', str(output)], video)
    assert output.read_text() == 'written'


def test_postprocessor_recovers_before_first_effect(guard, tmp_path):
    from subtitles.post_processing import postprocessing

    video = str(tmp_path / 'Movie.mkv')
    output = tmp_path / 'sequence'
    guard(lambda path: output.write_text('recovered'))
    postprocessing([sys.executable, '-c', 'from pathlib import Path; import sys; p=Path(sys.argv[1]); '
                    'p.write_text(p.read_text()+";written")', str(output)], video)
    assert output.read_text() == 'recovered;written'


def test_same_media_writer_waits_for_other_thread(guard, tmp_path):
    from concurrent.futures import TimeoutError
    from subtitles.post_processing import postprocessing

    video = str(tmp_path / 'Movie.mkv')
    output = tmp_path / 'written'
    started = threading.Event()

    def write():
        started.set()
        postprocessing([sys.executable, '-c', 'from pathlib import Path; import sys; '
                        'Path(sys.argv[1]).write_text("written")', str(output)], video)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with coordination.media_write_lock(video):
            task = pool.submit(write)
            assert started.wait(timeout=5)
            with pytest.raises(TimeoutError):
                task.result(timeout=0.25)
            assert not output.exists()
        task.result(timeout=5)
    assert output.read_text() == 'written'


def test_subtitle_indexed_for_two_videos_is_rejected(media, memory_session):
    from app.database import TableMovies, TableMoviesSubtitles

    video, subtitle, _ = media
    memory_session.add(TableMovies(radarrId=8, path=str(video.with_name('Other.mkv')), title='Other', tmdbId='8'))
    memory_session.commit()
    memory_session.add(TableMoviesSubtitles(radarrId=8, path=str(subtitle), language='en', forced=False, hi=False))
    memory_session.commit()
    with pytest.raises(OSError, match='multiple media'):
        coordination.subtitle_media_path(str(subtitle), str(video))


def test_fresh_sidecar_absolute_custom_folder(media, tmp_path, monkeypatch):
    from app.config import settings

    video, _, _ = media
    destination = tmp_path / 'absolute captions'
    monkeypatch.setattr(settings.general, 'subfolder', 'absolute')
    monkeypatch.setattr(settings.general, 'subfolder_custom', str(destination))
    path = destination / (video.stem + '.fr.ass')
    assert coordination.subtitle_media_path(str(path), str(video), allow_unindexed=True) == str(video)
    assert not destination.exists()


@pytest.mark.parametrize('extension', ['.vtt', '.srt'])
def test_fresh_translation_accepts_writer_formats_on_casefolding_paths(media, guard, monkeypatch, extension):
    from app.config import settings
    from subtitles.tools.translate.main import translate_subtitles_file

    video, _, _ = media
    source = video.with_suffix('.en' + extension)
    source.write_text('WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n')
    monkeypatch.setattr(settings.general, 'subfolder', 'current')
    monkeypatch.setattr(coordination.os.path, 'normcase', lambda value: value.lower())
    guard(lambda path: (_ for _ in ()).throw(RecoveryRequired(path)))
    with pytest.raises(RecoveryRequired):
        translate_subtitles_file(str(video), str(source), 'en', 'fr', False, False,
                                 'movie', None, None, 7, None, job_id=1)


def test_fresh_unindexed_mixed_case_basename_is_normalized(media, monkeypatch):
    from app.config import settings

    video, _, _ = media
    source = video.with_suffix('.de.srt')
    monkeypatch.setattr(settings.general, 'subfolder', 'current')
    monkeypatch.setattr(coordination.os.path, 'normcase', lambda value: value.lower())
    assert coordination.subtitle_media_path(str(source), str(video), allow_unindexed=True) == str(video).lower()


def test_cancellation_exits_cross_process_file_lock_wait(guard, tmp_path, monkeypatch):
    from app.get_args import args
    from app.jobs_queue import jobs_queue, JobCanceled

    video = str(tmp_path / 'Movie.mkv')
    ready = tmp_path / 'lock-ready'
    canceled = threading.Event()
    monkeypatch.setattr(jobs_queue, 'get_job', lambda job_id: SimpleNamespace(cancel_event=canceled))
    script = '''
import os,sys
os.environ['NO_CLI']='true'
import bazarr.app.libs
from app.get_args import args
from utilities.media_write_lock import media_write_lock
from pathlib import Path
args.config_dir=sys.argv[1]
with media_write_lock(sys.argv[2]):
    Path(sys.argv[3]).write_text('locked')
    sys.stdin.read(1)
'''
    environment = dict(os.environ)
    environment['PYTHONPATH'] = os.pathsep.join((str(Path.cwd()), str(Path.cwd() / 'bazarr')))
    process = subprocess.Popen([sys.executable, '-c', script, args.config_dir, video, str(ready)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=environment, text=True)
    try:
        for _ in range(500):
            if ready.exists():
                break
            threading.Event().wait(0.01)
        assert ready.exists(), 'child did not acquire file lock'

        def wait_for_writer():
            with coordination.media_write_lock(video, job_id=1):
                pytest.fail('canceled writer acquired the busy file lock')

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(wait_for_writer)
            canceled.set()
            with pytest.raises(JobCanceled):
                future.result(timeout=2)
        assert process.poll() is None
    finally:
        process.communicate('x', timeout=5)


def test_canceled_sync_leaves_writer_wait_without_releasing_other_writer(media, guard, monkeypatch):
    from app.jobs_queue import JobCanceled
    from subtitles.tools import subsyncer

    video, subtitle, _ = media
    canceled = threading.Event()
    started = threading.Event()
    monkeypatch.setattr(subsyncer.jobs_queue, 'get_job', lambda job_id: SimpleNamespace(cancel_event=canceled))

    def sync():
        started.set()
        return subsyncer.SubSyncer().sync(str(video), str(subtitle), 'en', False, False,
                                         60, False, False, job_id=1)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with coordination.media_write_lock(str(video)):
            future = pool.submit(sync)
            assert started.wait(5)
            canceled.set()
            try:
                with pytest.raises(JobCanceled):
                    future.result(timeout=2)
            finally:
                canceled.set()


def test_waiting_writer_recovers_after_guard_failure(guard, tmp_path):
    from concurrent.futures import TimeoutError

    video = str(tmp_path / 'Movie.mkv')
    entered = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    attempts = []

    def recover(path):
        attempts.append(path)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(timeout=5)
            raise RuntimeError('interrupted recovery')

    def write():
        waiting.set()
        with coordination.media_write_lock(video):
            return 'written'

    guard(recover)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write)
        assert entered.wait(timeout=5)
        waiting.clear()
        second = pool.submit(write)
        assert waiting.wait(timeout=5)
        try:
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match='interrupted recovery'):
            first.result(timeout=5)
        assert second.result(timeout=5) == 'written'
    assert len(attempts) == 2


def test_mapped_subtitle_and_video_use_local_canonical_identity(media, memory_session, monkeypatch):
    from app.database import TableMovies, TableMoviesSubtitles, select
    from utilities.path_mappings import path_mappings

    video, subtitle, _ = media
    movie = memory_session.get(TableMovies, 7)
    movie.path = '/radarr/' + video.name
    record = memory_session.execute(select(TableMoviesSubtitles).where(
        TableMoviesSubtitles.path == str(subtitle))).scalar_one()
    record.path = '/radarr/' + subtitle.name
    memory_session.commit()
    monkeypatch.setattr(path_mappings, 'path_mapping_movies', [('/radarr', str(video.parent))])
    assert coordination.subtitle_media_path(str(subtitle), str(video)) == str(video)
