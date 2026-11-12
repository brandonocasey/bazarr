import os
from types import SimpleNamespace

import pytest


class ProbeReached(Exception):
    pass


@pytest.fixture(params=['movie', 'episode'])
def extraction_selection(request, memory_session, monkeypatch, tmp_path):
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
    monkeypatch.setattr(embedded, 'database', memory_session)
    monkeypatch.setattr(embedded.jobs_queue, 'update_job_name', lambda **kwargs: None)

    def probe(path):
        assert path == str(video)
        raise ProbeReached('selection reached media probing')

    monkeypatch.setattr(embedded, 'FFprobeVideoContainer', probe)
    if request.param == 'movie':
        owner = TableMovies(radarrId=7, path=str(video), title='Film', year='2026', tmdbId='7')
        memory_session.add(owner)
        memory_session.commit()
        subtitle = TableMoviesSubtitles(radarrId=7, embedded_track_id=3, language='en', forced=False, hi=False)
    else:
        memory_session.add(TableShows(sonarrSeriesId=8, path=str(tmp_path), title='Show'))
        memory_session.commit()
        owner = TableEpisodes(sonarrEpisodeId=7, sonarrSeriesId=8, path=str(video), title='Film', season=1, episode=1)
        memory_session.add(owner)
        memory_session.commit()
        subtitle = TableEpisodesSubtitles(sonarrEpisodeId=7, sonarrSeriesId=8, embedded_track_id=3,
                                         language='en', forced=False, hi=False)
    memory_session.add(subtitle)
    memory_session.commit()
    return SimpleNamespace(video=video, owner=owner, subtitle=subtitle, database=memory_session,
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
