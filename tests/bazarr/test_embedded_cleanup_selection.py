import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session


class ProbeReached(Exception):
    pass


@pytest.fixture
def selection_database(tmp_path):
    from app.database import metadata

    engine = create_engine(f'sqlite:///{tmp_path / "selection.db"}')
    metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


@pytest.fixture(params=['movie', 'episode'])
def extraction_selection(request, selection_database, monkeypatch, tmp_path):
    from app.database import TableEpisodes, TableEpisodesSubtitles, TableMovies, TableMoviesSubtitles, TableShows
    from app.get_args import args
    from subtitles import embedded
    from utilities import media_write_lock as coordination

    video = tmp_path / 'Film ü; v1.2.mkv'
    video.write_bytes(b'original video')
    config = tmp_path / 'config'
    (config / 'config').mkdir(parents=True)
    monkeypatch.setattr(args, 'config_dir', str(config))
    monkeypatch.setattr(coordination, '_guards', [])
    monkeypatch.setattr(embedded, 'database', selection_database)
    monkeypatch.setattr(embedded.jobs_queue, 'update_job_name', lambda **kwargs: None)

    def probe(path):
        assert path == str(video)
        raise ProbeReached('selection reached media probing')

    monkeypatch.setattr(embedded, 'FFprobeVideoContainer', probe)
    if request.param == 'movie':
        owner = TableMovies(radarrId=7, path=str(video), title='Film', year='2026', tmdbId='7')
        selection_database.add(owner)
        selection_database.commit()
        subtitle = TableMoviesSubtitles(radarrId=7, embedded_track_id=3, language='en', forced=False, hi=False)
    else:
        selection_database.add(TableShows(sonarrSeriesId=8, path=str(tmp_path), title='Show'))
        selection_database.commit()
        owner = TableEpisodes(sonarrEpisodeId=7, sonarrSeriesId=8, path=str(video), title='Film', season=1, episode=1)
        selection_database.add(owner)
        selection_database.commit()
        subtitle = TableEpisodesSubtitles(sonarrEpisodeId=7, sonarrSeriesId=8, embedded_track_id=3,
                                         language='en', forced=False, hi=False)
    selection_database.add(subtitle)
    selection_database.commit()
    return SimpleNamespace(video=video, owner=owner, subtitle=subtitle, database=selection_database,
                           media_type=request.param, register=coordination.register_media_write_guard,
                           extract=embedded.extract_embedded_subtitle)


@pytest.mark.parametrize('change', ['renumbered', 'same_fields', 'removed', 'path_changed', 'language_changed'])
def test_extraction_rejects_selection_changed_by_waiting_writer(extraction_selection, change):
    state = extraction_selection
    subtitle_id = state.subtitle.id

    def rewrite(path):
        assert path == str(state.video)
        if change in ('renumbered', 'same_fields'):
            replacement = state.video.with_suffix('.replacement.mkv')
            replacement.write_bytes(b'rewritten video')
            os.replace(replacement, state.video)
        if change == 'renumbered':
            state.subtitle.embedded_track_id = 2
        elif change == 'removed':
            state.database.delete(state.subtitle)
        elif change == 'path_changed':
            state.owner.path = str(state.video.parent / 'decoy' / state.video.name)
        elif change == 'language_changed':
            state.subtitle.language = 'fr'
        state.database.commit()

    state.register(rewrite)
    from subtitles.embedded import EmbeddedSubtitlesExtractionError
    with pytest.raises(EmbeddedSubtitlesExtractionError):
        state.extract(subtitle_id, state.media_type, job_id=1)
    assert not list(state.video.parent.glob('*.srt'))


def test_extraction_accepts_unchanged_selection(extraction_selection):
    state = extraction_selection
    with pytest.raises(ProbeReached):
        state.extract(state.subtitle.id, state.media_type, job_id=1)


def test_extraction_accepts_selection_inside_own_writer(extraction_selection):
    from utilities.media_write_lock import media_write_lock

    state = extraction_selection
    with media_write_lock(str(state.video)):
        with pytest.raises(ProbeReached):
            state.extract(state.subtitle.id, state.media_type, job_id=1)


def test_extraction_binds_current_inventory_after_completed_writer(extraction_selection):
    from utilities.media_write_lock import media_write_lock

    state = extraction_selection
    with media_write_lock(str(state.video)):
        state.video.write_bytes(b'current video with current inventory')
    with pytest.raises(ProbeReached):
        state.extract(state.subtitle.id, state.media_type, job_id=1)


def test_extraction_rejects_writer_already_replaced_media_before_selection(extraction_selection):
    from subtitles.embedded import EmbeddedSubtitlesExtractionError
    from utilities.media_write_lock import media_write_lock

    state = extraction_selection
    subtitle_id = state.subtitle.id
    replaced, release, selected = threading.Event(), threading.Event(), threading.Event()

    def writer():
        with media_write_lock(str(state.video)):
            replacement = state.video.with_suffix('.replacement.mkv')
            replacement.write_bytes(b'rewritten video with the same subtitle row')
            os.replace(replacement, state.video)
            replaced.set()
            assert release.wait(5)

    def selection_read(connection, cursor, statement, *context):
        if 'embedded_track_id' in statement:
            selected.set()

    engine = state.database.get_bind()
    event.listen(engine, 'after_cursor_execute', selection_read)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            rewriting = pool.submit(writer)
            assert replaced.wait(5)
            extraction = pool.submit(state.extract, subtitle_id, state.media_type, job_id=1)
            try:
                assert selected.wait(5)
            finally:
                release.set()
            rewriting.result(timeout=5)
            assert isinstance(extraction.exception(timeout=5), EmbeddedSubtitlesExtractionError)
    finally:
        event.remove(engine, 'after_cursor_execute', selection_read)
    assert not list(state.video.parent.glob('*.srt'))


def test_extraction_rejects_snapshot_from_interrupted_writer(extraction_selection):
    from app.get_args import args
    from pathlib import Path
    from subtitles.embedded import EmbeddedSubtitlesExtractionError

    state = extraction_selection
    script = '''
import os, sys
os.environ['NO_CLI'] = 'true'
import bazarr.app.libs
from app.get_args import args
from utilities.media_write_lock import media_write_lock
from pathlib import Path
args.config_dir = sys.argv[1]
with media_write_lock(sys.argv[2]):
    Path(sys.argv[2]).write_bytes(b'rewritten video before inventory recovery')
    os._exit(0)
'''
    environment = dict(os.environ)
    environment['PYTHONPATH'] = os.pathsep.join((str(Path.cwd()), str(Path.cwd() / 'bazarr')))
    process = subprocess.run([sys.executable, '-c', script, args.config_dir, str(state.video)],
                             env=environment, capture_output=True, text=True, timeout=15)
    assert process.returncode == 0, process.stderr
    with pytest.raises(EmbeddedSubtitlesExtractionError):
        state.extract(state.subtitle.id, state.media_type, job_id=1)


def test_extraction_rejects_writer_finishing_before_first_file_sample(extraction_selection, monkeypatch):
    from subtitles.embedded import EmbeddedSubtitlesExtractionError
    from utilities.media_write_lock import media_write_lock

    state = extraction_selection
    subtitle_id = state.subtitle.id
    sampling, rewritten = threading.Event(), threading.Event()
    original_stat = os.stat

    def sample(path, *arguments, **keywords):
        if path in (state.video, str(state.video)) and not sampling.is_set():
            sampling.set()
            assert rewritten.wait(5)
        return original_stat(path, *arguments, **keywords)

    def writer():
        assert sampling.wait(5)
        with media_write_lock(str(state.video)):
            replacement = state.video.with_suffix('.replacement.mkv')
            replacement.write_bytes(b'rewritten video before the first sample')
            os.replace(replacement, state.video)
        rewritten.set()

    monkeypatch.setattr(os, 'stat', sample)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rewriting = pool.submit(writer)
        extraction = pool.submit(state.extract, subtitle_id, state.media_type, job_id=1)
        rewriting.result(timeout=5)
        assert isinstance(extraction.exception(timeout=5), EmbeddedSubtitlesExtractionError)


def test_extraction_cannot_rebind_to_another_video_between_locator_and_selection(extraction_selection):
    from sqlalchemy import update
    from subtitles.embedded import EmbeddedSubtitlesExtractionError

    state = extraction_selection
    subtitle_id = state.subtitle.id
    decoy = state.video.parent / 'decoy' / state.video.name
    decoy.parent.mkdir()
    decoy.write_bytes(b'other video')
    owner = type(state.owner)
    identifier = owner.radarrId if state.media_type == 'movie' else owner.sonarrEpisodeId
    moved = threading.Event()

    def move_owner(connection, cursor, statement, *context):
        if 'JOIN' in statement and 'embedded_track_id' not in statement and not moved.is_set():
            moved.set()
            connection.execute(update(owner).where(identifier == 7).values(path=str(decoy)))

    engine = state.database.get_bind()
    event.listen(engine, 'after_cursor_execute', move_owner)
    try:
        with pytest.raises(EmbeddedSubtitlesExtractionError):
            state.extract(subtitle_id, state.media_type, job_id=1)
    finally:
        event.remove(engine, 'after_cursor_execute', move_owner)
    assert moved.is_set()
    assert state.video.read_bytes() == b'original video'
    assert decoy.read_bytes() == b'other video'
    assert not list(decoy.parent.glob('*.srt'))


def test_extraction_rejects_recovered_inventory_even_when_selected_fields_are_unchanged(extraction_selection):
    from sqlalchemy import update
    from subtitles.embedded import EmbeddedSubtitlesExtractionError

    state = extraction_selection
    subtitle_id = state.subtitle.id
    state.video.write_bytes(b'video already replaced before recovery')

    def recover(path):
        assert path == str(state.video)
        table = type(state.subtitle)
        state.database.execute(update(table).where(table.id == subtitle_id).values(language='en'))
        state.database.commit()
        return True

    state.register(recover)
    with pytest.raises(EmbeddedSubtitlesExtractionError):
        state.extract(subtitle_id, state.media_type, job_id=1)
