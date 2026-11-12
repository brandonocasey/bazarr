from collections import Counter
from pathlib import Path

import pytest

from app import database as app_database
from app.config import settings
from app.database import TableMovies, TableMoviesSubtitles
from subtitles.cleanup import store
from utilities import helper


@pytest.mark.parametrize('case', [(size, mode) for size in (1, 30, 300)
                                  for mode in ('current', 'relative', 'absolute')])
def test_inventory_reads_destination_settings_once_per_scan(
        memory_session, monkeypatch, tmp_path, case):
    library_size, mode = case
    monkeypatch.setattr(app_database, 'database', memory_session)
    monkeypatch.setattr(store, 'database', memory_session)
    video = tmp_path / 'Movie ü ; v1.2.mkv'
    video.write_bytes(b'video')
    local = video.with_suffix('.en.srt')
    local.write_text('local subtitle')
    destination = tmp_path / '字幕 ; shared'
    destination.mkdir()
    configured = destination / (video.stem + '.fr.srt')
    configured.write_text('configured subtitle')
    foreign_video = video.with_name('Other ü ; v1.2.mkv')
    foreign_video.write_bytes(b'foreign video')
    foreign_local = foreign_video.with_suffix('.en.srt')
    foreign_local.write_text('foreign local subtitle')
    foreign_custom = destination / foreign_local.name
    foreign_custom.write_text('foreign configured subtitle')
    sources = [video, local, configured, foreign_video, foreign_local, foreign_custom]
    before = {path: path.read_bytes() for path in sources}
    memory_session.add_all([
        TableMovies(radarrId=index + 1, title=f'Movie {index}', tmdbId=str(index + 1),
                    path=str(video if index == 0 else tmp_path / f'library {index}' / f'Other {index}.mkv'))
        for index in range(library_size)
    ])
    memory_session.commit()
    memory_session.add(TableMoviesSubtitles(radarrId=1, path=str(local), language='en', forced=False, hi=False))
    memory_session.commit()
    general = settings.general
    monkeypatch.setattr(general, 'subfolder', mode)
    monkeypatch.setattr(general, 'subfolder_custom', destination.name if mode == 'relative' else str(destination))
    reads = Counter()
    original_getattr = type(general).__getattr__

    def record_read(node, name):
        if node is general and name in ('subfolder', 'subfolder_custom'):
            reads[name] += 1
        return original_getattr(node, name)

    monkeypatch.setattr(type(general), '__getattr__', record_read)
    for scan_mode in (mode, 'relative' if mode == 'current' else 'current'):
        monkeypatch.setattr(general, 'subfolder', scan_mode)
        monkeypatch.setattr(general, 'subfolder_custom',
                            destination.name if scan_mode == 'relative' else str(destination))
        reads.clear()
        inventory = store.indexed_subtitles('movie', 1)
        expected = [{'path': str(local), 'language': 'en', 'forced': False, 'hearing_impaired': False}]
        if scan_mode != 'current':
            expected.append({'path': str(configured), 'language': 'fr', 'forced': False,
                             'hearing_impaired': False})
        assert inventory == sorted(expected, key=lambda item: item['path'])
        assert {path: path.read_bytes() for path in sources} == before
        assert reads == {'subfolder': 1, 'subfolder_custom': 1}, (library_size, scan_mode, reads)


@pytest.mark.parametrize('case', [
    ('current', ' captions ', None, None),
    ('relative', ' 字幕 ; v1.2 ', 'media/字幕 ; v1.2', '字幕 ; v1.2'),
    ('absolute', ' 字幕 ; v1.2 ', '字幕 ; v1.2', '字幕 ; v1.2'),
    ('relative', '/absolute captions', '/absolute captions', '/absolute captions'),
    ('absolute', '/absolute captions', '/absolute captions', '/absolute captions'),
    ('unknown', ' captions ', None, 'captions'),
    ('relative', '', None, None),
    ('absolute', '  ', None, ''),
    ('relative', None, None, None),
])
def test_target_folder_retains_lexical_and_indexer_contract(
        monkeypatch, tmp_path, case):
    mode, custom, target, destination = case
    absolute = str(tmp_path / 'absolute captions')
    if custom == '/absolute captions':
        custom = target = destination = absolute
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings.general, 'subfolder', mode)
    monkeypatch.setattr(settings.general, 'subfolder_custom', custom)
    assert helper.get_subtitle_destination_folder() == destination
    assert helper.get_subtitle_target_path('media/Movie.mkv') == target
    directories = {Path('media')}
    if target is not None:
        directories.add(Path(target))
    assert helper.get_subtitle_output_directories('media/Movie.mkv') == directories
    assert helper.get_target_folder('media/Movie.mkv') == target
    if target is not None:
        assert (tmp_path / target).is_dir()


def test_target_folder_preserves_symlink_spelling_and_creation_failure(monkeypatch, tmp_path, caplog):
    directory = tmp_path / '字幕 ; real'
    directory.mkdir()
    alias = tmp_path / '字幕 ; alias'
    alias.symlink_to(directory, target_is_directory=True)
    monkeypatch.setattr(settings.general, 'subfolder', 'absolute')
    monkeypatch.setattr(settings.general, 'subfolder_custom', str(alias))
    assert helper.get_target_folder('media/Movie.mkv') == str(alias)
    blocked = tmp_path / 'ordinary file'
    blocked.write_bytes(b'keep')
    monkeypatch.setattr(settings.general, 'subfolder_custom', str(blocked / 'captions'))
    assert helper.get_target_folder('media/Movie.mkv') is None
    assert f'BAZARR is unable to create directory to save subtitles: {blocked / "captions"}' in caplog.text
    assert blocked.read_bytes() == b'keep'
