# coding=utf-8

import hashlib
import json
import math
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from collections.abc import Callable

from . import media, store
from .models import AnalysisOptions, ExternalSubtitle, JSONValue, MediaAnalysis, PreparedFile
from .records import FileRecord, IndexedSubtitle, MediaMetadata, Operation, TrackGroup, analysis_record
from .transactions import FileIdentity, fingerprint, verify_identity


def _companion(path: Path, directories: dict[Path, dict[str, list[Path]]]) -> Path | None:
    if path.suffix.lower() not in ('.idx', '.sub'):
        return None
    companion = path.with_suffix('.sub' if path.suffix.lower() == '.idx' else '.idx')
    if companion.exists():
        return companion
    if path.parent not in directories:
        names: dict[str, list[Path]] = {}
        for item in path.parent.iterdir():
            names.setdefault(item.name.casefold(), []).append(item)
        directories[path.parent] = names
    matches = directories[path.parent].get(companion.name.casefold(), [])
    if len(matches) > 1:
        raise store.CleanupError(f'Ambiguous subtitle companions for {path}', 409)
    return next(iter(matches), None)


def input_paths(video_path: str, subtitles: list[IndexedSubtitle]) -> list[str]:
    paths = {video_path}
    directories = {}
    for subtitle in subtitles:
        path = Path(subtitle['path'])
        paths.add(str(path))
        companion = _companion(path, directories)
        if companion is not None:
            paths.add(str(companion))
    return sorted(paths)


def group_tracks(analysis: MediaAnalysis) -> list[TrackGroup]:
    groups: dict[str, TrackGroup] = {}
    for track in analysis.tracks:
        key = f'{track.language}:{int(track.forced)}'
        group = groups.setdefault(key, {'key': key, 'language': track.language, 'forced': track.forced,
                                        'track_keys': [], 'recommended_keeper': None, 'requires_selection': True})
        group['track_keys'].append(track.key)
    by_key = {track.key: track for track in analysis.tracks}
    for group in groups.values():
        keys = group['track_keys']
        scores = [by_key[key].score for key in keys]
        ranked = [score for score in scores if score is not None and math.isfinite(score)]
        if len(keys) == 1:
            group['recommended_keeper'] = keys[0]
        elif len(ranked) == len(keys):
            maximum = max(ranked)
            winners = [key for key in keys if by_key[key].score == maximum]
            if len(winners) == 1:
                group['recommended_keeper'] = winners[0]
        group['requires_selection'] = group['recommended_keeper'] is None
    return sorted(groups.values(), key=lambda group: group['key'])


def _analysis_payload(analysis: MediaAnalysis, download_scores: dict[str, float]) -> dict[str, JSONValue]:
    payload = analysis.to_dict()
    tracks: list[JSONValue] = []
    for track in analysis.tracks:
        record: dict[str, JSONValue] = asdict(track)
        record['download_score'] = download_scores.get(track.path) if track.source == 'external' else None
        tracks.append(record)
    payload['tracks'] = tracks
    return payload


@dataclass
class AnalysisInputs:
    metadata: MediaMetadata
    subtitles: list[IndexedSubtitle]
    identities: list[FileIdentity]


def capture_inputs(operation: Operation) -> AnalysisInputs:
    """Callers MUST capture writer state before collecting analysis inputs."""
    metadata = store.media_metadata(operation['media_type'], operation['media_id'])
    if metadata['video_path'] != operation['video_path']:
        raise store.CleanupError('Indexed video changed before analysis; create a fresh preview', 409)
    subtitles = store.indexed_subtitles(operation['media_type'], operation['media_id'])
    paths = input_paths(metadata['video_path'], subtitles)
    identities = [fingerprint(path) for path in paths]
    return AnalysisInputs(metadata, subtitles, identities)


def analyze_operation(operation: Operation, workspace: Path, cancelled: Callable[[], bool],
                      inputs: AnalysisInputs) -> Operation:
    metadata, subtitles, identities = inputs.metadata, inputs.subtitles, inputs.identities
    paths = [identity.path for identity in identities]
    analysis = media.analyze_media(metadata['video_path'], [ExternalSubtitle(**item) for item in subtitles],
                                   str(workspace), AnalysisOptions(operation['audio_stream_index'], cancelled))
    if set(media.source_paths(analysis)) != set(paths):
        raise store.CleanupError('Subtitle inputs changed or contain an unpreviewed companion; scan and preview again', 409)
    for identity in identities:
        verify_identity(identity)
    if store.indexed_subtitles(operation['media_type'], operation['media_id']) != subtitles:
        raise store.CleanupError('Subtitle inventory changed during analysis; preview again', 409)
    download_scores = store.download_scores(operation['media_type'], operation['media_id'], metadata['indexed_path'])
    payload = _analysis_payload(analysis, download_scores)
    operation.update(analysis=payload, groups=group_tracks(analysis), files=[FileRecord(**asdict(item)) for item in identities],
                     indexed_subtitles=subtitles,
                     required_space_bytes=6 * sum(item.size for item in identities) + int(analysis.duration * 32000), status='ready', error=None)
    signed = {key: operation[key] for key in ('analysis', 'groups', 'files', 'indexed_subtitles', 'media_type', 'media_id')}
    operation['preview_token'] = hashlib.sha256(json.dumps(signed, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return operation


def _validate_preview_inventory(operation: Operation) -> None:
    metadata = store.media_metadata(operation['media_type'], operation['media_id'])
    if metadata['video_path'] != operation['video_path']:
        raise store.CleanupError('Indexed video changed since the cleanup preview', 409)
    if store.indexed_subtitles(operation['media_type'], operation['media_id']) != operation['indexed_subtitles']:
        raise store.CleanupError('Subtitle inventory changed since the cleanup preview', 409)


def validate_analysis_preview(operation: Operation) -> None:
    """Callers MUST hold the media lock and verify the captured writer generation."""
    _validate_preview_inventory(operation)
    for item in operation['files']:
        info = Path(item['path']).lstat()
        actual = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                  stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
        expected = (item['device'], item['inode'], item['size'], item['modified_ns'], item['changed_ns'],
                    item['mode'], item['uid'], item['gid'])
        if not stat.S_ISREG(info.st_mode) or actual != expected:
            raise store.CleanupError(f'File changed since the cleanup preview: {item["path"]}', 409)


def validate_preview(operation: Operation) -> None:
    _validate_preview_inventory(operation)
    for item in operation['files']:
        verify_identity(FileIdentity(**item))


def validate_keepers(operation: Operation, keepers: dict[str, str]) -> list[str]:
    groups = operation['groups']
    if set(keepers) != {group['key'] for group in groups}:
        raise store.CleanupError('Choose exactly one keeper for every language and forced-subtitle group')
    tracks = {track.key: track for track in MediaAnalysis.from_dict(analysis_record(operation)).tracks}
    for group in groups:
        selected = keepers[group['key']]
        if selected not in group['track_keys']:
            raise store.CleanupError('Keeper does not belong to this preview group')
        scores = [tracks[key].score for key in group['track_keys']]
        ranked = [score for score in scores if score is not None]
        if len(ranked) == len(scores) and tracks[selected].score != max(ranked):
            raise store.CleanupError('Keep the highest timing score; lower-scoring tracks cannot be selected')
    return list(keepers.values())


@dataclass
class _PhysicalUnit:
    embedded: bool
    paths: set[str] = field(default_factory=set)
    kept: bool = False
    removed: bool = False


def physical_changes(analysis: MediaAnalysis, keep_keys: list[str]) -> dict[str, str]:
    keep = set(keep_keys)
    units = {}
    for track in analysis.tracks:
        paths = {track.path, *track.companion_paths}
        primary = analysis.video_path if track.source == 'embedded' else track.path
        unit = units.setdefault(primary, _PhysicalUnit(embedded=track.source == 'embedded'))
        unit.paths.update(paths)
        unit.kept |= track.key in keep
        unit.removed |= track.key not in keep
    changes = {}
    for unit in units.values():
        if unit.removed:
            action = 'rewrite' if unit.embedded or unit.kept else 'remove'
            changes.update({path: action for path in unit.paths})
    return changes


def validate_outputs(analysis: MediaAnalysis, keep_keys: list[str], outputs: list[PreparedFile], workspace: Path) -> None:
    expected = physical_changes(analysis, keep_keys)
    actual = {item.original_path: 'rewrite' if item.staged_path is not None else 'remove' for item in outputs}
    if len(actual) != len(outputs) or actual != expected:
        raise store.CleanupError('Prepared outputs differ from the confirmed physical cleanup plan', 409)
    for item in outputs:
        if item.staged_path is not None and not Path(item.staged_path).resolve().is_relative_to(workspace.resolve()):
            raise store.CleanupError('Cleanup output escaped its operation staging directory', 409)
