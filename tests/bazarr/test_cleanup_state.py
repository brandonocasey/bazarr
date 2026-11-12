from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
from threading import Event, Thread

import pytest

from app.database import database, select, TableSubtitleCleanupOperations
from app.jobs_queue import jobs_queue
from subtitles.cleanup import service, store, transactions
from tests.bazarr.test_cleanup_api import authenticated_client, library, media, _apply, _preview, _run
from utilities.media_write_lock import media_write_lock, media_write_state


@pytest.mark.parametrize('refresh_fails', [False, True])
def test_restore_requested_at_completion_is_never_lost(library, monkeypatch, refresh_fails):
    client, headers, _, video, aligned, lower = library
    originals = {path: path.read_bytes() for path in (video, aligned, lower)}
    preview = _preview(library)
    refresh = store.refresh_inventory
    requested = []

    def request_restore():
        response = client.post(f'/api/subtitles/cleanup/{preview["id"]}/restore', headers=headers,
                               json={'preview_token': preview['preview_token']})
        requested.append((response.status_code, response.json))

    def refresh_with_immediate_restore(operation):
        refresh(operation)
        current = store.get_operation(operation['id'])
        if current['status'] == 'complete' and not requested:
            request_restore()
        if refresh_fails:
            (video.parent / 'absent-inventory-input').stat()

    monkeypatch.setattr(store, 'refresh_inventory', refresh_with_immediate_restore)
    completed = _apply(library, preview)
    if not requested:
        request_restore()
    assert requested[0][0] == 202, requested
    restored = _run(requested[0][1])
    assert restored['status'] == 'restored', restored
    assert {path: path.read_bytes() for path in originals} == originals
    if refresh_fails:
        assert 'Files verified, but inventory refresh failed:' in completed['error']
        assert 'absent-inventory-input' in restored['error']
    else:
        assert restored['error'] is None


def test_apply_records_original_failure_before_releasing_media_lock(library, monkeypatch):
    _, _, _, video, *_ = library
    preview = _preview(library)
    commit = transactions.commit_changes
    update = store.update_operation
    failures = []

    def fail_validation(directory, outputs, identities, _validate):
        def validate():
            (video.parent / 'absent-commit-input').stat()
        return commit(directory, outputs, identities, validate)

    def observe_failure(operation, *args, **kwargs):
        if operation['status'] == 'failed':
            with ThreadPoolExecutor(max_workers=1) as pool:
                state = pool.submit(media_write_state, str(video)).result(timeout=5)
            failures.append((operation['error'], state.active))
        return update(operation, *args, **kwargs)

    monkeypatch.setattr(transactions, 'commit_changes', fail_validation)
    monkeypatch.setattr(store, 'update_operation', observe_failure)
    failed = _apply(library, preview)
    assert failed['status'] == 'failed'
    assert 'absent-commit-input' in failed['error']
    assert failures == [(failed['error'], True)]
    assert database.scalar(select(TableSubtitleCleanupOperations.recovery_pending).where(
        TableSubtitleCleanupOperations.id == failed['id'])) is False


@pytest.mark.parametrize('claim', ['unchanged', 'job_attachment', 'payload_change', 'pending_change'])
def test_canceled_lock_wait_preserves_current_claim_and_live_writer(library, monkeypatch, claim):
    _, _, _, video, *_ = library
    preview = _preview(library)
    keepers = {group['key']: group['recommended_keeper'] for group in preview['groups']}
    applying = service.apply(preview['id'], preview['preview_token'], keepers)
    job = next(job for job in jobs_queue.jobs_pending_queue if job.job_id == applying['job_id'])
    original_lock = service.media_write_lock
    entered, release = Event(), Event()
    failures = []

    def writer():
        try:
            with media_write_lock(str(video)):
                entered.set()
                if not release.wait(15):
                    raise TimeoutError('writer release timed out')
        finally:
            database.remove()

    @contextmanager
    def cancel_before_acquisition(path, job_id=None):
        if claim == 'job_attachment':
            store.set_job_id(applying['id'], applying['job_id'])
        elif claim == 'payload_change':
            current = store.get_operation(applying['id'])
            current.update(status='failed', error='newer terminal claim')
            store.update_operation(current, recovery_pending=False)
        elif claim == 'pending_change':
            store.update_operation(store.get_operation(applying['id']), recovery_pending=False)
        job.cancel_event.set()
        try:
            with original_lock(path, job_id=job_id):
                yield
        except Exception as error:
            failures.append(str(error))
            raise

    thread = Thread(target=writer)
    thread.start()
    try:
        assert entered.wait(5)
        current = store.get_operation(applying['id'])
        store.update_operation(current, reset_job=claim == 'job_attachment', recovery_pending=True)
        before = media_write_state(str(video))
        monkeypatch.setattr(service, 'media_write_lock', cancel_before_acquisition)
        result = _run(applying)
        after = media_write_state(str(video))
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert before.active and after == before
    assert job.status == 'canceled'
    assert len(failures) == 1
    if claim == 'payload_change':
        assert result['status'] == 'failed' and result['error'] == 'newer terminal claim'
    elif claim == 'pending_change':
        assert result['status'] == 'applying' and result['error'] is None
    else:
        assert result['status'] == 'failed' and result['error'] == failures[0]
        snapshot = database.execute(select(TableSubtitleCleanupOperations.payload).where(
            TableSubtitleCleanupOperations.id == applying['id'])).scalar_one()
        assert json.loads(snapshot)['job_id'] == applying['job_id']


@pytest.mark.parametrize('claim', ['restore', 'job_attachment'])
def test_recovery_refresh_preserves_later_claim(library, monkeypatch, claim):
    client, headers, _, video, aligned, lower = library
    originals = {path: path.read_bytes() for path in (video, aligned, lower)}
    completed = _apply(library, _preview(library))
    store.update_operation(completed, recovery_pending=True)
    refresh = store.refresh_inventory
    claims = []

    def refresh_with_claim(operation):
        refresh(operation)
        if claims:
            return
        if claim == 'restore':
            response = client.post(f'/api/subtitles/cleanup/{completed["id"]}/restore', headers=headers,
                                   json={'preview_token': completed['preview_token']})
            claims.append((response.status_code, response.json))
        else:
            claims.append(store.set_job_id(completed['id'], -123))

    monkeypatch.setattr(store, 'refresh_inventory', refresh_with_claim)
    with media_write_lock(str(video)):
        pass
    current = store.get_operation(completed['id'])
    if claim == 'restore':
        assert claims[0][0] == 202, claims
        assert current['status'] == 'restoring'
        assert current['job_id'] == claims[0][1]['job_id']
        restored = _run(claims[0][1])
        assert restored['status'] == 'restored', restored
        assert {path: path.read_bytes() for path in originals} == originals
    else:
        assert current['status'] == 'complete' and current['job_id'] == -123
        assert store.list_operations('movie', completed['media_id'])[0]['job_id'] == -123
        assert store.recovery_operations(str(video)) == []


@pytest.mark.parametrize('entrypoint', ['single', 'batch', 'restart'])
@pytest.mark.parametrize('captured_staging', [False, True])
def test_deleted_restore_preserves_explanation_and_retires_only_settled_recovery(
        library, entrypoint, captured_staging):
    client, headers, _, video, aligned, lower = library
    completed = _apply(library, _preview(library))
    cleaned = {path: path.read_bytes() for path in (video, aligned, lower) if path.exists()}
    response = client.post(f'/api/subtitles/cleanup/{completed["id"]}/restore', headers=headers,
                           json={'preview_token': completed['preview_token']})
    assert response.status_code == 202, response.json
    restoring = response.json
    staging = None
    if captured_staging:
        staging = Path(completed['backup_path']).parents[2] / 'cache' / 'subtitle-cleanup' / completed['id'] / 'captured'
        staging.mkdir(parents=True)
        (staging / 'unresolved-output').write_bytes(b'retain captured staging')
        info = staging.stat()
        restoring['staging_workspace'] = {'path': str(staging), 'device': info.st_dev, 'inode': info.st_ino}
        store.update_operation(restoring)
    response = client.delete('/api/system/jobs', headers=headers, data={'id': restoring['job_id']})
    assert response.status_code == 204, response.json
    assert not any(job.job_id == restoring['job_id'] for job in jobs_queue.jobs_pending_queue)
    if entrypoint == 'restart':
        service.recover_interrupted_operations()
    elif entrypoint == 'batch':
        response = client.post('/api/subtitles/cleanup/status', headers=headers,
                               json={'operation_ids': [completed['id']]})
        assert response.status_code == 200, response.json
    else:
        response = client.get(f'/api/subtitles/cleanup/{completed["id"]}', headers=headers)
        assert response.status_code == 200, response.json
    interrupted = store.get_operation(completed['id'])
    assert interrupted['status'] == 'complete'
    assert 'confirm restoration again' in interrupted['error']
    assert database.scalar(select(TableSubtitleCleanupOperations.recovery_pending).where(
        TableSubtitleCleanupOperations.id == completed['id'])) is captured_staging
    if captured_staging:
        assert (staging / 'unresolved-output').read_bytes() == b'retain captured staging'
        assert interrupted['staging_workspace'] == restoring['staging_workspace']
    else:
        for _ in range(2):
            with media_write_lock(str(video)):
                pass
            assert store.get_operation(completed['id'])['error'] == interrupted['error']
            assert store.recovery_operations(str(video)) == []
    assert {path: path.read_bytes() for path in cleaned} == cleaned
    assert not lower.exists()
    journal = transactions.read_journal(Path(completed['backup_path']))
    assert journal['phase'] == 'complete'
    assert all(Path(entry['backup']).exists() for entry in journal['entries'])


def test_status_snapshot_cannot_discard_apply_queued_after_snapshot(library, monkeypatch):
    client, headers, _, video, aligned, lower = library
    preview = _preview(library)
    snapshot_jobs = jobs_queue.snapshot_jobs
    requests = []

    def snapshot_then_apply():
        snapshot = snapshot_jobs()
        if not requests:
            keepers = {group['key']: group['recommended_keeper'] for group in preview['groups']}
            response = client.post(f'/api/subtitles/cleanup/{preview["id"]}/apply', headers=headers,
                                   json={'preview_token': preview['preview_token'], 'keepers': keepers})
            requests.append((response.status_code, response.json))
        return snapshot

    monkeypatch.setattr(jobs_queue, 'snapshot_jobs', snapshot_then_apply)
    response = client.get(f'/api/subtitles/cleanup/{preview["id"]}', headers=headers)
    assert response.status_code == 200, response.json
    assert requests[0][0] == 202, requests
    applying = store.get_operation(preview['id'])
    assert applying['status'] == 'applying', applying
    assert applying['job_id'] == requests[0][1]['job_id']
    job = next(job for job in jobs_queue.jobs_pending_queue if job.job_id == applying['job_id'])
    assert jobs_queue._run_job(job_instance=job)
    completed = store.get_operation(preview['id'])
    assert job.status == 'completed'
    assert completed['status'] == 'complete', completed
    assert completed['error'] is None
    assert video.exists() and aligned.exists() and not lower.exists()
    journal = transactions.read_journal(Path(completed['backup_path']))
    assert journal['phase'] == 'complete'
    assert store.recovery_operations(str(video)) == []
