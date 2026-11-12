# coding=utf-8

import hashlib
import json
import logging
import os
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .models import PreparedFile
from .records import FileRecord, Journal, JournalEntry


@dataclass(frozen=True)
class FileIdentity:
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


@dataclass(frozen=True)
class _CopyContext:
    content: FileIdentity
    metadata: FileIdentity
    created: Callable[[os.stat_result], None] | None = None


def fingerprint(path: str) -> FileIdentity:
    source = Path(path)
    if not source.is_absolute() or source.is_symlink():
        raise ValueError(f'Cleanup requires an absolute, non-symlink file: {path}')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(source, flags), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f'Cleanup requires a regular file: {path}')
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise ValueError(f'File changed while reading: {path}')
    return FileIdentity(path=str(source), size=after.st_size, sha256=digest.hexdigest(), device=after.st_dev,
                        inode=after.st_ino, modified_ns=after.st_mtime_ns, changed_ns=after.st_ctime_ns,
                        mode=stat.S_IMODE(after.st_mode), uid=after.st_uid, gid=after.st_gid)


def verify_identity(expected: FileIdentity) -> None:
    if fingerprint(expected.path) != expected:
        raise ValueError(f'File changed since the cleanup preview: {expected.path}')


def _sync_directory(directory: Path) -> None:
    if os.name != 'nt':
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _ensure_directory(directory: Path) -> None:
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700, exist_ok=True)
        _sync_directory(item.parent)


def _write_journal(directory: Path, journal: Journal) -> None:
    temporary = directory / 'journal.next'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(journal, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / 'journal.json')
    _sync_directory(directory)


def read_journal(directory: Path) -> Journal | None:
    path = directory / 'journal.json'
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError(f'Cleanup journal cannot be a symlink: {path}')
    with path.open() as stream:
        journal: Journal = json.load(stream)
    _validate_journal_header(journal, path)
    return journal


def _validate_journal_header(journal: Journal, path: Path) -> None:
    if not isinstance(journal, dict) or journal.get('version') != 1 or not isinstance(journal.get('entries'), list):
        raise ValueError(f'Invalid cleanup journal: {path}')


def _verified_copy(source: str, target: Path, context: _CopyContext) -> None:
    if target.exists() or target.is_symlink():
        raise ValueError(f'Cleanup output already exists: {target}')
    _ensure_directory(target.parent)
    created_inode = None
    try:
        with open(source, 'rb') as reader, target.open('xb') as writer:
            created_inode = os.fstat(writer.fileno()).st_ino
            if context.created is not None:
                # Persist ownership before copying bytes so recovery can discard interrupted output.
                os.fsync(writer.fileno())
                _sync_directory(target.parent)
                context.created(os.fstat(writer.fileno()))
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        try:
            shutil.copystat(source, target, follow_symlinks=False)
        except PermissionError as error:
            raise _metadata_permission_error(context.metadata, error) from error
        _copy_identity_metadata(target, context.metadata)
        actual = fingerprint(str(target))
        if (actual.size, actual.sha256) != (context.content.size, context.content.sha256):
            raise ValueError(f'Cleanup copy verification failed: {source}')
    except BaseException:
        try:
            if created_inode is not None and target.lstat().st_ino == created_inode:
                target.unlink()
        except OSError:
            logging.exception('Cannot remove incomplete cleanup copy %s', target)
        raise
    _sync_directory(target.parent)


def _copy_identity_metadata(target: Path, expected: FileIdentity) -> None:
    current = target.stat()
    try:
        if os.name != 'nt' and (current.st_uid, current.st_gid) != (expected.uid, expected.gid):
            os.chown(target, expected.uid, expected.gid)
        os.chmod(target, expected.mode)
    except PermissionError as error:
        raise _metadata_permission_error(expected, error) from error
    with target.open('rb') as stream:
        os.fsync(stream.fileno())


def _metadata_permission_error(expected: FileIdentity, error: PermissionError) -> PermissionError:
    return PermissionError(
        f'Cannot preserve metadata for original file {expected.path}: '
        f'required uid {expected.uid}, gid {expected.gid}, mode {expected.mode:#o}. '
        f'Grant the Bazarr service account permission to preserve this owner, group and mode, '
        f'or have an operator with that permission run the operation. Filesystem refusal: {error}'
    )


def _same_content(path: str, expected: FileRecord | None) -> bool:
    if expected is None:
        return not os.path.lexists(path)
    try:
        actual = fingerprint(path)
    except FileNotFoundError:
        return False
    return actual.sha256 == expected['sha256'] and actual.size == expected['size']


def _space_required(entries: list[JournalEntry], directory: Path) -> None:
    required: dict[int, tuple[Path, int]] = {}
    for path, count in [(directory, sum(entry['original']['size'] for entry in entries))] + [
        (Path(entry['path']).parent, entry['replacement']['size'])
        for entry in entries if entry['replacement'] is not None
    ]:
        device = path.stat().st_dev
        old = required.get(device, (path, 0))
        required[device] = (path, old[1] + count)
    for path, count in required.values():
        if shutil.disk_usage(path).free < count:
            raise OSError(f'Insufficient space for cleanup backups and replacements on {path}: need {count} bytes')


def _prepare_entries(directory: Path, changes: list[PreparedFile], identities: list[FileIdentity]) -> list[JournalEntry]:
    known = {identity.path: identity for identity in identities}
    entries: list[JournalEntry] = []
    for index, change in enumerate(changes):
        original = known.get(change.original_path)
        if original is None:
            raise ValueError(f'Cleanup output was not in the confirmed preview: {change.original_path}')
        replacement = fingerprint(change.staged_path) if change.staged_path is not None else None
        relative = Path(*Path(original.path).parts[1:])
        entries.append({
            'path': original.path, 'original': FileRecord(**asdict(original)),
            'backup': str(directory / 'originals' / relative), 'backup_owner': None,
            'temporary': str(Path(original.path).with_name(f'.bazarr-cleanup-{directory.name}-{index}.stage')),
            'replacement': FileRecord(**asdict(replacement)) if replacement else None,
            'source': change.staged_path, 'applied': False, 'after': None,
            'temporary_owner': None, 'temporary_ready': False, 'temporary_content': None,
        })
    if len({entry['path'] for entry in entries}) != len(entries):
        raise ValueError('Cleanup proposed more than one change to the same file')
    return entries


def commit_changes(directory: Path, changes: list[PreparedFile], identities: list[FileIdentity], validate: Callable[[], None]) -> Journal:
    if directory.exists():
        raise ValueError(f'Cleanup operation already has a backup journal: {directory}')
    _ensure_directory(directory)
    entries = _prepare_entries(directory, changes, identities)
    journal: Journal = {'version': 1, 'phase': 'preparing', 'entries': entries}
    _space_required(entries, directory)
    _write_journal(directory, journal)
    try:
        _stage_entries(directory, journal)
        validate()
        journal['phase'] = 'committing'
        _write_journal(directory, journal)
        for entry in entries:
            verify_identity(FileIdentity(**entry['original']))
            _replace_entry(entry)
            entry['applied'] = True
            entry['after'] = FileRecord(**asdict(fingerprint(entry['path']))) if entry['replacement'] else None
            _write_journal(directory, journal)
        journal['phase'] = 'complete'
        _write_journal(directory, journal)
    except BaseException as error:
        try:
            recover_transaction(directory)
        except Exception as recovery_error:
            raise RuntimeError(f'{error}; cleanup recovery requires attention: {recovery_error}') from error
        raise
    return journal


def _stage_entries(directory: Path, journal: Journal) -> None:
    for entry in journal['entries']:
        original = FileIdentity(**entry['original'])
        verify_identity(original)
        def remember_backup_owner(info: os.stat_result) -> None:
            entry['backup_owner'] = {'device': info.st_dev, 'inode': info.st_ino}
            _write_journal(directory, journal)

        _verified_copy(entry['path'], Path(entry['backup']), _CopyContext(original, original, remember_backup_owner))
        if entry['replacement']:
            _copy_staged(entry, journal, directory)
        _write_journal(directory, journal)


def _copy_staged(entry: JournalEntry, journal: Journal, directory: Path) -> None:
    restoring = journal['phase'] in ('restoring', 'rolling_back')
    expected = entry['original'] if restoring else entry['replacement']
    source = entry['backup'] if restoring else entry['source']
    if expected is None or source is None:
        raise ValueError('Cleanup replacement has no staged source or identity')
    if restoring:
        entry['temporary'] = str(Path(entry['path']).with_name(f'.bazarr-cleanup-{uuid4()}.stage'))
    entry.update(temporary_owner=None, temporary_ready=False, temporary_content=expected)
    _write_journal(directory, journal)

    def remember_owner(info: os.stat_result) -> None:
        entry['temporary_owner'] = {'device': info.st_dev, 'inode': info.st_ino}
        _write_journal(directory, journal)

    context = _CopyContext(FileIdentity(**expected), FileIdentity(**entry['original']), remember_owner)
    _verified_copy(source, Path(entry['temporary']), context)
    entry['temporary_ready'] = True
    _write_journal(directory, journal)


def _replace_entry(entry: JournalEntry) -> None:
    if entry['replacement']:
        os.replace(entry['temporary'], entry['path'])
    else:
        os.unlink(entry['path'])
    _sync_directory(Path(entry['path']).parent)


def _remove_temporaries(journal: Journal) -> None:
    for entry in journal['entries']:
        temporary = Path(entry['temporary'])
        owner = entry['temporary_owner']
        if owner is not None and os.path.lexists(temporary):
            info = temporary.lstat()
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (owner['device'], owner['inode']):
                raise ValueError(f'Cleanup temporary file was replaced: {temporary}')
            if entry['temporary_ready'] and not _same_content(str(temporary), entry['temporary_content']):
                raise ValueError(f'Cleanup temporary file changed: {temporary}')
            temporary.unlink()
            _sync_directory(temporary.parent)


def _restore_original(entry: JournalEntry, journal: Journal, directory: Path) -> None:
    if _same_content(entry['path'], entry['original']):
        return
    _copy_staged(entry, journal, directory)
    os.replace(entry['temporary'], entry['path'])
    _sync_directory(Path(entry['path']).parent)


def _validate_restore_entries(journal: Journal, completed: bool) -> None:
    for entry in journal['entries']:
        if not _same_content(entry['backup'], entry['original']):
            raise ValueError(f'Cleanup backup is missing or changed: {entry["backup"]}')
        expected = entry['after'] if completed else entry['replacement']
        if completed and expected is not None:
            verify_identity(FileIdentity(**expected))
        elif not _same_content(entry['path'], expected):
            if completed or not _same_content(entry['path'], entry['original']):
                raise ValueError(f'Refusing to overwrite later changes: {entry["path"]}')


def restore_transaction(directory: Path) -> Journal:
    journal = read_journal(directory)
    if journal is None or journal['phase'] != 'complete':
        raise ValueError('Only completed cleanup operations can be restored')
    _validate_restore_entries(journal, completed=True)
    _remove_temporaries(journal)
    journal['phase'] = 'restoring'
    _write_journal(directory, journal)
    return recover_transaction(directory)


def _originals_match(journal: Journal) -> bool:
    for entry in journal['entries']:
        original = entry['original']
        actual = fingerprint(entry['path'])
        if (actual.size, actual.sha256, actual.mode, actual.uid, actual.gid, actual.modified_ns) != (
            original['size'], original['sha256'], original['mode'], original['uid'], original['gid'], original['modified_ns']
        ):
            return False
    return True


def _discard_backup(entry: JournalEntry) -> None:
    owner = entry.get('backup_owner')
    if owner is None:
        return
    backup = Path(entry['backup'])
    try:
        actual = fingerprint(str(backup))
        if (actual.device, actual.inode, actual.size, actual.sha256) != (
            owner['device'], owner['inode'], entry['original']['size'], entry['original']['sha256']
        ):
            return
        backup.unlink()
        _sync_directory(backup.parent)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        logging.exception('Cannot discard rolled-back cleanup backup %s', backup)


def discard_rollback_backups(directory: Path) -> None:
    """Remove captured backup copies only after durable rollback and verification of all originals."""
    try:
        journal = read_journal(directory)
        if journal is None or journal['phase'] != 'rolled_back':
            return
        if not _originals_match(journal):
            return
        for entry in journal['entries']:
            _discard_backup(entry)
    except (OSError, ValueError):
        logging.exception('Cannot verify originals for rolled-back cleanup %s', directory)


def recover_transaction(directory: Path) -> Journal:
    journal = read_journal(directory)
    if journal is None:
        raise ValueError(f'Cleanup journal is missing: {directory}')
    phase = journal['phase']
    if phase in ('complete', 'restored', 'rolled_back'):
        if phase == 'rolled_back':
            discard_rollback_backups(directory)
        return journal
    if phase == 'preparing':
        _remove_temporaries(journal)
        journal['phase'] = 'rolled_back'
        _write_journal(directory, journal)
        discard_rollback_backups(directory)
        return journal
    if phase not in ('committing', 'rolling_back', 'restoring'):
        raise ValueError(f'Unknown cleanup recovery phase: {phase}')
    _validate_restore_entries(journal, completed=False)
    _remove_temporaries(journal)
    if phase != 'restoring':
        journal['phase'] = 'rolling_back'
        _write_journal(directory, journal)
    for entry in reversed(journal['entries']):
        _restore_original(entry, journal, directory)
        _write_journal(directory, journal)
    journal['phase'] = 'restored' if phase == 'restoring' else 'rolled_back'
    _write_journal(directory, journal)
    if journal['phase'] == 'rolled_back':
        discard_rollback_backups(directory)
    return journal
