"""TaskManager unit tests: state machine, logs, persistence, stop, isolation."""

import json
import time
from pathlib import Path

import pytest

from task_manager import TaskManager


def test_task_runs_to_success_and_persists(tmp_path):
    manager = TaskManager(tmp_path)
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


def test_failing_command_records_exit_code(tmp_path):
    manager = TaskManager(tmp_path)
    record = manager.create('python -c "raise SystemExit(3)"')
    for _ in range(100):
        record = manager.get(record.id)
        if record.status in ('succeeded', 'failed'):
            break
        time.sleep(0.05)
    assert record.status == 'failed'
    assert record.exit_code == 3


def test_stop_cancels_running_task(tmp_path):
    manager = TaskManager(tmp_path)
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


def test_reload_marks_orphaned_running_tasks_failed(tmp_path):
    manager = TaskManager(tmp_path)
    record = manager.create('python -c "import time; time.sleep(30)"')
    for _ in range(100):
        if manager.get(record.id).status == 'running':
            break
        time.sleep(0.05)

    # Simulate a server restart: a fresh manager reads the persisted record
    # whose OS process no longer exists.
    reloaded = TaskManager(tmp_path)
    survivor = reloaded.get(record.id)
    assert survivor.status == 'failed'
    assert 'restart' in survivor.detail
    assert survivor.exit_code is None


def test_cwd_outside_allowed_root_rejected(tmp_path):
    inside = tmp_path / 'workspace'
    inside.mkdir()
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    manager = TaskManager(tmp_path / 'tasks', allowed_root=inside)
    with pytest.raises(ValueError):
        manager.create('echo hi', cwd=str(outside))
    record = manager.create('echo hi', cwd=str(inside))
    assert record.cwd == str(inside)


def test_list_orders_newest_first(tmp_path):
    manager = TaskManager(tmp_path)
    first = manager.create('echo one')
    second = manager.create('echo two')
    listing = manager.list()
    assert [r.id for r in listing][:2] == [second.id, first.id]
