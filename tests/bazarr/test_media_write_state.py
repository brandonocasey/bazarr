import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.bazarr.test_media_write_lock import guard
from utilities import media_write_lock as coordination


def test_writer_state_preserves_nested_owner_and_detects_other_threads(guard, tmp_path):
    video = str(tmp_path / 'Film ü; v1.2.mkv')
    before = coordination.media_write_state(video)
    with coordination.media_write_lock(video) as previous:
        assert previous == before
        owned = coordination.media_write_state(video)
        assert not owned.active
        with coordination.media_write_lock(video) as nested:
            assert nested == owned
        with ThreadPoolExecutor(max_workers=1) as pool:
            concurrent = pool.submit(coordination.media_write_state, video).result(timeout=5)
        assert concurrent.active
        assert concurrent.generation == owned.generation
    finished = coordination.media_write_state(video)
    assert not finished.active
    assert finished != before
    assert finished != owned


def test_writer_state_changes_across_processes_without_replacing_or_growing_lock_file(guard, tmp_path):
    from app.get_args import args

    video = str(tmp_path / 'Film.mkv')
    with coordination.media_write_lock(video):
        pass
    lock_path = next((Path(args.config_dir) / 'media_write_locks').glob('*.lock'))
    initial = lock_path.stat()
    before = coordination.media_write_state(video)
    script = '''
import os, sys
os.environ['NO_CLI'] = 'true'
import bazarr.app.libs
from app.get_args import args
from utilities.media_write_lock import media_write_lock
args.config_dir = sys.argv[1]
for _ in range(10):
    with media_write_lock(sys.argv[2]):
        pass
'''
    environment = dict(os.environ)
    environment['PYTHONPATH'] = os.pathsep.join((str(Path.cwd()), str(Path.cwd() / 'bazarr')))
    result = subprocess.run([sys.executable, '-c', script, args.config_dir, video],
                            env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    after = coordination.media_write_state(video)
    assert after != before
    assert not after.active
    assert lock_path.stat().st_size == initial.st_size
    assert lock_path.stat().st_ino == initial.st_ino


@pytest.mark.parametrize('damage', ['truncated', 'invalid_active', 'oversized'])
def test_invalid_writer_state_cannot_bind_selection_or_start_writer(guard, tmp_path, damage):
    from app.get_args import args

    video = str(tmp_path / 'Film.mkv')
    with coordination.media_write_lock(video):
        pass
    lock_path = next((Path(args.config_dir) / 'media_write_locks').glob('*.lock'))
    valid = lock_path.read_bytes()
    malformed = {'truncated': valid[:-1], 'invalid_active': valid[:-1] + b'X', 'oversized': valid + b'X'}
    lock_path.write_bytes(malformed[damage])
    with pytest.raises(OSError):
        coordination.media_write_state(video)
    with pytest.raises(OSError):
        with coordination.media_write_lock(video):
            pytest.fail('writer started with malformed coordination state')


def test_failed_generation_flush_preserves_original_writer_exception(guard, tmp_path, monkeypatch):
    original = os.fsync
    calls = []

    def flush(descriptor):
        calls.append(descriptor)
        if len(calls) == 2:
            raise OSError('generation flush failed')
        return original(descriptor)

    monkeypatch.setattr(os, 'fsync', flush)
    with pytest.raises(ValueError, match='writer failed'):
        with coordination.media_write_lock(str(tmp_path / 'Film.mkv')):
            raise ValueError('writer failed')
