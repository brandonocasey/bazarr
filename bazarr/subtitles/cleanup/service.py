# coding=utf-8

import hmac
import logging
import shutil
import stat
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError
from contextlib import contextmanager, ExitStack
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict
from uuid import UUID

from app.get_args import args
from app.jobs_queue import jobs_queue, Job, JobCanceled
from utilities.media_write_lock import MediaWriteState, media_write_lock, media_write_state, register_media_write_guard

from . import media, planner, store, transactions
from .models import MediaAnalysis
from .records import Journal, Operation, analysis_record


def _directory(operation: Operation) -> Path:
    if str(UUID(operation['id'])) != operation['id']:
        raise store.CleanupError('Invalid cleanup operation identifier', 409)
    return Path(args.config_dir) / 'backup' / 'subtitle-cleanup' / operation['id']


def _workspace(operation: Operation) -> Path:
    path = Path(args.config_dir) / 'cache' / 'subtitle-cleanup' / operation['id']
    if path.is_symlink():
        raise store.CleanupError('Cleanup staging directory cannot be a symlink', 409)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _cancelled(job_id: int | None) -> bool:
    job = jobs_queue.get_job(job_id)
    return job is not None and job.cancel_event.is_set()


def _reject_enqueue(operation: Operation, function: str, message: str) -> None:
    previous = operation['status']
    status = {'run_analysis': 'failed', 'run_apply': 'ready', 'run_restore': 'complete'}[function]
    operation.update(status=status, error=message)
    store.update_operation(operation, previous=(previous,), recovery_pending=False)


def _enqueue(operation: Operation, function: str) -> Operation:
    try:
        job_id = jobs_queue.feed_jobs_pending_queue(
            job_name=f'Subtitle cleanup: {operation["title"]}', module=__name__, func=function,
            kwargs={'operation_id': operation['id']}, is_progress=True, is_cancellable=True,
        )
    except Exception as error:
        _reject_enqueue(operation, function, f'Cannot queue cleanup: {error}')
        raise
    if job_id is False:
        _reject_enqueue(operation, function, 'This cleanup job is already queued; reload and retry after it finishes')
        raise store.CleanupError('This cleanup job is already queued', 409)
    return store.set_job_id(operation['id'], job_id)


def preview(media_type: str, media_id: int, audio_stream_index: int | None = None) -> Operation:
    operation = store.create_operation(media_type, media_id, audio_stream_index)
    operation['backup_path'] = str(_directory(operation))
    store.update_operation(operation)
    return _enqueue(operation, 'run_analysis')


def get_operation(operation_id: str) -> Operation:
    operation = store.get_operation(operation_id)
    is_active = partial(_snapshot_job_is_active, jobs=jobs_queue.snapshot_jobs())
    return _revalidate_inactive_operation(operation, is_active)


class OperationStatus(TypedDict):
    id: str
    operation: Operation | None
    error: str | None


def get_operations(operation_ids: list[str]) -> list[OperationStatus]:
    identifiers = list(dict.fromkeys(operation_ids))
    operations = store.get_operations(identifiers)
    jobs = jobs_queue.snapshot_jobs()
    is_active = partial(_snapshot_job_is_active, jobs=jobs)
    return [_operation_status(identifier, operations.get(identifier), is_active) for identifier in identifiers]


def _operation_status(identifier: str, operation: Operation | store.CleanupError | None,
                      is_active: Callable[[Operation], bool]) -> OperationStatus:
    try:
        if isinstance(operation, store.CleanupError):
            raise operation
        if operation is None:
            raise store.CleanupError('Cleanup operation not found', 404)
        operation = _revalidate_inactive_operation(operation, is_active)
        return {'id': identifier, 'operation': operation, 'error': None}
    except Exception as error:
        return {'id': identifier, 'operation': None, 'error': str(error)}


def _snapshot_job_is_active(operation: Operation, jobs: dict[int, Job]) -> bool:
    job_id = operation['job_id']
    if job_id is None:
        return True
    job = jobs.get(job_id)
    return job is not None and job.module == __name__ and job.kwargs.get('operation_id') == operation['id'] \
        and job.status in ('pending', 'running')


def _revalidate_inactive_operation(operation: Operation, is_active: Callable[[Operation], bool]) -> Operation:
    pending = ('queued', 'analyzing', 'applying', 'restoring')
    if operation['status'] not in pending or operation['job_id'] is None:
        return operation
    if is_active(operation):
        return operation
    with media_write_lock(operation['video_path']):
        current = store.get_operation(operation['id'])
        unchanged = current['status'] == operation['status'] and current['job_id'] == operation['job_id']
        if unchanged and not is_active(current):
            current['status'] = 'complete' if current['status'] == 'restoring' else 'failed'
            current['error'] = 'Cleanup job was canceled or interrupted; create a fresh preview or confirm restoration again'
            journal = transactions.read_journal(_directory(current)) if current['status'] == 'complete' else None
            pending = False if _restore_recovery_is_settled(current, journal) else None
            store.update_operation(current, recovery_pending=pending)
        return current


def _check_token(operation: Operation, token: object) -> None:
    if not isinstance(token, str) or not operation['preview_token'] or not hmac.compare_digest(
        operation['preview_token'].encode(), token.encode()
    ):
        raise store.CleanupError('Confirm the exact cleanup preview token', 409)


def apply(operation_id: str, token: object, keepers: dict[str, str]) -> Operation:
    operation = store.get_operation(operation_id)
    _check_token(operation, token)
    if operation['status'] != 'ready':
        raise store.CleanupError('Only a ready preview can be applied once', 409)
    operation['selected_keepers'] = planner.validate_keepers(operation, keepers)
    operation.update(status='applying', error=None, backup_path=str(_directory(operation)))
    store.update_operation(operation, previous=('ready',), reset_job=True, recovery_pending=True)
    return _enqueue(operation, 'run_apply')


def restore(operation_id: str, token: object) -> Operation:
    operation = store.get_operation(operation_id)
    _check_token(operation, token)
    if operation['status'] != 'complete':
        raise store.CleanupError('Only a completed cleanup can be restored', 409)
    operation.update(status='restoring', error=None)
    store.update_operation(operation, previous=('complete',), reset_job=True, recovery_pending=True)
    return _enqueue(operation, 'run_restore')


def _analysis_writer_state(video_path: str, job_id: int | None) -> MediaWriteState:
    writer_state = media_write_state(video_path)
    if writer_state.active or store.recovery_operations(video_path):
        try:
            with media_write_lock(video_path, job_id=job_id, blocking=False):
                pass
        except BlockingIOError as error:
            raise store.CleanupError('Media writer is active; create a fresh cleanup preview', 409) from error
        writer_state = media_write_state(video_path)
    return writer_state


def run_analysis(operation_id: str, job_id: int | None = None) -> None:
    operation = store.get_operation(operation_id)
    operation['status'] = 'analyzing'
    store.update_operation(operation, previous=('queued',))
    try:
        writer_state = _analysis_writer_state(operation['video_path'], job_id)
        inputs = planner.capture_inputs(operation)
        planner.analyze_operation(operation, _workspace(operation), lambda: _cancelled(job_id), inputs)
        with media_write_lock(operation['video_path'], job_id=job_id) as previous_writer:
            if previous_writer != writer_state:
                raise store.CleanupError('Media writer changed during cleanup analysis; create a fresh preview', 409)
            planner.validate_analysis_preview(operation)
            if _cancelled(job_id):
                raise CancelledError('Cleanup analysis was canceled')
            store.update_operation(operation, previous=('analyzing',))
    except Exception as error:
        _fail(operation, error)


def _commit_files(operation: Operation, job_id: int | None, workspace: Path) -> Journal:
    planner.validate_preview(operation)
    analysis = MediaAnalysis.from_dict(analysis_record(operation))
    selected = operation['selected_keepers']
    outputs = media.prepare_outputs(analysis, selected, str(workspace), cancelled=lambda: _cancelled(job_id))
    planner.validate_outputs(analysis, selected, outputs, workspace)

    def validate():
        planner.validate_preview(operation)
        if _cancelled(job_id):
            raise CancelledError('Cleanup was canceled before replacement')

    identities = [transactions.FileIdentity(**item) for item in operation['files']]
    return transactions.commit_changes(_directory(operation), outputs, identities, validate)


def _apply_files(operation: Operation, job_id: int | None) -> Journal:
    with TemporaryDirectory(prefix='apply-', dir=_workspace(operation)) as directory:
        path = Path(directory)
        info = path.stat()
        operation['staging_workspace'] = {'path': directory, 'device': info.st_dev, 'inode': info.st_ino}
        store.update_operation(operation, previous=('applying',))
        return _commit_files(operation, job_id, path)


def _cleanup_staging(operation: Operation) -> None:
    record = operation.get('staging_workspace')
    if record is None:
        return
    path = Path(record['path'])
    if path.parent.resolve() != _workspace(operation).resolve():
        raise ValueError('Cleanup staging directory escaped its operation workspace')
    try:
        info = path.lstat()
    except FileNotFoundError:
        operation['staging_workspace'] = None
        return
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (record['device'], record['inode']):
        raise ValueError('Cleanup staging directory identity changed; refusing removal')
    shutil.rmtree(path)
    operation['staging_workspace'] = None


def run_apply(operation_id: str, job_id: int | None = None) -> None:
    with _locked_operation(operation_id, job_id) as operation:
        if operation is None or operation['status'] != 'applying':
            return
        # Staging MUST rearm recovery after the guard clears an empty claim.
        store.update_operation(operation, previous=('applying',), recovery_pending=True)
        journal = _apply_files(operation, job_id)
        _finish(operation, journal)


def run_restore(operation_id: str, job_id: int | None = None) -> None:
    with _locked_operation(operation_id, job_id) as operation:
        if operation is None or operation['status'] != 'restoring':
            return
        if _cancelled(job_id):
            operation['status'] = 'complete'
            store.update_operation(operation, recovery_pending=False)
            return
        journal = transactions.restore_transaction(_directory(operation))
        _finish(operation, journal)


@contextmanager
def _locked_operation(operation_id: str, job_id: int | None) -> Iterator[Operation | None]:
    snapshot = store.get_operation_snapshot(operation_id)
    operation = snapshot.operation()
    with ExitStack() as stack:
        try:
            stack.enter_context(media_write_lock(operation['video_path'], job_id=job_id))
        except Exception as error:
            _fail_before_lock(snapshot, error)
            yield None
            return
        operation = store.get_operation(operation_id)
        try:
            yield operation
        except Exception as error:
            _fail(operation, error)


def _finish(operation: Operation, journal: Journal, snapshot: store.OperationSnapshot | None = None) -> bool:
    _cleanup_staging(operation)
    restored = journal['phase'] == 'restored'
    operation['status'] = 'restored' if restored else 'complete'
    selected = operation.get('selected_keepers', [])
    all_keys = [track.key for track in MediaAnalysis.from_dict(analysis_record(operation)).tracks]
    operation['result'] = {
        'removed_track_keys': [] if restored else [key for key in all_keys if key not in selected],
        'retained_track_keys': all_keys if restored else selected,
        'changed_files': [entry['path'] for entry in journal['entries']],
        'restored_files': [entry['path'] for entry in journal['entries']] if restored else [],
    }
    operation['error'] = None
    pending = False
    try:
        store.refresh_inventory(operation)
    except Exception as error:
        operation['error'] = f'Files verified, but inventory refresh failed: {error}'
        pending = True
        logging.exception('Subtitle cleanup inventory refresh failed')
    if snapshot is not None:
        return store.update_operation_snapshot(operation, snapshot, recovery_pending=pending)
    store.update_operation(operation, recovery_pending=pending)
    return True


def _record_failure(operation: Operation, error: Exception) -> tuple[Journal | None, Exception]:
    try:
        journal = transactions.read_journal(_directory(operation))
    except (OSError, ValueError) as journal_error:
        journal = None
        error = RuntimeError(f'{error}; journal recovery requires attention: {journal_error}')
    refused_restore = operation['status'] == 'restoring' and journal is not None and journal['phase'] == 'complete'
    operation.update(status='complete' if refused_restore else 'failed', error=str(error))
    return journal, error


def _restore_recovery_is_settled(operation: Operation, journal: Journal | None) -> bool:
    return operation['status'] == 'complete' and journal is not None and journal['phase'] == 'complete' \
        and operation.get('staging_workspace') is None


def _fail_before_lock(snapshot: store.OperationSnapshot, error: Exception) -> None:
    canceled = isinstance(error, (CancelledError, JobCanceled))
    while True:
        operation = snapshot.operation()
        journal, failure = _record_failure(operation, error)
        pending = False if _restore_recovery_is_settled(operation, journal) else None
        if store.update_operation_snapshot(operation, snapshot, recovery_pending=pending):
            break
        current = store.get_operation_snapshot(snapshot.id)
        if current.payload != snapshot.payload or current.recovery_pending != snapshot.recovery_pending:
            break
        snapshot = current
    if canceled:
        raise JobCanceled(str(failure)) from error
    logging.exception('Subtitle cleanup operation %s could not acquire its media lock', snapshot.id)


def _fail(operation: Operation, error: Exception) -> None:
    canceled = isinstance(error, (CancelledError, JobCanceled))
    journal, error = _record_failure(operation, error)
    retired = False
    if journal is not None and (operation['status'] == 'complete' or journal['phase'] == 'rolled_back'):
        try:
            if journal['phase'] == 'rolled_back':
                transactions.discard_rollback_backups(_directory(operation))
            _cleanup_staging(operation)
            retired = True
        except (OSError, ValueError):
            logging.exception('Subtitle cleanup failure staging requires recovery for %s', operation['id'])
    store.update_operation(operation, recovery_pending=False if retired else None)
    if canceled:
        raise JobCanceled(str(error)) from error
    logging.exception('Subtitle cleanup operation %s failed', operation['id'])


def recover_for_media(video_path: str) -> bool:
    recovered = False
    for snapshot in store.recovery_snapshots(video_path):
        recovered = _recover_snapshot(snapshot) or recovered
    return recovered


def _recover_snapshot(snapshot: store.OperationSnapshot) -> bool:
    recovered = False
    while snapshot.recovery_pending:
        settled, recovered_changes = _recover_snapshot_once(snapshot)
        recovered = recovered_changes or recovered
        if settled:
            return recovered
        snapshot = store.get_operation_snapshot(snapshot.id)
    return recovered


def _recover_snapshot_once(snapshot: store.OperationSnapshot) -> tuple[bool, bool]:
    operation = snapshot.operation()
    directory = _directory(operation)
    journal = transactions.read_journal(directory)
    if journal is None:
        _cleanup_staging(operation)
        return store.update_operation_snapshot(operation, snapshot, recovery_pending=False), False
    if journal['phase'] == 'complete' and operation['status'] == 'restoring':
        return True, False
    needs_recovery = journal['phase'] != 'rolled_back'
    journal = transactions.recover_transaction(directory)
    _cleanup_staging(operation)
    if journal['phase'] in ('complete', 'restored'):
        return _finish(operation, journal, snapshot), needs_recovery
    operation.update(status='failed', error=operation['error'] or
                     'Interrupted cleanup was rolled back; create a fresh preview')
    return store.update_operation_snapshot(operation, snapshot, recovery_pending=False), needs_recovery


def _recover_operation(operation: Operation) -> None:
    with media_write_lock(operation['video_path']):
        current = store.get_operation(operation['id'])
        if current['status'] in ('queued', 'analyzing', 'applying'):
            current.update(status='failed', error='Cleanup interrupted by restart; create a fresh preview')
            store.update_operation(current)
        elif current['status'] == 'restoring':
            current.update(status='complete', error='Restore interrupted before changes; confirm restoration again')
            journal = transactions.read_journal(_directory(current))
            pending = False if _restore_recovery_is_settled(current, journal) else None
            store.update_operation(current, recovery_pending=pending)


def recover_interrupted_operations() -> None:
    from app.database import database, select, TableSubtitleCleanupOperations
    rows = database.execute(select(TableSubtitleCleanupOperations.id)
                            .where(TableSubtitleCleanupOperations.recovery_pending.is_(True) |
                                   TableSubtitleCleanupOperations.status.in_(
                                       ('queued', 'analyzing', 'applying', 'restoring')))).all()
    for row in rows:
        operation = store.get_operation(row.id)
        try:
            _recover_operation(operation)
        except Exception as error:
            operation.update(status='failed', error=f'Cleanup recovery requires attention: {error}')
            store.update_operation(operation)
            logging.exception('Subtitle cleanup recovery failed for %s', row.id)


register_media_write_guard(recover_for_media)
