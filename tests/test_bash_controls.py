"""bash 的前台/后台控制：超时、cwd、显式后台路由、输出等待与取消。"""
import json
import sys
import time
from types import SimpleNamespace

import pytest

import background_task
import tools


@pytest.fixture(autouse=True)
def clean_background(monkeypatch):
    monkeypatch.setenv('HARNESS_SANDBOX_MODE', 'danger-full-access')

    def reset():
        with background_task.background_lock:
            background_task.background_tasks.clear()
            background_task.background_results.clear()
            background_task.background_finished.clear()
            background_task.background_procs.clear()
            background_task._bg_counter = 0

    reset()
    yield
    reset()


def _start(name, args, registry):
    call = SimpleNamespace(
        id='call-1',
        function=SimpleNamespace(name=name, arguments=json.dumps(args)))
    return background_task.start_background_task(call, registry)


def _wait_for_proc(bg_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with background_task.background_lock:
            if bg_id in background_task.background_procs:
                return True
        time.sleep(0.01)
    return False


def test_bash_explicit_timeout_bounds_execution(tmp_path):
    started = time.monotonic()
    result = tools.run_bash(
        f'"{sys.executable}" -c "import time; time.sleep(5)"',
        cwd=str(tmp_path), timeout=0.3)
    assert 'timeout' in result.lower()
    assert time.monotonic() - started < 4


def test_bash_rejects_invalid_timeout(tmp_path):
    result = tools.run_bash('echo hi', cwd=str(tmp_path), timeout=0)
    assert result.startswith('Error:')
    assert 'timeout' in result.lower()

    result = tools.run_bash('echo hi', cwd=str(tmp_path), timeout=99999)
    assert result.startswith('Error:')


def test_bash_rejects_missing_cwd(tmp_path):
    missing = tmp_path / 'nope'
    result = tools.run_bash('echo hi', cwd=str(missing))
    assert 'cwd does not exist' in result


def test_bash_runs_in_requested_cwd(tmp_path):
    subdir = tmp_path / 'nested'
    subdir.mkdir()
    result = tools.run_bash(
        f'"{sys.executable}" -c "import os; print(os.getcwd())"',
        cwd=str(subdir))
    assert str(subdir.resolve()) in result


def test_should_run_background_respects_explicit_flag():
    assert background_task.should_run_background(
        'bash', {'command': 'echo hi'}) is False
    assert background_task.should_run_background(
        'bash', {'command': 'pytest tests'}) is True
    assert background_task.should_run_background(
        'bash', {'command': 'pytest tests', 'run_in_background': False}) is False
    assert background_task.should_run_background(
        'bash', {'command': 'echo hi', 'run_in_background': True}) is True


def test_background_output_waits_for_completion():
    def slow_ok(**_kwargs):
        time.sleep(0.2)
        return 'finished-output'

    bg_id = _start('slow', {}, {'slow': slow_ok})
    assert 'running' in tools.run_background_output(bg_id)
    result = tools.run_background_output(bg_id, wait=True, timeout=3)
    assert 'completed' in result
    assert 'finished-output' in result


def test_background_output_survives_collection():
    bg_id = _start('quick', {}, {'quick': lambda **_kwargs: 'payload'})
    snap = background_task.wait_background(bg_id, 3)
    assert snap['status'] == 'completed'

    notifications = background_task.collect_background_results()
    assert any(bg_id in item for item in notifications)

    result = tools.run_background_output(bg_id)
    assert 'payload' in result


def test_background_output_rejects_unknown_id():
    assert tools.run_background_output('bg_9999').startswith('Error:')


def test_kill_background_terminates_running_process(tmp_path):
    args = {
        'command': f'"{sys.executable}" -c "import time; time.sleep(30)"',
        'cwd': str(tmp_path),
        'run_in_background': True,
    }
    bg_id = _start('bash', args, {'bash': tools.run_bash})
    assert _wait_for_proc(bg_id), 'background process was not registered'

    result = tools.run_kill_background(bg_id)
    assert 'cancel requested' in result

    snap = background_task.wait_background(bg_id, 5)
    assert snap['status'] == 'cancelled'
    assert tools.run_kill_background('bg_9999').startswith('Error:')
