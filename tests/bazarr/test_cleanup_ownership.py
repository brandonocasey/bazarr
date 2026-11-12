from pathlib import Path
import shutil

import pytest

from app.config import settings
from app.database import database, delete, TableMovies, TableShows, TableEpisodes
from tests.bazarr import test_cleanup_api as api_fixtures
from utilities.path_mappings import path_mappings


authenticated_client = api_fixtures.authenticated_client
library = api_fixtures.library
media = api_fixtures.media


@pytest.fixture
def other_movie():
    media_id = 170002

    def add(path):
        database.add(TableMovies(radarrId=media_id, title='Other movie', path=str(path),
                                 tmdbId=str(media_id), movie_file_id=media_id))
        database.commit()

    yield add
    database.execute(delete(TableMovies).where(TableMovies.radarrId == media_id))
    database.commit()


@pytest.mark.parametrize('location', ['current', 'relative', 'absolute', 'mapped-absolute', 'unindexed'])
def test_overlapping_video_stems_fail_preview_without_losing_unindexed_subtitles(
        library, other_movie, monkeypatch, location):
    client, headers, media_id, video, aligned, lower = library
    other = video.with_name(video.stem + '.Extended.mkv')
    shutil.copyfile(video, other)
    directory = video.parent
    if location in ('relative', 'absolute', 'mapped-absolute'):
        directory = video.parent / '字幕 ; shared'
        directory.mkdir()
        monkeypatch.setattr(settings.general, 'subfolder', 'relative' if location == 'relative' else 'absolute')
        monkeypatch.setattr(settings.general, 'subfolder_custom', directory.name if location == 'relative' else str(directory))
    indexed_path = other
    if location == 'mapped-absolute':
        monkeypatch.setattr(path_mappings, 'path_mapping_movies', [['/remote/library', str(video.parent)]])
        indexed_path = Path('/remote/library') / other.name
    if location != 'unindexed':
        other_movie(indexed_path)
    foreign = directory / (other.stem + '.en.srt')
    foreign.write_bytes(lower.read_bytes())
    before = {path: api_fixtures._sha(path) for path in (video, aligned, lower, other, foreign)}

    response = client.post('/api/subtitles/cleanup/preview', headers=headers,
                           json={'media_type': 'movie', 'media_id': media_id})
    assert response.status_code == 202, response.json
    failed = api_fixtures._run(response.json)
    assert failed['status'] == 'failed'
    assert 'ambiguous video owners' in failed['error'], failed['error']
    response = client.post(f'/api/subtitles/cleanup/{failed["id"]}/apply', headers=headers,
                           json={'preview_token': failed['preview_token'], 'keepers': {}})
    assert response.status_code == 409
    assert {path: api_fixtures._sha(path) for path in before} == before


@pytest.mark.parametrize('location', ['current', 'absolute'])
def test_unique_video_owner_can_complete_cleanup_with_foreign_sidecars_present(
        library, other_movie, monkeypatch, location):
    _, _, _, video, _, lower = library
    other = video.with_name('Another film ü ; v1.2.mkv')
    shutil.copyfile(video, other)
    other_movie(other)
    directory = video.parent
    if location == 'absolute':
        directory = video.parent / 'shared'
        directory.mkdir()
        monkeypatch.setattr(settings.general, 'subfolder', 'absolute')
        monkeypatch.setattr(settings.general, 'subfolder_custom', str(directory))
    foreign = directory / (other.stem + '.en.srt')
    foreign.write_bytes(lower.read_bytes())
    before = {path: api_fixtures._sha(path) for path in (other, foreign)}
    operation = api_fixtures._preview(library)
    assert str(foreign) not in {track['path'] for track in operation['analysis']['tracks']}
    completed = api_fixtures._apply(library, operation)
    assert completed['status'] == 'complete', completed['error']
    assert {path: api_fixtures._sha(path) for path in before} == before


@pytest.mark.parametrize('location', ['same-directory', 'absolute'])
def test_ambiguous_full_video_identity_fails_preview_and_preserves_every_source(
        library, other_movie, monkeypatch, location):
    client, headers, media_id, video, aligned, lower = library
    if location == 'same-directory':
        other = video.with_suffix('.mp4')
        foreign = video.with_suffix('.fr.srt')
    else:
        other_directory = video.parent / 'other library'
        other_directory.mkdir()
        other = other_directory / video.name
        shared = video.parent / 'shared subtitles'
        shared.mkdir()
        monkeypatch.setattr(settings.general, 'subfolder', 'absolute')
        monkeypatch.setattr(settings.general, 'subfolder_custom', str(shared))
        foreign = shared / (video.stem + '.en.srt')
    shutil.copyfile(video, other)
    other_movie(other)
    foreign.write_bytes(lower.read_bytes())
    before = {path: api_fixtures._sha(path) for path in (video, aligned, lower, other, foreign)}

    response = client.post('/api/subtitles/cleanup/preview', headers=headers,
                           json={'media_type': 'movie', 'media_id': media_id})
    assert response.status_code == 202, response.json
    failed = api_fixtures._run(response.json)
    assert failed['status'] == 'failed'
    assert 'owner' in failed['error'].lower(), failed['error']
    assert {path: api_fixtures._sha(path) for path in before} == before


def test_mapped_episode_with_same_basename_prevents_shared_directory_cleanup(library, monkeypatch):
    client, headers, media_id, video, aligned, lower = library
    directory = video.parent / 'episode library'
    directory.mkdir()
    episode = directory / video.name
    shutil.copyfile(video, episode)
    shared = video.parent / 'shared'
    shared.mkdir()
    foreign = shared / aligned.name
    foreign.write_bytes(aligned.read_bytes())
    monkeypatch.setattr(settings.general, 'subfolder', 'absolute')
    monkeypatch.setattr(settings.general, 'subfolder_custom', str(shared))
    monkeypatch.setattr(path_mappings, 'path_mapping_series', [['/remote/episodes', str(directory)]])
    database.add(TableShows(sonarrSeriesId=170003, title='Other show', path='/remote/episodes'))
    database.commit()
    database.add(TableEpisodes(sonarrSeriesId=170003, sonarrEpisodeId=170003, title='Other episode',
                               path=str(Path('/remote/episodes') / video.name), season=1, episode=1))
    database.commit()
    before = {path: api_fixtures._sha(path) for path in (video, aligned, lower, episode, foreign)}
    try:
        response = client.post('/api/subtitles/cleanup/preview', headers=headers,
                               json={'media_type': 'movie', 'media_id': media_id})
        assert response.status_code == 202, response.json
        failed = api_fixtures._run(response.json)
        assert failed['status'] == 'failed'
        assert 'ambiguous video owners' in failed['error'], failed['error']
        assert {path: api_fixtures._sha(path) for path in before} == before
    finally:
        database.execute(delete(TableEpisodes).where(TableEpisodes.sonarrEpisodeId == 170003))
        database.execute(delete(TableShows).where(TableShows.sonarrSeriesId == 170003))
        database.commit()


def test_case_variant_full_video_names_remain_distinct_owners(library, other_movie):
    client, headers, media_id, video, aligned, lower = library
    other = video.with_name(video.stem.lower() + '.MP4')
    shutil.copyfile(video, other)
    other_movie(other)
    before = {path: api_fixtures._sha(path) for path in (video, aligned, lower, other)}
    response = client.post('/api/subtitles/cleanup/preview', headers=headers,
                           json={'media_type': 'movie', 'media_id': media_id})
    assert response.status_code == 202, response.json
    failed = api_fixtures._run(response.json)
    assert failed['status'] == 'failed'
    assert 'ambiguous video owners' in failed['error'], failed['error']
    assert {path: api_fixtures._sha(path) for path in before} == before


def test_directory_alias_of_same_video_is_one_owner(library, other_movie):
    _, _, _, video, _, _ = library
    alias = video.parent / 'library alias'
    alias.symlink_to(video.parent, target_is_directory=True)
    other_movie(alias / video.name)
    operation = api_fixtures._preview(library)
    completed = api_fixtures._apply(library, operation)
    assert completed['status'] == 'complete', completed['error']


def test_owner_collision_appearing_after_preview_prevents_all_mutations(library, other_movie):
    _, _, _, video, aligned, lower = library
    operation = api_fixtures._preview(library)
    other = video.with_suffix('.mp4')
    shutil.copyfile(video, other)
    other_movie(other)
    before = {path: api_fixtures._sha(path) for path in (video, aligned, lower, other)}
    failed = api_fixtures._apply(library, operation)
    assert failed['status'] == 'failed'
    assert 'ambiguous video owners' in failed['error'], failed['error']
    assert {path: api_fixtures._sha(path) for path in before} == before
