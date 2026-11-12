# coding=utf-8

import os
from collections.abc import Iterable


def indexed_subtitle_owners(paths: Iterable[str]) -> set[str]:
    """Return canonical media owners for indexed subtitle paths and their mapped aliases."""
    from app.database import (database, select, TableEpisodes, TableEpisodesSubtitles,
                              TableMovies, TableMoviesSubtitles, SUBTITLE_MAP_QUERY_BATCH_SIZE)
    from utilities.path_mappings import path_mappings

    aliases = set(paths)
    aliases.update(os.path.normcase(os.path.realpath(path)) for path in tuple(aliases))
    owners: set[str] = set()
    for media, subtitles, identifier, reverse, forward in (
        (TableEpisodes, TableEpisodesSubtitles, 'sonarrEpisodeId',
         path_mappings.path_replace_reverse, path_mappings.path_replace),
        (TableMovies, TableMoviesSubtitles, 'radarrId',
         path_mappings.path_replace_reverse_movie, path_mappings.path_replace_movie),
    ):
        values = sorted(aliases | {reverse(path) for path in aliases})
        for offset in range(0, len(values), SUBTITLE_MAP_QUERY_BATCH_SIZE):
            rows = database.execute(select(media.path).join(
                subtitles, getattr(media, identifier) == getattr(subtitles, identifier))
                .where(subtitles.path.in_(values[offset:offset + SUBTITLE_MAP_QUERY_BATCH_SIZE]))).all()
            owners.update(os.path.normcase(os.path.realpath(forward(row.path))) for row in rows)
    return owners
