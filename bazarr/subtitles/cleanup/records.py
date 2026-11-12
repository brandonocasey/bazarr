from typing import TypedDict

from .models import JSONValue


class FileRecord(TypedDict):
    path: str
    size: int
    sha256: str
    device: int
    inode: int
    modified_ns: int
    changed_ns: int
    mode: int
    uid: int
    gid: int


class JournalEntry(TypedDict):
    path: str
    original: FileRecord
    backup: str
    temporary: str
    replacement: FileRecord | None
    source: str | None
    applied: bool
    after: FileRecord | None
    temporary_created: bool


class Journal(TypedDict):
    version: int
    phase: str
    entries: list[JournalEntry]


class IndexedSubtitle(TypedDict):
    path: str
    language: str
    forced: bool
    hearing_impaired: bool


class MediaMetadata(TypedDict):
    video_path: str
    title: str
    indexed_path: str


class TrackGroup(TypedDict):
    key: str
    language: str
    forced: bool
    track_keys: list[str]
    recommended_keeper: str | None
    requires_selection: bool


class CleanupResult(TypedDict):
    removed_track_keys: list[str]
    retained_track_keys: list[str]
    changed_files: list[str]
    restored_files: list[str]


class OperationExtras(TypedDict):
    job_id: int | None
    indexed_subtitles: list[IndexedSubtitle]
    selected_keepers: list[str]


class Operation(OperationExtras):
    id: str
    status: str
    media_type: str
    media_id: int
    title: str
    video_path: str
    audio_stream_index: int | None
    preview_token: str
    analysis: dict[str, JSONValue] | None
    groups: list[TrackGroup]
    files: list[FileRecord]
    required_space_bytes: int
    backup_path: str | None
    result: CleanupResult | None
    error: str | None
    created_at: str


def analysis_record(operation: Operation) -> dict[str, JSONValue]:
    value = operation['analysis']
    if value is None:
        raise ValueError('Cleanup operation has no completed analysis')
    return value
