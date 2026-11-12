from pathlib import Path
import subprocess
import sys
from threading import Event, Thread

import pytest
from sqlalchemy import event

from app.database import database, engine, select, TableSubtitleCleanupOperations
from app.jobs_queue import jobs_queue
from subtitles.cleanup import media as cleanup_media, planner, service, store
from tests.bazarr.test_cleanup_api import authenticated_client, library, media, _run
from utilities.media_write_lock import media_write_lock


@pytest.mark.parametrize('count', [1, 300, 3000])
def test_batch_status_preserves_rows_with_one_queue_snapshot(library, monkeypatch, count):
    client, headers, media_id, *_ = library
    operation = service.preview('movie', media_id)
    ids = [operation['id'], *[f'absent-{index}' for index in range(count)], operation['id']]
    snapshots = []
    original = jobs_queue.snapshot_jobs

    def snapshot(*args, **kwargs):
        snapshots.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(jobs_queue, 'snapshot_jobs', snapshot)
    response = client.post('/api/subtitles/cleanup/status', headers=headers, json={'operation_ids': ids})
    assert response.status_code == 200, response.json
    assert response.json['data'][0] == {'id': operation['id'], 'operation': operation, 'error': None}
    assert response.json['data'][1:] == [
        {'id': f'absent-{index}', 'operation': None, 'error': 'Cleanup operation not found'}
        for index in range(count)]
    assert len(snapshots) == 1


@pytest.mark.parametrize('body', [None, [], {}, {'operation_ids': 'id'}, {'operation_ids': [1]},
                                 {'operation_ids': [None]}])
def test_batch_status_rejects_malformed_body(authenticated_client, body):
    client, headers = authenticated_client
    response = client.post('/api/subtitles/cleanup/status', headers=headers, json=body)
    assert response.status_code == 400


def test_batch_status_accepts_empty_and_requires_authentication(authenticated_client):
    client, headers = authenticated_client
    assert client.post('/api/subtitles/cleanup/status', json={'operation_ids': []}).status_code == 401
    response = client.post('/api/subtitles/cleanup/status', headers=headers, json={'operation_ids': []})
    assert response.status_code == 200
    assert response.json == {'data': []}


def test_settled_failure_does_not_repeat_recovery_writes(library):
    _, _, media_id, video, *_ = library
    operation = store.create_operation('movie', media_id, None)
    operation.update(status='failed', error='Analysis refused before creating changes')
    store.update_operation(operation)
    with media_write_lock(str(video)):
        pass
    writes = []

    def record_write(_connection, _cursor, statement, *_details):
        if statement.lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')):
            writes.append(statement)

    event.listen(engine, 'before_cursor_execute', record_write)
    try:
        with media_write_lock(str(video)):
            pass
        service.recover_interrupted_operations()
    finally:
        event.remove(engine, 'before_cursor_execute', record_write)
    assert writes == []


def test_analysis_allows_media_writer_during_tool_execution(library, monkeypatch):
    _, _, media_id, video, *_ = library
    entered = Event()
    errors = []
    writers = []
    acquired_during_analysis = []
    original = cleanup_media.analyze_media

    def writer():
        try:
            with media_write_lock(str(video)):
                entered.set()
        except Exception as error:
            errors.append(error)

    def analyze(*args, **kwargs):
        thread = Thread(target=writer)
        writers.append(thread)
        thread.start()
        acquired_during_analysis.append(entered.wait(2))
        return original(*args, **kwargs)

    monkeypatch.setattr(cleanup_media, 'analyze_media', analyze)
    try:
        operation = _run(service.preview('movie', media_id))
    finally:
        for thread in writers:
            thread.join(5)
    assert not errors
    assert acquired_during_analysis == [True], 'Read-only analysis blocked a normal media writer'
    assert operation['status'] == 'failed'
    assert 'writer' in operation['error'].lower()


def test_analysis_revalidates_inputs_before_publishing(library, monkeypatch):
    _, _, media_id, _, aligned, *_ = library
    original = planner.analyze_operation

    def analyze(*args, **kwargs):
        operation = original(*args, **kwargs)
        aligned.write_text(aligned.read_text() + '\n')
        return operation

    monkeypatch.setattr(planner, 'analyze_operation', analyze)
    operation = _run(service.preview('movie', media_id))
    assert operation['status'] == 'failed'
    assert 'changed' in operation['error']


def test_batch_status_isolates_failed_revalidation_and_preserves_other_rows(library, monkeypatch):
    from contextlib import contextmanager

    client, headers, media_id, *_ = library
    inactive = store.create_operation('movie', media_id, None)
    store.set_job_id(inactive['id'], -1)
    active = service.preview('movie', media_id)

    @contextmanager
    def unavailable(path):
        raise OSError('Media recovery storage unavailable')
        yield

    monkeypatch.setattr(service, 'media_write_lock', unavailable)
    response = client.post('/api/subtitles/cleanup/status', headers=headers,
                           json={'operation_ids': [inactive['id'], active['id']]})
    assert response.status_code == 200
    assert response.json['data'] == [
        {'id': inactive['id'], 'operation': None, 'error': 'Media recovery storage unavailable'},
        {'id': active['id'], 'operation': active, 'error': None},
    ]


def test_batch_status_marks_inactive_job_under_its_media_lock(library):
    client, headers, media_id, *_ = library
    operation = store.create_operation('movie', media_id, None)
    store.set_job_id(operation['id'], -1)
    response = client.post('/api/subtitles/cleanup/status', headers=headers,
                           json={'operation_ids': [operation['id']]})
    assert response.status_code == 200
    row = response.json['data'][0]
    assert row['error'] is None
    assert row['operation']['status'] == 'failed'
    assert 'canceled or interrupted' in row['operation']['error']


def test_recovery_pending_survives_apply_failure_until_rollback_and_staging_cleanup(library, monkeypatch):
    from tests.bazarr.test_cleanup_api import _apply, _preview
    from subtitles.cleanup import transactions

    _, _, _, video, *_ = library
    operation = _preview(library)
    original = transactions.commit_changes

    def interrupt(*args, **kwargs):
        def stop():
            raise OSError('Interrupted before replacement')
        return original(args[0], args[1], args[2], stop)

    monkeypatch.setattr(transactions, 'commit_changes', interrupt)
    apply_files = service._apply_files
    foreign = service._workspace(operation) / 'foreign-workspace'
    foreign.mkdir()
    marker = foreign / 'foreign.txt'
    marker.write_text('unowned staging must survive')

    def replace_finished_workspace(current, job_id):
        try:
            return apply_files(current, job_id)
        except OSError:
            foreign.rename(current['staging_workspace']['path'])
            raise

    with monkeypatch.context() as patch:
        patch.setattr(service, '_apply_files', replace_finished_workspace)
        failed = _apply(library, operation)
    assert failed['status'] == 'failed'
    assert failed['error'] == 'Interrupted before replacement'
    replacement = Path(failed['staging_workspace']['path'])
    assert (replacement / marker.name).read_text() == 'unowned staging must survive'
    assert database.scalar(select(TableSubtitleCleanupOperations.recovery_pending).where(
        TableSubtitleCleanupOperations.id == operation['id'])) is True
    (replacement / marker.name).unlink()
    replacement.rmdir()
    with media_write_lock(str(video)):
        pass
    assert database.scalar(select(TableSubtitleCleanupOperations.recovery_pending).where(
        TableSubtitleCleanupOperations.id == operation['id'])) is False
    assert store.recovery_operations(str(video)) == []
    assert store.get_operation(operation['id'])['error'] == failed['error']


def test_recovery_migration_preserves_legacy_rows_and_is_additive():
    from importlib import import_module
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = import_module('migrations.versions.7d3e8b2f5a12_cleanup_recovery_pending')
    previous = import_module('migrations.versions.6c2d7a9e4b11_subtitle_cleanup')
    statuses = ['queued', 'analyzing', 'ready', 'applying', 'restoring', 'failed', 'complete', 'restored']
    legacy = sa.create_engine('sqlite://')
    with legacy.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            previous.upgrade()
        for status in statuses:
            connection.exec_driver_sql(
                'INSERT INTO table_subtitle_cleanup_operations '
                '(id, media_type, media_id, video_path, status, payload, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (status, 'movie', 1, '/missing/video.mkv', status, 'preserved', '2026-01-01'))
        columns = [column['name'] for column in sa.inspect(connection).get_columns(
            'table_subtitle_cleanup_operations')]
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
        assert connection.exec_driver_sql(
            'SELECT id, payload, recovery_pending FROM table_subtitle_cleanup_operations ORDER BY id').all() == [
                (status, 'preserved', int(status in ('applying', 'restoring', 'failed')))
                for status in sorted(statuses)]
        assert [column['name'] for column in sa.inspect(connection).get_columns(
            'table_subtitle_cleanup_operations')] == [*columns, 'recovery_pending']
        connection.exec_driver_sql(
            'INSERT INTO table_subtitle_cleanup_operations '
            '(id, media_type, media_id, video_path, status, payload, created_at) '
            "VALUES ('fresh', 'movie', 1, '/missing/fresh.mkv', 'queued', 'fresh payload', '2026-01-01')")
        assert connection.exec_driver_sql(
            "SELECT recovery_pending FROM table_subtitle_cleanup_operations WHERE id = 'fresh'").scalar() == 0
    legacy.dispose()


def test_fresh_cleanup_table_defaults_to_inactive_recovery():
    import sqlalchemy as sa

    fresh = sa.create_engine('sqlite://')
    TableSubtitleCleanupOperations.__table__.create(fresh)
    with fresh.begin() as connection:
        connection.exec_driver_sql(
            'INSERT INTO table_subtitle_cleanup_operations '
            '(id, media_type, media_id, video_path, status, payload, created_at) '
            "VALUES ('fresh', 'movie', 1, '/missing/fresh.mkv', 'queued', '{}', '2026-01-01')")
        assert connection.exec_driver_sql(
            'SELECT recovery_pending FROM table_subtitle_cleanup_operations').scalar() == 0
    fresh.dispose()


def test_batch_status_uses_chunked_database_reads(library):
    from app.database import SUBTITLE_MAP_QUERY_BATCH_SIZE

    client, headers, media_id, *_ = library
    operation = service.preview('movie', media_id)
    ids = [operation['id'], *[f'missing-{index}' for index in range(3000)]]
    parameter_counts = []

    def record_reads(_connection, _cursor, statement, *details):
        parameters = details[0]
        if 'table_subtitle_cleanup_operations.id IN (' in statement:
            parameter_counts.append(len(parameters))

    event.listen(engine, 'before_cursor_execute', record_reads)
    try:
        response = client.post('/api/subtitles/cleanup/status', headers=headers, json={'operation_ids': ids})
    finally:
        event.remove(engine, 'before_cursor_execute', record_reads)
    assert response.status_code == 200
    assert len(response.json['data']) == len(ids)
    assert sum(parameter_counts) == len(ids)
    assert max(parameter_counts) <= SUBTITLE_MAP_QUERY_BATCH_SIZE


@pytest.mark.parametrize('batch', [False, True])
def test_batch_status_preserves_new_job_claim_during_lock_acquisition(library, monkeypatch, batch):
    from contextlib import contextmanager

    client, headers, media_id, video, *_ = library
    operation = store.create_operation('movie', media_id, None)
    store.set_job_id(operation['id'], -1)
    original_lock = service.media_write_lock

    @contextmanager
    def claimed_lock(path):
        with original_lock(path):
            store.set_job_id(operation['id'], -2)
            yield

    monkeypatch.setattr(service, 'media_write_lock', claimed_lock)
    if batch:
        response = client.post('/api/subtitles/cleanup/status', headers=headers,
                               json={'operation_ids': [operation['id']]})
        current = response.json['data'][0]['operation']
    else:
        response = client.get(f'/api/subtitles/cleanup/{operation["id"]}', headers=headers)
        current = response.json
    assert response.status_code == 200
    assert current['status'] == 'queued'
    assert current['job_id'] == -2


@pytest.mark.parametrize('batch', [False, True])
def test_batch_status_observes_live_job_reactivation(library, monkeypatch, batch):
    from contextlib import contextmanager

    client, headers, media_id, *_ = library
    operation = service.preview('movie', media_id)
    job = next(job for job in jobs_queue.jobs_pending_queue if job.job_id == operation['job_id'])
    job.status = 'canceled'
    original_lock = service.media_write_lock

    @contextmanager
    def activated_lock(path):
        with original_lock(path):
            job.status = 'running'
            yield

    monkeypatch.setattr(service, 'media_write_lock', activated_lock)
    if batch:
        response = client.post('/api/subtitles/cleanup/status', headers=headers,
                               json={'operation_ids': [operation['id']]})
        current = response.json['data'][0]['operation']
    else:
        response = client.get(f'/api/subtitles/cleanup/{operation["id"]}', headers=headers)
        current = response.json
    assert response.status_code == 200
    assert current['status'] == 'queued'


def test_batch_status_isolates_unreadable_saved_row(library):
    from app.database import update

    client, headers, media_id, *_ = library
    broken = store.create_operation('movie', media_id, None)
    active = service.preview('movie', media_id)
    database.execute(update(TableSubtitleCleanupOperations).where(
        TableSubtitleCleanupOperations.id == broken['id']).values(payload='{'))
    database.commit()
    response = client.post('/api/subtitles/cleanup/status', headers=headers,
                           json={'operation_ids': [broken['id'], active['id']]})
    assert response.status_code == 200
    assert response.json['data'][0]['id'] == broken['id']
    assert response.json['data'][0]['operation'] is None
    assert response.json['data'][0]['error']
    assert response.json['data'][1] == {'id': active['id'], 'operation': active, 'error': None}


def test_analysis_hashing_allows_writers_and_rejects_overlapping_generation(library, monkeypatch):
    from types import SimpleNamespace
    from subtitles.cleanup import transactions

    _, _, media_id, video, *_ = library
    original_sha256 = transactions.hashlib.sha256
    acquired_during_hash = []
    writers = []
    errors = []

    def writer(entered):
        try:
            with media_write_lock(str(video)):
                entered.set()
        except Exception as error:
            errors.append(error)

    class SlowDigest:
        def __init__(self):
            self.delegate = original_sha256()
            self.started = False

        def update(self, chunk):
            if not self.started:
                self.started = True
                entered = Event()
                thread = Thread(target=writer, args=(entered,))
                writers.append(thread)
                thread.start()
                acquired_during_hash.append(entered.wait(2))
            self.delegate.update(chunk)

        def hexdigest(self):
            return self.delegate.hexdigest()

    monkeypatch.setattr(transactions, 'hashlib', SimpleNamespace(sha256=SlowDigest))
    try:
        operation = _run(service.preview('movie', media_id))
    finally:
        for thread in writers:
            thread.join(5)
    assert not errors
    assert acquired_during_hash and all(acquired_during_hash), 'Full analysis hashing blocked a media writer'
    assert operation['status'] == 'failed'
    assert 'writer' in operation['error'].lower()


def test_concurrent_analysis_previews_only_publish_consistent_inputs(library, monkeypatch):
    from threading import Barrier
    from tests.bazarr.test_cleanup_api import _sha

    _, _, media_id, video, aligned, lower = library
    expected = {str(path): _sha(path) for path in (video, aligned, lower)}
    operations = [store.create_operation('movie', media_id, None) for _ in range(2)]
    analyzed = Barrier(2)
    errors = []
    original = cleanup_media.analyze_media

    def analyze(*args, **kwargs):
        result = original(*args, **kwargs)
        analyzed.wait(10)
        return result

    def run(operation):
        try:
            service.run_analysis(operation['id'])
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(cleanup_media, 'analyze_media', analyze)
    threads = [Thread(target=run, args=(operation,)) for operation in operations]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    results = [store.get_operation(operation['id']) for operation in operations]
    assert any(operation['status'] == 'ready' for operation in results)
    for operation in results:
        if operation['status'] == 'ready':
            assert {item['path']: item['sha256'] for item in operation['files']} == expected
        else:
            assert operation['status'] == 'failed'
            assert 'writer' in operation['error'].lower()


@pytest.mark.parametrize('pending_recovery', [False, True])
def test_analysis_refuses_already_active_writer_before_reading(library, monkeypatch, pending_recovery):
    _, _, media_id, video, *_ = library
    operation = store.create_operation('movie', media_id, None)
    if pending_recovery:
        interrupted = store.create_operation('movie', media_id, None)
        interrupted['status'] = 'applying'
        store.update_operation(interrupted, recovery_pending=True)
    entered, release, done = Event(), Event(), Event()
    errors, inputs, reads = [], [], []
    capture_inputs = planner.capture_inputs
    from subtitles.cleanup import transactions
    original_fingerprint = transactions.fingerprint

    def capture(current):
        inputs.append(current['id'])
        return capture_inputs(current)

    def fingerprint(path):
        reads.append(path)
        return original_fingerprint(path)

    monkeypatch.setattr(planner, 'capture_inputs', capture)
    monkeypatch.setattr(planner, 'fingerprint', fingerprint)
    monkeypatch.setattr(transactions, 'fingerprint', fingerprint)

    def writer():
        try:
            with media_write_lock(str(video)):
                entered.set()
                assert release.wait(10)
        except Exception as error:
            errors.append(error)

    def analyze():
        try:
            service.run_analysis(operation['id'])
        finally:
            done.set()

    writer_thread, analysis_thread = Thread(target=writer), Thread(target=analyze)
    writer_thread.start()
    try:
        assert entered.wait(5)
        if pending_recovery:
            store.update_operation(interrupted, recovery_pending=True)
        analysis_thread.start()
        assert done.wait(2), 'Analysis did not reject a writer that was already active'
    finally:
        release.set()
        writer_thread.join(5)
        if analysis_thread.ident is not None:
            analysis_thread.join(10)
    assert not errors
    assert inputs == []
    assert reads == []
    assert not writer_thread.is_alive() and not analysis_thread.is_alive()
    failed = store.get_operation(operation['id'])
    assert failed['status'] == 'failed'
    assert 'writer is active' in failed['error'].lower()


def test_analysis_recovers_pending_journal_before_reading_inputs(library, monkeypatch):
    from tests.bazarr.test_cleanup_api import _apply, _preview
    from subtitles.cleanup import transactions
    from types import SimpleNamespace

    _, _, media_id, *_ = library
    interrupted = _apply(library, _preview(library))
    interrupted['status'] = 'failed'
    store.update_operation(interrupted, recovery_pending=True)
    original_sha256 = transactions.hashlib.sha256
    checked = []

    class RecoveredDigest:
        def __init__(self):
            self.delegate = original_sha256()

        def update(self, chunk):
            checked.append(store.get_operation(interrupted['id'])['status'])
            self.delegate.update(chunk)

        def hexdigest(self):
            return self.delegate.hexdigest()

    monkeypatch.setattr(transactions, 'hashlib', SimpleNamespace(sha256=RecoveredDigest))
    operation = _run(service.preview('movie', media_id))
    assert operation['status'] == 'ready', operation['error']
    assert checked and set(checked) == {'complete'}


def test_analysis_final_validation_does_not_block_writer_with_full_file_reads(library, monkeypatch):
    from subtitles.cleanup import transactions

    _, _, media_id, video, *_ = library
    analyzed = Event()
    acquired_during_final_reads = []
    writers = []
    original_analyze = planner.analyze_operation
    original_fingerprint = transactions.fingerprint

    def analyze(*args, **kwargs):
        operation = original_analyze(*args, **kwargs)
        analyzed.set()
        return operation

    def writer(entered):
        with media_write_lock(str(video)):
            entered.set()

    def fingerprint(path):
        if analyzed.is_set():
            entered = Event()
            thread = Thread(target=writer, args=(entered,))
            writers.append(thread)
            thread.start()
            acquired_during_final_reads.append(entered.wait(2))
        return original_fingerprint(path)

    monkeypatch.setattr(planner, 'analyze_operation', analyze)
    monkeypatch.setattr(transactions, 'fingerprint', fingerprint)
    try:
        operation = _run(service.preview('movie', media_id))
    finally:
        for thread in writers:
            thread.join(5)
    assert all(acquired_during_final_reads), 'Final preview publication blocked a writer while hashing inputs'
    assert operation['status'] == 'ready', operation['error']


def test_completed_rollback_retires_recovery_before_retry(library, monkeypatch):
    from subtitles.cleanup import transactions
    from tests.bazarr.test_cleanup_api import _apply, _preview

    _, _, _, video, *_ = library
    operation = _preview(library)
    commit = transactions.commit_changes

    def interrupt(*args, **kwargs):
        def stop():
            raise OSError('Interrupted before replacement')
        return commit(args[0], args[1], args[2], stop)

    with monkeypatch.context() as patch:
        patch.setattr(transactions, 'commit_changes', interrupt)
        failed = _apply(library, operation)
    assert failed['status'] == 'failed'
    assert failed['error'] == 'Interrupted before replacement'
    assert failed['staging_workspace'] is None
    assert store.recovery_operations(str(video)) == []
    journal = transactions.read_journal(Path(failed['backup_path']))
    assert journal['phase'] == 'rolled_back'
    assert all(not Path(entry['backup']).exists() for entry in journal['entries'])
    reads = []
    fingerprint = transactions.fingerprint

    def record_read(path):
        reads.append(path)
        return fingerprint(path)

    with monkeypatch.context() as patch:
        patch.setattr(transactions, 'fingerprint', record_read)
        for _ in range(2):
            with media_write_lock(str(video)):
                pass
        service.recover_interrupted_operations()
    assert reads == []
    assert store.get_operation(operation['id'])['error'] == failed['error']
    assert _preview(library)['status'] == 'ready'


@pytest.mark.parametrize('legacy_pending', [False, True])
def test_settled_ownership_refusal_preserves_cause_without_rehashing(library, monkeypatch, legacy_pending):
    import os
    from subtitles.cleanup import transactions
    from tests.bazarr.test_cleanup_api import _apply, _preview

    _, _, _, video, *_ = library
    operation = _preview(library)
    chmod = os.chmod

    def refuse_metadata(path, *args, **kwargs):
        if 'originals' in Path(path).parts:
            raise PermissionError('filesystem denied metadata preservation')
        return chmod(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, 'chmod', refuse_metadata)
        failed = _apply(library, operation)
    assert failed['status'] == 'failed'
    assert 'Cannot preserve metadata for original file' in failed['error']
    assert 'Grant the Bazarr service account permission' in failed['error']
    assert store.recovery_operations(str(video)) == []
    if legacy_pending:
        store.update_operation(failed, recovery_pending=True)
    reads = []
    fingerprint = transactions.fingerprint

    def record_read(path):
        reads.append(path)
        return fingerprint(path)

    monkeypatch.setattr(transactions, 'fingerprint', record_read)
    for _ in range(2):
        with media_write_lock(str(video)):
            pass
        service.recover_interrupted_operations()
        assert store.get_operation(operation['id'])['error'] == failed['error']
    assert reads == []
    assert store.recovery_operations(str(video)) == []


def test_analysis_repairs_actual_crashed_writer_before_repeated_previews(library, monkeypatch):
    from app.get_args import args
    from utilities.media_write_lock import media_write_state
    from tests.bazarr.test_cleanup_api import _preview

    _, _, _, video, *_ = library
    script = """
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'bazarr'))
os.environ['NO_CLI'] = 'true'
import bazarr.app.libs
from app.get_args import args
from utilities.media_write_lock import media_write_lock
args.config_dir = sys.argv[2]
with media_write_lock(sys.argv[1]):
    os._exit(31)
"""
    child = subprocess.run([sys.executable, '-c', script, str(video), args.config_dir],
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 31, child.stderr
    assert media_write_state(str(video)).active
    states_before_inputs = []
    capture_inputs = planner.capture_inputs

    def capture_after_repair(current):
        states_before_inputs.append(media_write_state(current['video_path']).active)
        return capture_inputs(current)

    monkeypatch.setattr(planner, 'capture_inputs', capture_after_repair)
    for _ in range(2):
        assert _preview(library)['status'] == 'ready'
        assert not media_write_state(str(video)).active
    assert states_before_inputs == [False, False]
