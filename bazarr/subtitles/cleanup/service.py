# coding=utf-8

import hmac
import logging
import shutil
import stat
from concurrent.futures import CancelledError
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from app.get_args import args
from app.jobs_queue import jobs_queue, JobCanceled
from utilities.media_write_lock import media_write_lock, register_media_write_guard

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
    store.update_operation(operation, previous=(previous,))


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


def _has_active_job(operation: Operation) -> bool:
    job_id = operation['job_id']
    if job_id is None:
        return True
    jobs = jobs_queue.list_jobs_from_queue(job_id=job_id)
    return any(job['module'] == __name__ and job['kwargs'].get('operation_id') == operation['id']
               and job['status'] in ('pending', 'running') for job in jobs)


def get_operation(operation_id: str) -> Operation:
    operation = store.get_operation(operation_id)
    pending = ('queued', 'analyzing', 'applying', 'restoring')
    if operation['status'] not in pending or _has_active_job(operation):
        return operation
    with media_write_lock(operation['video_path']):
        operation = store.get_operation(operation_id)
        if operation['status'] in pending and not _has_active_job(operation):
            operation['status'] = 'complete' if operation['status'] == 'restoring' else 'failed'
            operation['error'] = 'Cleanup job was canceled or interrupted; create a fresh preview or confirm restoration again'
            store.update_operation(operation)
    return operation


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
    store.update_operation(operation, previous=('ready',), reset_job=True)
    return _enqueue(operation, 'run_apply')


def restore(operation_id: str, token: object) -> Operation:
    operation = store.get_operation(operation_id)
    _check_token(operation, token)
    if operation['status'] != 'complete':
        raise store.CleanupError('Only a completed cleanup can be restored', 409)
    operation.update(status='restoring', error=None)
    store.update_operation(operation, previous=('complete',), reset_job=True)
    return _enqueue(operation, 'run_restore')


def run_analysis(operation_id: str, job_id: int | None = None) -> None:
    operation = store.get_operation(operation_id)
    operation['status'] = 'analyzing'
    store.update_operation(operation, previous=('queued',))
    try:
        with media_write_lock(operation['video_path'], job_id=job_id):
            planner.analyze_operation(operation, _workspace(operation), lambda: _cancelled(job_id))
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
    operation = store.get_operation(operation_id)
    try:
        with media_write_lock(operation['video_path'], job_id=job_id):
            operation = store.get_operation(operation_id)
            if operation['status'] != 'applying':
                return
            journal = _apply_files(operation, job_id)
            _finish(operation, journal)
    except Exception as error:
        _fail(operation, error)


def run_restore(operation_id: str, job_id: int | None = None) -> None:
    operation = store.get_operation(operation_id)
    try:
        with media_write_lock(operation['video_path'], job_id=job_id):
            operation = store.get_operation(operation_id)
            if operation['status'] != 'restoring':
                return
            if _cancelled(job_id):
                operation['status'] = 'complete'
                store.update_operation(operation)
                return
            journal = transactions.restore_transaction(_directory(operation))
            _finish(operation, journal)
    except Exception as error:
        _fail(operation, error)


def _finish(operation: Operation, journal: Journal) -> None:
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
    store.update_operation(operation)
    try:
        store.refresh_inventory(operation)
    except Exception as error:
        operation['error'] = f'Files verified, but inventory refresh failed: {error}'
        store.update_operation(operation)
        logging.exception('Subtitle cleanup inventory refresh failed')


def _fail(operation: Operation, error: Exception) -> None:
    try:
        journal = transactions.read_journal(_directory(operation))
    except (OSError, ValueError) as journal_error:
        journal = None
        error = RuntimeError(f'{error}; journal recovery requires attention: {journal_error}')
    status = 'complete' if operation['status'] == 'restoring' and journal and journal['phase'] == 'complete' else 'failed'
    operation.update(status=status, error=str(error))
    store.update_operation(operation)
    if isinstance(error, (CancelledError, JobCanceled)):
        raise JobCanceled(str(error)) from error
    logging.exception('Subtitle cleanup operation %s failed', operation['id'])


def recover_for_media(video_path: str) -> bool:
    recovered = False
    for operation in store.recovery_operations(video_path):
        directory = _directory(operation)
        journal = transactions.read_journal(directory)
        if journal is None:
            _cleanup_staging(operation)
            store.update_operation(operation)
            continue
        if journal['phase'] == 'complete' and operation['status'] == 'restoring':
            continue
        journal = transactions.recover_transaction(directory)
        recovered = True
        _cleanup_staging(operation)
        if journal['phase'] in ('complete', 'restored'):
            _finish(operation, journal)
        elif journal['phase'] == 'rolled_back':
            operation.update(status='failed', error='Interrupted cleanup was rolled back; create a fresh preview')
            store.update_operation(operation)
    return recovered


def _recover_operation(operation: Operation) -> None:
    with media_write_lock(operation['video_path']):
        current = store.get_operation(operation['id'])
        if current['status'] in ('queued', 'analyzing', 'applying'):
            current.update(status='failed', error='Cleanup interrupted by restart; create a fresh preview')
            store.update_operation(current)
        elif current['status'] == 'restoring':
            current.update(status='complete', error='Restore interrupted before changes; confirm restoration again')
            store.update_operation(current)


def recover_interrupted_operations() -> None:
    from app.database import database, select, TableSubtitleCleanupOperations
    rows = database.execute(select(TableSubtitleCleanupOperations.id)
                            .where(TableSubtitleCleanupOperations.status.in_(
                                ('queued', 'analyzing', 'applying', 'restoring', 'failed')))).all()
    for row in rows:
        operation = store.get_operation(row.id)
        try:
            _recover_operation(operation)
        except Exception as error:
            operation.update(status='failed', error=f'Cleanup recovery requires attention: {error}')
            store.update_operation(operation)
            logging.exception('Subtitle cleanup recovery failed for %s', row.id)


register_media_write_guard(recover_for_media)
