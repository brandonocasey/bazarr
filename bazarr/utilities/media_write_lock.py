# coding=utf-8

import errno
import hashlib
import os
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import BinaryIO


class _MediaLock:
    def __init__(self) -> None:
        self.mutex = threading.RLock()
        self.depth = 0
        self.file: BinaryIO | None = None


_registry_mutex = threading.Lock()
_locks: weakref.WeakValueDictionary[str, _MediaLock] = weakref.WeakValueDictionary()
_guards: list[Callable[[str], None]] = []


def _canonical_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def register_media_write_guard(callback: Callable[[str], None]) -> None:
    """Register recovery to run under the media lock before an outermost writer starts."""
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


def _lock_file(file: BinaryIO, job_id: int | None) -> None:
    while True:
        _raise_if_canceled(job_id)
        try:
            _try_file_lock(file)
            return
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
        time.sleep(0.05)


@contextmanager
def _thread_lock(lock: _MediaLock, job_id: int | None) -> Iterator[None]:
    while not lock.mutex.acquire(timeout=0.05):
        _raise_if_canceled(job_id)
    try:
        _raise_if_canceled(job_id)
        yield
    finally:
        lock.mutex.release()

@contextmanager
def media_write_lock(video_path: str, *, job_id: int | None = None) -> Iterator[None]:
    """Serialize writers by canonical video path across threads and processes."""
    from app.get_args import args

    canonical = _canonical_path(video_path)
    with _registry_mutex:
        lock = _locks.get(canonical)
        if lock is None:
            lock = _MediaLock()
            _locks[canonical] = lock

    with _thread_lock(lock, job_id):
        if lock.depth:
            lock.depth += 1
            try:
                yield
            finally:
                lock.depth -= 1
            return

        directory = os.path.join(args.config_dir, 'media_write_locks')
        os.makedirs(directory, exist_ok=True)
        # Retain lock files; unlinking them splits waiting and new writers across inodes.
        name = hashlib.sha256(os.fsencode(canonical)).hexdigest() + '.lock'
        with open(os.path.join(directory, name), 'a+b') as file:
            _lock_file(file, job_id)
            lock.file = file
            lock.depth = 1
            try:
                with _registry_mutex:
                    guards = tuple(_guards)
                for guard in guards:
                    guard(canonical)
                _raise_if_canceled(job_id)
                yield
            finally:
                lock.depth = 0
                lock.file = None


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
    from app.database import (database, select, TableEpisodes, TableEpisodesSubtitles,
                              TableMovies, TableMoviesSubtitles)
    from utilities.path_mappings import path_mappings

    canonical_subtitle = _canonical_path(subtitle_path)
    owners = set()
    for media, subtitles, identifier, reverse, forward in (
        (TableEpisodes, TableEpisodesSubtitles, 'sonarrEpisodeId',
         path_mappings.path_replace_reverse, path_mappings.path_replace),
        (TableMovies, TableMoviesSubtitles, 'radarrId',
         path_mappings.path_replace_reverse_movie, path_mappings.path_replace_movie),
    ):
        candidates = (subtitle_path, reverse(subtitle_path), canonical_subtitle, reverse(canonical_subtitle))
        rows = database.execute(
            select(media.path).join(subtitles, getattr(media, identifier) == getattr(subtitles, identifier))
            .where(subtitles.path.in_(candidates))).all()
        owners.update(_canonical_path(forward(row.path)) for row in rows)
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
    from app.config import settings
    from utilities.helper import get_subtitle_destination_folder

    directory = os.path.dirname(video_path)
    destinations = {_canonical_path(directory)}
    custom = get_subtitle_destination_folder()
    if custom and settings.general.subfolder == 'absolute':
        destinations.add(_canonical_path(custom))
    elif custom and settings.general.subfolder == 'relative':
        destinations.add(_canonical_path(os.path.join(directory, custom)))
    return destinations
