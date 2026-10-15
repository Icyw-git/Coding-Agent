"""TaskManager unit tests: state machine, logs, persistence, stop, isolation."""

import json
import time
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

from task_manager import TaskManager


@pytest.fixture
def manager_factory():
    managers = []
    def create(*args, **kwargs):
        manager = TaskManager(*args, **kwargs)
        managers.append(manager)
        return manager
    yield create
    for manager in reversed(managers):
        manager.close()


def test_task_runs_to_success_and_persists(tmp_path, manager_factory):
    manager = manager_factory(tmp_path)
    record = manager.create('python -c "print(42)"')

    for _ in range(100):
        record = manager.get(record.id)
        if record.status in ('succeeded', 'failed'):
            break
        time.sleep(0.05)

    assert record.status == 'succeeded', record.detail
    assert record.exit_code == 0
    assert record.started_at and record.ended_at
    assert '42' in manager.tail_logs(record.id)
    assert record.log_path and Path(record.log_path).exists()

    saved = json.loads((tmp_path / f'{record.id}.json').read_text(encoding='utf-8'))
    assert saved['status'] == 'succeeded'
    assert saved['exit_code'] == 0


def test_failing_command_records_exit_code(tmp_path, manager_factory):
    manager = manager_factory(tmp_path)
    record = manager.create('python -c "raise SystemExit(3)"')
    for _ in range(100):
        record = manager.get(record.id)
        if record.status in ('succeeded', 'failed'):
            break
        time.sleep(0.05)
    assert record.status == 'failed'
    assert record.exit_code == 3


def test_stop_cancels_running_task(tmp_path, manager_factory):
    manager = manager_factory(tmp_path)
    record = manager.create('python -c "import time; time.sleep(30)"')
    for _ in range(100):
        if manager.get(record.id).status == 'running':
            break
        time.sleep(0.05)
    assert manager.get(record.id).status == 'running'

    stopped = manager.stop(record.id)
    assert stopped.status == 'cancelled'
    assert stopped.ended_at is not None
    with pytest.raises(RuntimeError):
        manager.stop(record.id)   # already terminal → 409 at the API layer


def test_reload_marks_active_tasks_interrupted(tmp_path, manager_factory):
    from task_manager import TaskRecord

    record = TaskRecord('task_orphan', 'echo old', str(tmp_path), 'running',
                        time.time(), pid=12345)
    (tmp_path / 'task_orphan.json').write_text(json.dumps(asdict(record)))
    reloaded = manager_factory(tmp_path)
    survivor = reloaded.get(record.id)
    assert survivor.status == 'interrupted'
    assert 'restart' in survivor.detail
    assert survivor.exit_code is None
    assert survivor.ended_at is None


def test_cwd_outside_allowed_root_rejected(tmp_path, manager_factory):
    inside = tmp_path / 'workspace'
    inside.mkdir()
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    manager = manager_factory(tmp_path / 'tasks', allowed_root=inside)
    with pytest.raises(ValueError):
        manager.create('echo hi', cwd=str(outside))
    record = manager.create('echo hi', cwd=str(inside))
    assert record.cwd == str(inside)


def test_list_orders_newest_first(tmp_path, manager_factory):
    manager = manager_factory(tmp_path)
    first = manager.create('echo one')
    second = manager.create('echo two')
    listing = manager.list()
    assert [r.id for r in listing][:2] == [second.id, first.id]


def test_cancelled_queued_task_never_spawns(tmp_path, monkeypatch, manager_factory):
    from unittest.mock import Mock

    gate = threading.Event()
    original = TaskManager._run
    def delayed_run(self, record):
        gate.wait(5)
        original(self, record)
    monkeypatch.setattr(TaskManager, '_run', delayed_run)
    spawn = Mock(side_effect=AssertionError('cancelled task must not start'))
    monkeypatch.setattr('task_manager.subprocess.Popen', spawn)
    manager = manager_factory(tmp_path)
    record = manager.create('echo cancelled')
    try:
        manager.stop(record.id)
    finally:
        gate.set()
        manager.close()
    assert record.status == 'cancelled'
    spawn.assert_not_called()


def test_close_stops_process_and_flushes_logs(tmp_path, manager_factory):
    manager = manager_factory(tmp_path)
    record = manager.create('python -c "import time; time.sleep(30)"')
    for _ in range(100):
        with manager._lock:
            proc = manager._procs.get(record.id)
        if proc is not None:
            break
        time.sleep(0.01)
    assert proc is not None
    manager.close()
    assert proc.poll() is not None
    assert record.status == 'cancelled'
    assert not manager._threads
    assert '[exit ' in manager.tail_logs(record.id)
    with pytest.raises(RuntimeError, match='closed'):
        manager.create('echo too late')


def test_posix_stop_signals_process_group(monkeypatch):
    import signal
    from types import SimpleNamespace
    from unittest.mock import Mock

    proc = Mock(pid=123)
    proc.poll.return_value = None
    killpg = Mock()
    monkeypatch.setattr('process_utils.os', SimpleNamespace(name='posix', killpg=killpg))
    TaskManager._kill_tree(proc)
    killpg.assert_called_once_with(123, getattr(signal, 'SIGKILL', 9))
    proc.wait.assert_called_once_with(timeout=10)


def test_failed_termination_does_not_claim_cancellation(tmp_path, monkeypatch, manager_factory):
    from unittest.mock import Mock
    from task_manager import TaskRecord

    manager = manager_factory(tmp_path)
    record = TaskRecord('task_stop_error', 'echo x', str(tmp_path), 'running', time.time())
    manager._tasks[record.id] = record
    manager._procs[record.id] = Mock()
    terminate = Mock(side_effect=RuntimeError('termination denied'))
    monkeypatch.setattr(manager, '_kill_tree', terminate)
    with pytest.raises(RuntimeError, match='termination denied'):
        manager.stop(record.id)
    assert record.status == 'running'
    assert record.ended_at is None
    # Synthetic record, no OS process was spawned.
    manager._tasks.clear()
    manager._procs.clear()
