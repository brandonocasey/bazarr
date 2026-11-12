import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from subtitles.cleanup import transactions


_CHILD_BOOTSTRAP = '''
import os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path.cwd()/'bazarr'))
os.environ['NO_CLI']='true'
import bazarr.app.libs
from subtitles.cleanup import transactions
'''


def _inputs(tmp_path):
    video = tmp_path / 'A movie #2.mkv'
    subtitle = tmp_path / 'A movie #2.eng.srt'
    replacement = tmp_path / 'new.mkv'
    video.write_bytes(b'original video and both subtitle streams')
    subtitle.write_bytes(b'lower ranked subtitles')
    replacement.write_bytes(b'video and the retained subtitle stream')
    identities = [transactions.fingerprint(str(path)) for path in (video, subtitle)]
    changes = [SimpleNamespace(original_path=str(video), staged_path=str(replacement)),
               SimpleNamespace(original_path=str(subtitle), staged_path=None)]
    return video, subtitle, replacement, identities, changes


def test_commit_and_restore_preserve_original_bytes_and_unrelated_paths(tmp_path):
    video, subtitle, replacement, identities, changes = _inputs(tmp_path)
    decoy = tmp_path / 'another library' / video.name
    decoy.parent.mkdir()
    decoy.write_bytes(b'unrelated video')
    directory = tmp_path / 'backups' / 'operation'
    journal = transactions.commit_changes(directory, changes, identities, lambda: None)
    assert journal['phase'] == 'complete'
    assert video.read_bytes() == replacement.read_bytes()
    assert not subtitle.exists()
    assert decoy.read_bytes() == b'unrelated video'
    assert all(Path(entry['backup']).read_bytes() for entry in journal['entries'])
    assert transactions.restore_transaction(directory)['phase'] == 'restored'
    assert video.read_bytes() == b'original video and both subtitle streams'
    assert subtitle.read_bytes() == b'lower ranked subtitles'
    assert decoy.read_bytes() == b'unrelated video'


def test_changed_preview_refuses_every_replacement(tmp_path):
    video, subtitle, _, identities, changes = _inputs(tmp_path)
    subtitle.write_bytes(b'later edit')
    with pytest.raises(ValueError, match='changed since'):
        transactions.commit_changes(tmp_path / 'backup', changes, identities, lambda: None)
    assert video.read_bytes() == b'original video and both subtitle streams'
    assert subtitle.read_bytes() == b'later edit'


def test_replacement_failure_rolls_back_prior_file(tmp_path, monkeypatch):
    video, subtitle, _, identities, changes = _inputs(tmp_path)
    original_unlink = os.unlink
    failed = False

    def fail_once(path, *args, **kwargs):
        nonlocal failed
        if str(path) == str(subtitle) and not failed:
            failed = True
            raise OSError('simulated filesystem failure')
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', fail_once)
    directory = tmp_path / 'backup'
    with pytest.raises(OSError, match='simulated filesystem failure'):
        transactions.commit_changes(directory, changes, identities, lambda: None)
    assert video.read_bytes() == b'original video and both subtitle streams'
    assert subtitle.read_bytes() == b'lower ranked subtitles'
    journal = transactions.read_journal(directory)
    assert journal['phase'] == 'rolled_back'
    assert all(not Path(entry['backup']).exists() for entry in journal['entries'])


def test_process_crash_between_replacement_and_journal_recovers(tmp_path):
    video, subtitle, _, _, _ = _inputs(tmp_path)
    directory = tmp_path / 'backup'
    script = _CHILD_BOOTSTRAP + '''
root=Path(sys.argv[1]);video=root/'A movie #2.mkv';sub=root/'A movie #2.eng.srt'
changes=[SimpleNamespace(original_path=str(video),staged_path=str(root/'new.mkv')),
         SimpleNamespace(original_path=str(sub),staged_path=None)]
identities=[transactions.fingerprint(str(p)) for p in (video,sub)]
replace=os.replace
def crash_after_replace(source,target):
    replace(source,target)
    if str(target)==str(video): os._exit(23)
os.replace=crash_after_replace
transactions.commit_changes(root/'backup',changes,identities,lambda:None)
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], timeout=30, capture_output=True, text=True)
    assert result.returncode == 23, result.stderr
    assert video.read_bytes() == b'video and the retained subtitle stream'
    recovered = transactions.recover_transaction(directory)
    assert recovered['phase'] == 'rolled_back'
    assert all(not Path(entry['backup']).exists() for entry in recovered['entries'])
    assert video.read_bytes() == b'original video and both subtitle streams'
    assert subtitle.read_bytes() == b'lower ranked subtitles'


@pytest.mark.parametrize('same_as_original', [False, True])
def test_restore_refuses_later_edit_without_changing_other_files(tmp_path, same_as_original):
    video, subtitle, _, identities, changes = _inputs(tmp_path)
    later = subtitle.read_bytes() if same_as_original else b'new subtitles downloaded later'
    directory = tmp_path / 'backup'
    transactions.commit_changes(directory, changes, identities, lambda: None)
    subtitle.write_bytes(later)
    with pytest.raises(ValueError, match='later changes'):
        transactions.restore_transaction(directory)
    assert video.read_bytes() == b'video and the retained subtitle stream'
    assert subtitle.read_bytes() == later


def test_restore_refuses_changed_backup_before_changing_outputs(tmp_path):
    video, subtitle, _, identities, changes = _inputs(tmp_path)
    directory = tmp_path / 'backup'
    journal = transactions.commit_changes(directory, changes, identities, lambda: None)
    Path(journal['entries'][1]['backup']).write_bytes(b'corrupted backup')
    with pytest.raises(ValueError, match='backup is missing or changed'):
        transactions.restore_transaction(directory)
    assert video.read_bytes() == b'video and the retained subtitle stream'
    assert not subtitle.exists()


def test_preexisting_staging_file_is_never_deleted(tmp_path):
    video, subtitle, _, identities, changes = _inputs(tmp_path)
    directory = tmp_path / 'backup'
    decoy = video.with_name('.bazarr-cleanup-backup-0.stage')
    decoy.write_bytes(b'preexisting unrelated file')
    with pytest.raises(ValueError, match='already exists'):
        transactions.commit_changes(directory, changes, identities, lambda: None)
    assert decoy.read_bytes() == b'preexisting unrelated file'
    assert video.read_bytes() == b'original video and both subtitle streams'
    assert subtitle.read_bytes() == b'lower ranked subtitles'


def test_unpreviewed_file_is_never_changed(tmp_path):
    video, subtitle, replacement, identities, _ = _inputs(tmp_path)
    decoy = tmp_path / 'unpreviewed.mkv'
    decoy.write_bytes(b'unrelated')
    changes = [SimpleNamespace(original_path=str(decoy), staged_path=str(replacement))]
    with pytest.raises(ValueError, match='not in the confirmed preview'):
        transactions.commit_changes(tmp_path / 'backup', changes, identities, lambda: None)
    assert decoy.read_bytes() == b'unrelated'
    assert video.exists() and subtitle.exists()


@pytest.mark.parametrize('kind', ['symlink', 'fifo'])
def test_nonregular_sources_are_rejected_without_blocking(tmp_path, kind):
    source = tmp_path / 'input'
    if kind == 'symlink':
        target = tmp_path / 'target'
        target.write_bytes(b'subtitle')
        source.symlink_to(target)
    elif hasattr(os, 'mkfifo'):
        os.mkfifo(source)
    else:
        pytest.skip('FIFO requires POSIX')
    with pytest.raises(ValueError, match='non-symlink|regular file'):
        transactions.fingerprint(str(source))


def test_restore_crash_finishes_from_durable_journal(tmp_path):
    video, subtitle, _, identities, changes = _inputs(tmp_path)
    directory = tmp_path / 'backup'
    journal = transactions.commit_changes(directory, changes, identities, lambda: None)
    journal['phase'] = 'restoring'
    (directory / 'journal.json').write_text(json.dumps(journal))
    assert transactions.recover_transaction(directory)['phase'] == 'restored'
    assert video.read_bytes() == b'original video and both subtitle streams'
    assert subtitle.read_bytes() == b'lower ranked subtitles'


@pytest.mark.parametrize('phase,filename', [('apply', 'video'), ('restore', 'video'), ('restore', 'subtitle')])
def test_copy_crash_recovers_owned_staging_and_preserves_decoys(tmp_path, phase, filename):
    video, subtitle, _, identities, changes = _inputs(tmp_path)
    directory = tmp_path / 'backup'
    if phase == 'restore':
        transactions.commit_changes(directory, changes, identities, lambda: None)
    decoy = tmp_path / '.bazarr-cleanup-other-operation.stage'
    decoy.write_bytes(b'Unrelated staging output')
    script = _CHILD_BOOTSTRAP + '''
import shutil
root=Path(sys.argv[1]);phase=sys.argv[2];filename=sys.argv[3]
video=root/'A movie #2.mkv';sub=root/'A movie #2.eng.srt'
source = str(root/'new.mkv')
if phase=='restore':
    journal=transactions.read_journal(root/'backup')
    original=video if filename=='video' else sub
    source=next(entry['backup'] for entry in journal['entries'] if entry['path']==str(original))
copy=shutil.copyfileobj
def crash_during_copy(reader,writer,*args,**kwargs):
    if str(reader.name)==source:
        writer.write(reader.read(8));writer.flush();os.fsync(writer.fileno());os._exit(24)
    return copy(reader,writer,*args,**kwargs)
shutil.copyfileobj=crash_during_copy
if phase=='restore':
    transactions.restore_transaction(root/'backup')
else:
    changes=[SimpleNamespace(original_path=str(video),staged_path=str(root/'new.mkv')),
             SimpleNamespace(original_path=str(sub),staged_path=None)]
    identities=[transactions.fingerprint(str(p)) for p in (video,sub)]
    transactions.commit_changes(root/'backup',changes,identities,lambda:None)
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path), phase, filename],
                            timeout=30, capture_output=True, text=True)
    assert result.returncode == 24, result.stderr
    recovered = transactions.recover_transaction(directory)
    assert recovered['phase'] == ('restored' if phase == 'restore' else 'rolled_back')
    assert video.read_bytes() == b'original video and both subtitle streams'
    assert subtitle.read_bytes() == b'lower ranked subtitles'
    assert list(tmp_path.glob('.bazarr-cleanup-*.stage')) == [decoy]
    assert decoy.read_bytes() == b'Unrelated staging output'


@pytest.mark.parametrize('cancelled', [False, True])
def test_rollback_releases_only_captured_backups(tmp_path, cancelled):
    video, subtitle, _, identities, changes = _inputs(tmp_path)
    directory = tmp_path / 'backup'
    unknown = directory / 'originals' / 'unrelated.mkv'

    def refuse():
        unknown.write_bytes(b'unrelated historical backup')
        raise KeyboardInterrupt('cancelled') if cancelled else ValueError('validation failed')

    with pytest.raises(KeyboardInterrupt if cancelled else ValueError):
        transactions.commit_changes(directory, changes, identities, refuse)
    journal = transactions.read_journal(directory)
    assert journal['phase'] == 'rolled_back'
    assert all(not Path(entry['backup']).exists() for entry in journal['entries'])
    assert unknown.read_bytes() == b'unrelated historical backup'
    assert video.read_bytes() == b'original video and both subtitle streams'
    assert subtitle.read_bytes() == b'lower ranked subtitles'
    assert transactions.recover_transaction(directory)['phase'] == 'rolled_back'


def test_rollback_cleanup_failure_preserves_original_error_and_retries(tmp_path, monkeypatch):
    _, _, _, identities, changes = _inputs(tmp_path)
    directory = tmp_path / 'backup'
    unlink = os.unlink

    def refuse_backup(path, *args, **kwargs):
        if directory / 'originals' in Path(path).parents:
            raise PermissionError('backup filesystem temporarily read only')
        return unlink(path, *args, **kwargs)

    def refuse_validation():
        raise ValueError('confirmed cancellation')

    with monkeypatch.context() as patch:
        patch.setattr(os, 'unlink', refuse_backup)
        with pytest.raises(ValueError, match='confirmed cancellation'):
            transactions.commit_changes(directory, changes, identities, refuse_validation)
    journal = transactions.read_journal(directory)
    assert journal['phase'] == 'rolled_back'
    assert all(Path(entry['backup']).exists() for entry in journal['entries'])
    transactions.recover_transaction(directory)
    assert all(not Path(entry['backup']).exists() for entry in journal['entries'])


@pytest.mark.parametrize('retained', ['historical', 'replaced', 'modified', 'original_changed', 'owner_changed', 'mtime_changed'])
def test_recovery_preserves_backups_without_current_ownership_and_original_proof(tmp_path, retained):
    video, subtitle, _, _, _ = _inputs(tmp_path)
    directory = tmp_path / 'backup'
    script = _CHILD_BOOTSTRAP + """
root=Path(sys.argv[1]);video=root/'A movie #2.mkv';sub=root/'A movie #2.eng.srt'
changes=[SimpleNamespace(original_path=str(video),staged_path=str(root/'new.mkv')),
         SimpleNamespace(original_path=str(sub),staged_path=None)]
identities=[transactions.fingerprint(str(p)) for p in (video,sub)]
transactions.commit_changes(root/'backup',changes,identities,lambda:os._exit(25))
"""
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], timeout=30, capture_output=True, text=True)
    assert result.returncode == 25, result.stderr
    journal = transactions.read_journal(directory)
    backup = Path(journal['entries'][0]['backup'])
    if retained == 'historical':
        for entry in journal['entries']:
            entry.pop('backup_owner', None)
    elif retained == 'replaced':
        replacement = backup.with_name('foreign-same-content')
        replacement.write_bytes(backup.read_bytes())
        replacement.replace(backup)
    elif retained == 'modified':
        backup.write_bytes(b'later backup edit')
    elif retained == 'original_changed':
        video.write_bytes(b'later video edit')
    elif retained == 'mtime_changed':
        info = video.stat()
        os.utime(video, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    else:
        video.chmod(0o600 if video.stat().st_mode & 0o777 != 0o600 else 0o640)
    (directory / 'journal.json').write_text(json.dumps(journal))
    assert transactions.recover_transaction(directory)['phase'] == 'rolled_back'
    assert transactions.recover_transaction(directory)['phase'] == 'rolled_back'
    assert backup.exists()
    if retained == 'modified':
        assert backup.read_bytes() == b'later backup edit'
    assert subtitle.read_bytes() == b'lower ranked subtitles'


def test_backup_copy_crash_releases_captured_complete_copy(tmp_path):
    video, subtitle, _, _, _ = _inputs(tmp_path)
    directory = tmp_path / 'backup'
    script = _CHILD_BOOTSTRAP + """
import shutil
root=Path(sys.argv[1]);video=root/'A movie #2.mkv';sub=root/'A movie #2.eng.srt'
copy=shutil.copyfileobj
def crash_after_backup(reader,writer,*args,**kwargs):
    copy(reader,writer,*args,**kwargs)
    writer.flush();os.fsync(writer.fileno());os._exit(26)
shutil.copyfileobj=crash_after_backup
changes=[SimpleNamespace(original_path=str(video),staged_path=str(root/'new.mkv')),
         SimpleNamespace(original_path=str(sub),staged_path=None)]
identities=[transactions.fingerprint(str(p)) for p in (video,sub)]
transactions.commit_changes(root/'backup',changes,identities,lambda:None)
"""
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], timeout=30, capture_output=True, text=True)
    assert result.returncode == 26, result.stderr
    journal = transactions.recover_transaction(directory)
    assert journal['phase'] == 'rolled_back'
    assert all(not Path(entry['backup']).exists() for entry in journal['entries'])
    assert video.read_bytes() == b'original video and both subtitle streams'
    assert subtitle.read_bytes() == b'lower ranked subtitles'


def test_metadata_permission_refusal_names_original_and_resolution(tmp_path, monkeypatch):
    video, subtitle, _, identities, changes = _inputs(tmp_path)
    directory = tmp_path / 'backup'
    chmod = os.chmod

    def refuse_metadata(path, *args, **kwargs):
        if directory / 'originals' in Path(path).parents:
            raise PermissionError('simulated permission refusal')
        return chmod(path, *args, **kwargs)

    monkeypatch.setattr(os, 'chmod', refuse_metadata)
    with pytest.raises(PermissionError) as error:
        transactions.commit_changes(directory, changes, identities, lambda: None)
    message = str(error.value)
    assert str(video) in message
    assert f'uid {identities[0].uid}' in message
    assert f'gid {identities[0].gid}' in message
    assert 'preserve' in message.lower()
    assert 'permission' in message.lower()
    assert video.read_bytes() == b'original video and both subtitle streams'
    assert subtitle.read_bytes() == b'lower ranked subtitles'


def test_public_backup_disposal_preserves_successful_and_historical_restore(tmp_path):
    video, subtitle, _, identities, changes = _inputs(tmp_path)
    directory = tmp_path / 'backup'
    journal = transactions.commit_changes(directory, changes, identities, lambda: None)
    transactions.discard_rollback_backups(directory)
    assert all(Path(entry['backup']).exists() for entry in journal['entries'])
    for entry in journal['entries']:
        entry.pop('backup_owner')
    (directory / 'journal.json').write_text(json.dumps(journal))
    assert transactions.restore_transaction(directory)['phase'] == 'restored'
    transactions.discard_rollback_backups(directory)
    assert all(Path(entry['backup']).exists() for entry in journal['entries'])
    assert video.read_bytes() == b'original video and both subtitle streams'
    assert subtitle.read_bytes() == b'lower ranked subtitles'


def test_public_backup_disposal_preserves_restored_copies(tmp_path):
    _, _, _, identities, changes = _inputs(tmp_path)
    directory = tmp_path / 'backup'
    transactions.commit_changes(directory, changes, identities, lambda: None)
    journal = transactions.restore_transaction(directory)
    transactions.discard_rollback_backups(directory)
    assert all(Path(entry['backup']).exists() for entry in journal['entries'])


def test_replacement_metadata_refusal_names_original(tmp_path, monkeypatch):
    video, _, _, identities, changes = _inputs(tmp_path)
    chmod = os.chmod

    def refuse_metadata(path, *args, **kwargs):
        if str(path).endswith('.stage'):
            raise PermissionError('replacement metadata refused')
        return chmod(path, *args, **kwargs)

    monkeypatch.setattr(os, 'chmod', refuse_metadata)
    with pytest.raises(PermissionError) as error:
        transactions.commit_changes(tmp_path / 'backup', changes, identities, lambda: None)
    assert str(video) in str(error.value)
    assert 'required uid' in str(error.value)
    assert video.read_bytes() == b'original video and both subtitle streams'


@pytest.mark.parametrize('backup_state', ['absent', 'historical', 'foreign'])
def test_settled_rollback_with_no_captured_copies_does_not_rehash_originals(tmp_path, monkeypatch, backup_state):
    _, _, _, identities, changes = _inputs(tmp_path)
    directory = tmp_path / 'backup'

    def refuse():
        raise ValueError('validation refused before replacement')

    with pytest.raises(ValueError, match='validation refused before replacement'):
        transactions.commit_changes(directory, changes, identities, refuse)
    journal = transactions.read_journal(directory)
    assert journal['phase'] == 'rolled_back'
    for entry in journal['entries']:
        backup = Path(entry['backup'])
        assert not backup.exists()
        if backup_state != 'absent':
            backup.write_bytes(b'foreign content')
        if backup_state == 'historical':
            entry.pop('backup_owner')
    (directory / 'journal.json').write_text(json.dumps(journal))
    reads = []
    fingerprint = transactions.fingerprint

    def record_read(path):
        reads.append(path)
        return fingerprint(path)

    monkeypatch.setattr(transactions, 'fingerprint', record_read)
    for _ in range(2):
        assert transactions.recover_transaction(directory)['phase'] == 'rolled_back'
    assert reads == []
    if backup_state != 'absent':
        assert all(Path(entry['backup']).read_bytes() == b'foreign content' for entry in journal['entries'])
