# coding=utf-8

import errno
import hashlib
import logging
import os
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO
from uuid import uuid4


_GENERATION_BYTES = 16


@dataclass(frozen=True)
class MediaWriteState:
    """Snapshot for detecting selections that overlap another media writer."""
    generation: bytes = b''
    active: bool = False


class _MediaLock:
    def __init__(self) -> None:
        self.mutex = threading.RLock()
        self.depth = 0
        self.file: BinaryIO | None = None
        self.owner: int | None = None
        self.state = MediaWriteState()


_registry_mutex = threading.Lock()
_locks: weakref.WeakValueDictionary[str, _MediaLock] = weakref.WeakValueDictionary()
_guards: list[Callable[[str], bool | None]] = []


def _canonical_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _lock_path(canonical: str) -> str:
    from app.get_args import args

    name = hashlib.sha256(os.fsencode(canonical)).hexdigest() + '.lock'
    return os.path.join(args.config_dir, 'media_write_locks', name)


def _read_write_state(file: BinaryIO) -> MediaWriteState:
    # Byte zero remains reserved for the Windows advisory lock.
    file.seek(1)
    encoded = file.read(_GENERATION_BYTES + 2)
    if not encoded:
        return MediaWriteState()
    if len(encoded) != _GENERATION_BYTES + 1 or encoded[-1] not in (0, 1):
        raise OSError('Invalid media writer generation; refusing an ambiguous subtitle selection')
    return MediaWriteState(encoded[:-1], bool(encoded[-1]))


def media_write_state(video_path: str) -> MediaWriteState:
    """Callers MUST capture this state before reading selected subtitle fields."""
    canonical = _canonical_path(video_path)
    with _registry_mutex:
        lock = _locks.get(canonical)
        if lock is not None and lock.owner == threading.get_ident():
            return lock.state
    try:
        with open(_lock_path(canonical), 'rb') as file:
            return _read_write_state(file)
    except FileNotFoundError:
        return MediaWriteState()


def _record_write_state(file: BinaryIO, *, active: bool) -> MediaWriteState:
    state = MediaWriteState(uuid4().bytes, active)
    file.seek(1)
    file.write(state.generation + bytes((active,)))
    file.truncate(_GENERATION_BYTES + 2)
    file.flush()
    os.fsync(file.fileno())
    return state


def _finish_write_state(file: BinaryIO, failed: bool) -> None:
    try:
        _record_write_state(file, active=False)
    except OSError:
        if not failed:
            raise
        logging.exception('BAZARR could not finish media writer generation after the writer failed')


def register_media_write_guard(callback: Callable[[str], bool | None]) -> None:
    """Recovery callbacks MUST return True when selected subtitles can change."""
    with _registry_mutex:
        if callback not in _guards:
            _guards.append(callback)


def _raise_if_canceled(job_id: int | None) -> None:
    if job_id is None:
        return
    from app.jobs_queue import jobs_queue, JobCanceled
    job = jobs_queue.get_job(job_id)
    if job is not None and job.cancel_event.is_set():
        raise JobCanceled('Job canceled while waiting for a media writer')


def _try_file_lock(file: BinaryIO) -> None:
    if sys.platform == 'win32':
        import msvcrt
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _lock_file(file: BinaryIO, job_id: int | None, *, blocking: bool = True) -> None:
    while True:
        _raise_if_canceled(job_id)
        try:
            _try_file_lock(file)
            return
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            if not blocking:
                raise BlockingIOError('Cannot acquire lock: active media writer') from error
        time.sleep(0.05)


@contextmanager
def _thread_lock(lock: _MediaLock, job_id: int | None, *, blocking: bool = True) -> Iterator[None]:
    _raise_if_canceled(job_id)
    if not blocking and not lock.mutex.acquire(blocking=False):
        raise BlockingIOError('Cannot acquire lock: active media writer')
    while blocking and not lock.mutex.acquire(timeout=0.05):
        _raise_if_canceled(job_id)
    try:
        _raise_if_canceled(job_id)
        yield
    finally:
        lock.mutex.release()

@contextmanager
def media_write_lock(video_path: str, *, job_id: int | None = None,
                     blocking: bool = True) -> Iterator[MediaWriteState]:
    """Serialize writers; nonblocking acquisition refuses live owners before changing state."""
    canonical = _canonical_path(video_path)
    with _registry_mutex:
        lock = _locks.get(canonical)
        if lock is None:
            lock = _MediaLock()
            _locks[canonical] = lock

    with _thread_lock(lock, job_id, blocking=blocking):
        if lock.depth:
            lock.depth += 1
            try:
                yield lock.state
            finally:
                lock.depth -= 1
            return

        path = _lock_path(canonical)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Retain lock files; unlinking them splits waiting and new writers across inodes.
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0), 0o600)
        with os.fdopen(descriptor, 'r+b') as file:
            _lock_file(file, job_id, blocking=blocking)
            previous = _read_write_state(file)
            current = _record_write_state(file, active=True)
            lock.file = file
            lock.depth = 1
            lock.owner = threading.get_ident()
            lock.state = MediaWriteState(current.generation)
            failed = True
            try:
                recovered = _run_write_guards(canonical, job_id)
                yield lock.state if recovered else previous
                failed = False
            finally:
                lock.depth = 0
                lock.file = None
                lock.owner = None
                _finish_write_state(file, failed)


def _run_write_guards(canonical: str, job_id: int | None) -> bool:
    with _registry_mutex:
        guards = tuple(_guards)
    recovered = False
    for guard in guards:
        if guard(canonical) is True:
            recovered = True
    _raise_if_canceled(job_id)
    return recovered


def _after_fork() -> None:
    global _registry_mutex, _locks
    for lock in tuple(_locks.values()):
        if lock.file is not None:
            lock.file.close()
    _registry_mutex = threading.Lock()
    _locks = weakref.WeakValueDictionary()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


def subtitle_media_path(subtitle_path: str, video_path: str | None = None, *,
                        allow_unindexed: bool = False) -> str:
    """Resolve indexed ownership or validate fresh sidecars against the video and output location."""
    from utilities.subtitle_ownership import indexed_subtitle_owners

    owners = indexed_subtitle_owners((subtitle_path,))
    if owners:
        if len(owners) != 1:
            raise OSError('Subtitle is indexed for multiple media files')
        owner = owners.pop()
        if video_path is not None and owner != _canonical_path(video_path):
            raise OSError('Subtitle does not belong to the supplied media file')
        return owner

    if not allow_unindexed or video_path is None:
        raise OSError('Subtitle file is not indexed for this media file')
    return _fresh_sidecar_owner(subtitle_path, video_path)


def _fresh_sidecar_owner(subtitle_path: str, video_path: str) -> str:
    from app.database import database, select, TableEpisodes, TableMovies
    from utilities.path_mappings import path_mappings
    from subliminal_patch.core import SUBTITLE_EXTENSIONS

    canonical_subtitle = _canonical_path(subtitle_path)
    canonical_video = _canonical_path(video_path)
    stem = os.path.splitext(os.path.basename(os.path.normcase(video_path)))[0]
    if (os.path.dirname(canonical_subtitle) not in _output_locations(video_path)
            or not os.path.basename(canonical_subtitle).startswith(stem + '.')
            or os.path.splitext(canonical_subtitle)[1] not in SUBTITLE_EXTENSIONS):
        raise OSError('Subtitle file is not indexed for this media file')
    for media, reverse, forward in (
        (TableEpisodes, path_mappings.path_replace_reverse, path_mappings.path_replace),
        (TableMovies, path_mappings.path_replace_reverse_movie, path_mappings.path_replace_movie),
    ):
        row = database.execute(select(media.path).where(media.path.in_(
            (video_path, reverse(video_path), canonical_video, reverse(canonical_video))))).first()
        if row is not None and _canonical_path(forward(row.path)) == canonical_video:
            return canonical_video
    raise OSError('Subtitle file is not indexed for this media file')


def _output_locations(video_path: str) -> set[str]:
    from utilities.helper import get_subtitle_output_directories

    return {_canonical_path(str(path)) for path in get_subtitle_output_directories(video_path)}
