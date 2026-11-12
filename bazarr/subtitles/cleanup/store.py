# coding=utf-8

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy.sql.dml import Update

from .records import IndexedSubtitle, MediaMetadata, Operation

from app.database import (database, select, update, TableSubtitleCleanupOperations, TableMovies,
                          TableEpisodes, TableMoviesSubtitles, TableEpisodesSubtitles,
                          TableHistory, TableHistoryMovie, SUBTITLE_MAP_QUERY_BATCH_SIZE)
from utilities.path_mappings import path_mappings
from utilities.helper import (SubtitleDirectorySettings, get_subtitle_directory_settings,
                              get_subtitle_output_directories)
from utilities.subtitle_ownership import indexed_subtitle_owners


class CleanupError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class OperationSnapshot:
    id: str
    payload: str
    job_id: int | None
    recovery_pending: bool

    def operation(self) -> Operation:
        operation = json.loads(self.payload)
        operation['job_id'] = self.job_id
        return operation


def _canonical_source(path: str) -> str:
    if Path(path).is_symlink():
        raise CleanupError(f'Cleanup cannot replace a symlink: {path}', 409)
    return os.path.realpath(path)


def media_metadata(media_type: str, media_id: int) -> MediaMetadata:
    if media_type == 'movie':
        row = database.execute(select(TableMovies.path, TableMovies.title)
                               .where(TableMovies.radarrId == media_id)).first()
        mapper = path_mappings.path_replace_movie
    elif media_type == 'series':
        row = database.execute(select(TableEpisodes.path, TableEpisodes.title)
                               .where(TableEpisodes.sonarrEpisodeId == media_id)).first()
        mapper = path_mappings.path_replace
    else:
        raise CleanupError('Media type must be movie or series')
    if row is None or not row.path:
        raise CleanupError('Indexed media file not found', 404)
    return {'video_path': _canonical_source(mapper(row.path)), 'title': row.title or Path(row.path).name,
            'indexed_path': row.path}


def indexed_subtitles(media_type: str, media_id: int) -> list[IndexedSubtitle]:
    table = TableMoviesSubtitles if media_type == 'movie' else TableEpisodesSubtitles
    identity = table.radarrId if media_type == 'movie' else table.sonarrEpisodeId
    mapper = path_mappings.path_replace_movie if media_type == 'movie' else path_mappings.path_replace
    rows = database.execute(select(table.path, table.language, table.forced, table.hi)
                            .where(identity == media_id, table.path.is_not(None))).all()
    subtitles: list[IndexedSubtitle] = [{'path': _canonical_source(mapper(row.path)), 'language': row.language,
                  'forced': bool(row.forced), 'hearing_impaired': bool(row.hi)} for row in rows]
    video = Path(media_metadata(media_type, media_id)['video_path'])
    known = {item['path'] for item in subtitles}
    for item in _sidecar_files(video):
        path = _canonical_source(str(item))
        if path not in known:
            subtitles.append(_sidecar_metadata(item, video))
            known.add(path)
    _assert_exclusive_owners(str(video), known)
    return sorted(subtitles, key=lambda row: (row['path'], row['language'], row['forced'], row['hearing_impaired']))


def _assert_exclusive_owners(video_path: str, paths: set[str]) -> None:
    if indexed_subtitle_owners(paths) - {os.path.normcase(video_path)}:
        raise CleanupError('Subtitle is also indexed for another video; refusing shared-file cleanup', 409)


def _video_paths() -> list[Path]:
    videos: list[Path] = []
    for table, mapper in ((TableMovies, path_mappings.path_replace_movie),
                          (TableEpisodes, path_mappings.path_replace)):
        rows = database.execute(select(table.path).where(table.path.is_not(None))).all()
        videos.extend(Path(mapper(row.path)) for row in rows if row.path)
    return videos


def _index_video_owners(videos: list[Path],
                        configuration: SubtitleDirectorySettings) -> dict[Path, dict[str, set[str]]]:
    owners: dict[Path, dict[str, set[str]]] = {}
    directories: dict[Path, Path] = {}
    for video in videos:
        if video.parent not in directories:
            directories[video.parent] = Path(os.path.normcase(os.path.realpath(video.parent)))
        parent = directories[video.parent]
        identity = os.path.normcase(str(parent / video.name))
        for directory in get_subtitle_output_directories(str(video), configuration):
            if directory not in directories:
                directories[directory] = Path(os.path.normcase(os.path.realpath(directory)))
            names = owners.setdefault(directories[directory], {})
            names.setdefault(video.stem.casefold(), set()).add(identity)
    return owners


def _candidate_owners(path: Path, names: dict[str, set[str]]) -> set[str]:
    stem = path.stem.casefold()
    owners: set[str] = set()
    while stem:
        owners.update(names.get(stem, ()))
        stem, separator, _ = stem.rpartition('.')
        if not separator:
            break
    return owners


def _scan_subtitle_locations(locations: set[Path]) -> tuple[list[Path], list[Path]]:
    from subliminal_patch.core import SUBTITLE_EXTENSIONS
    from subliminal.video import VIDEO_EXTENSIONS

    extensions = {*SUBTITLE_EXTENSIONS, '.idx', '.sup', '.scc', '.ttml', '.dfxp'}
    videos = []
    candidates = []
    for directory in locations:
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                videos.append(path)
            if path.suffix.lower() in extensions:
                candidates.append(path)
    return videos, candidates


def _sidecar_files(video: Path) -> list[Path]:
    configuration = get_subtitle_directory_settings()
    locations = {Path(os.path.normcase(os.path.realpath(path)))
                 for path in get_subtitle_output_directories(str(video), configuration)}
    videos, candidates = _scan_subtitle_locations(locations)
    owners = _index_video_owners([video, *_video_paths(), *videos], configuration)
    identity = os.path.normcase(str(video))
    found = []
    for path in candidates:
        matches = _candidate_owners(path, owners[path.parent])
        if identity not in matches:
            continue
        if len(matches) != 1:
            raise CleanupError(f'Subtitle has ambiguous video owners; move it to a unique subtitle location: {path}', 409)
        found.append(path)
    return found


def _sidecar_metadata(path: Path, video: Path) -> IndexedSubtitle:
    from babelfish import Language, Error

    tokens = path.stem[len(video.stem):].strip('.').lower().split('.')
    language = 'und'
    roles = tokens
    for index, token in enumerate(tokens):
        try:
            language = str(Language.fromietf(token))
            roles = tokens[:index] + tokens[index + 1:]
            break
        except (Error, ValueError):
            continue
    return {'path': _canonical_source(str(path)), 'language': language,
            'forced': 'forced' in roles, 'hearing_impaired': bool({'hi', 'sdh'}.intersection(roles))}


def download_scores(media_type: str, media_id: int, indexed_path: str) -> dict[str, float]:
    table = TableHistoryMovie if media_type == 'movie' else TableHistory
    identity = table.radarrId if media_type == 'movie' else table.sonarrEpisodeId
    mapper = path_mappings.path_replace_movie if media_type == 'movie' else path_mappings.path_replace
    rows = database.execute(select(table.subtitles_path, table.score, table.score_out_of)
                            .where(identity == media_id, table.video_path == indexed_path,
                                   table.action.in_((1, 2, 3)), table.score.is_not(None), table.score_out_of > 0)
                            .order_by(table.id)).all()
    return {os.path.realpath(mapper(row.subtitles_path)): round(row.score * 100 / row.score_out_of, 2)
            for row in rows if row.subtitles_path and 0 <= row.score <= row.score_out_of}


def create_operation(media_type: str, media_id: int, audio_stream_index: int | None) -> Operation:
    metadata = media_metadata(media_type, media_id)
    operation: Operation = {
        'job_id': None, 'indexed_subtitles': [], 'selected_keepers': [],
        'id': str(uuid4()), 'status': 'queued', 'media_type': media_type, 'media_id': media_id,
        'title': metadata['title'], 'video_path': metadata['video_path'],
        'audio_stream_index': audio_stream_index, 'preview_token': '', 'analysis': None,
        'groups': [], 'files': [], 'required_space_bytes': 0, 'backup_path': None,
        'result': None, 'error': None, 'created_at': datetime.now(timezone.utc).isoformat(),
    }
    database.add(TableSubtitleCleanupOperations(
        id=operation['id'], media_type=media_type, media_id=media_id,
        video_path=os.path.normcase(metadata['video_path']),
        status='queued', recovery_pending=False, payload=json.dumps(operation), created_at=datetime.now(timezone.utc),
    ))
    database.commit()
    return operation


def get_operation(operation_id: str) -> Operation:
    return get_operation_snapshot(operation_id).operation()


def get_operation_snapshot(operation_id: str) -> OperationSnapshot:
    row = database.execute(select(TableSubtitleCleanupOperations.id, TableSubtitleCleanupOperations.payload,
                                  TableSubtitleCleanupOperations.job_id, TableSubtitleCleanupOperations.recovery_pending)
                           .where(TableSubtitleCleanupOperations.id == operation_id)).first()
    if row is None:
        raise CleanupError('Cleanup operation not found', 404)
    return OperationSnapshot(row.id, row.payload, row.job_id, row.recovery_pending)


def get_operations(operation_ids: list[str]) -> dict[str, Operation | CleanupError]:
    operations = {}
    for offset in range(0, len(operation_ids), SUBTITLE_MAP_QUERY_BATCH_SIZE):
        rows = database.execute(select(TableSubtitleCleanupOperations.id, TableSubtitleCleanupOperations.payload,
                                       TableSubtitleCleanupOperations.job_id).where(
            TableSubtitleCleanupOperations.id.in_(operation_ids[offset:offset + SUBTITLE_MAP_QUERY_BATCH_SIZE]))).all()
        for row in rows:
            try:
                operation = json.loads(row.payload)
                operation['job_id'] = row.job_id
                operations[row.id] = operation
            except (TypeError, ValueError) as error:
                operations[row.id] = CleanupError(str(error), 409)
    return operations


def set_job_id(operation_id: str, job_id: int) -> Operation:
    database.execute(update(TableSubtitleCleanupOperations).where(TableSubtitleCleanupOperations.id == operation_id)
                     .values(job_id=job_id))
    database.commit()
    return get_operation(operation_id)


def list_operations(media_type: str, media_id: int) -> list[Operation]:
    rows = database.execute(select(TableSubtitleCleanupOperations.payload)
                            .where(TableSubtitleCleanupOperations.media_type == media_type,
                                   TableSubtitleCleanupOperations.media_id == media_id)
                            .order_by(TableSubtitleCleanupOperations.created_at.desc())).all()
    return [json.loads(row.payload) for row in rows]


def update_operation(operation: Operation, previous: tuple[str, ...] | None = None, *, reset_job: bool = False,
                     recovery_pending: bool | None = None) -> Operation:
    if reset_job:
        operation['job_id'] = None
    statement = _operation_update(operation, recovery_pending)
    if previous is not None:
        statement = statement.where(TableSubtitleCleanupOperations.status.in_(previous))
    if reset_job:
        statement = statement.values(job_id=None)
    if not _commit_operation(statement, operation['id']):
        raise CleanupError('Cleanup operation changed; reload its current state', 409)
    return operation


def _operation_update(operation: Operation, recovery_pending: bool | None) -> Update:
    statement = update(TableSubtitleCleanupOperations).where(TableSubtitleCleanupOperations.id == operation['id'])
    if recovery_pending is not None:
        statement = statement.values(recovery_pending=recovery_pending)
    return statement.values(status=operation['status'], payload=json.dumps(operation, allow_nan=False))


def _commit_operation(statement: Update, operation_id: str) -> bool:
    result = database.execute(statement.returning(TableSubtitleCleanupOperations.id)).scalar_one_or_none()
    if result != operation_id:
        database.rollback()
        return False
    database.commit()
    return True


def update_operation_snapshot(operation: Operation, snapshot: OperationSnapshot, *,
                              recovery_pending: bool | None = None) -> bool:
    statement = _operation_update(operation, recovery_pending).where(
        TableSubtitleCleanupOperations.id == snapshot.id,
        TableSubtitleCleanupOperations.payload == snapshot.payload,
        TableSubtitleCleanupOperations.job_id.is_not_distinct_from(snapshot.job_id),
        TableSubtitleCleanupOperations.recovery_pending == snapshot.recovery_pending,
    )
    return _commit_operation(statement, operation['id'])


def recovery_operations(video_path: str) -> list[Operation]:
    return [snapshot.operation() for snapshot in recovery_snapshots(video_path)]


def recovery_snapshots(video_path: str) -> list[OperationSnapshot]:
    rows = database.execute(select(TableSubtitleCleanupOperations.id, TableSubtitleCleanupOperations.payload,
                                   TableSubtitleCleanupOperations.job_id, TableSubtitleCleanupOperations.recovery_pending)
                            .where(TableSubtitleCleanupOperations.video_path == os.path.normcase(video_path),
                                   TableSubtitleCleanupOperations.recovery_pending.is_(True))
                            .order_by(TableSubtitleCleanupOperations.created_at)).all()
    return [OperationSnapshot(row.id, row.payload, row.job_id, row.recovery_pending) for row in rows]


def refresh_inventory(operation: Operation) -> None:
    from app.event_handler import event_stream
    path = operation['video_path']
    if operation['media_type'] == 'movie':
        from subtitles.indexer.movies import store_subtitles_movie
        database.execute(update(TableMovies).where(TableMovies.radarrId == operation['media_id'])
                         .values(file_size=os.path.getsize(path), ffprobe_cache=None))
        database.commit()
        store_subtitles_movie(operation['media_id'], use_cache=False)
        event_stream(type='movie', payload=operation['media_id'])
    else:
        from subtitles.indexer.series import store_subtitles
        database.execute(update(TableEpisodes).where(TableEpisodes.sonarrEpisodeId == operation['media_id'])
                         .values(file_size=os.path.getsize(path), ffprobe_cache=None))
        database.commit()
        store_subtitles(operation['media_id'], use_cache=False)
        event_stream(type='episode', payload=operation['media_id'])
    database.commit()
