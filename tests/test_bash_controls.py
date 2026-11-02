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


# ---------- 并行调度：无害重定向与 cd 不该挡住只读判定 ----------

def test_fd_duplication_is_not_a_write():
    """`2>&1` 是"错误输出转到标准输出"，不写任何文件。

    实测：451 条真实 bash 命令里几乎每条都以 `cd … && … 2>&1` 结尾，而
    `_looks_read_only` 只要看到 `>` 就判非只读，于是整批 bash 只能串行。
    """
    from policy_bash import is_readonly_command

    for command in (
        'git diff HEAD -- a.py 2>&1',
        'cd /d repo && git diff HEAD -- a.py 2>&1',
        'git status 1>&2',
        'dir /b src 2>nul',
        'ls 2>/dev/null',
    ):
        assert is_readonly_command(command), command


def test_real_writes_are_still_rejected():
    """别放水：真写文件的重定向仍然非只读。"""
    from policy_bash import is_readonly_command

    for command in (
        'git diff > out.txt',
        'dir /b src > list.txt',
        'echo hi >> log.txt',
        'git status 2> errors.txt',      # 写一个真实的错误日志文件
        'type a.txt > b.txt 2>&1',
    ):
        assert not is_readonly_command(command), command


def test_cd_is_read_only_but_does_not_launder_writes():
    """`cd` 只改进程自己的工作目录；但它不能把后面的写命令洗成只读。"""
    from policy_bash import is_readonly_command

    assert is_readonly_command(r'cd /d C:\work')
    assert is_readonly_command('cd repo && dir /b src')
    assert not is_readonly_command('cd repo && rm -rf src')
    assert not is_readonly_command('cd repo && git push')


def test_two_read_only_bash_commands_run_in_parallel():
    """验收：这种最常见的形态（两条 cd 前缀的只读命令）应当并行。"""
    from types import SimpleNamespace

    import tool_dispatch

    def call(call_id, command):
        return SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(name='bash', arguments=json.dumps({'command': command})))

    batch = [call('c1', 'cd /d repo && git status --short 2>&1'),
             call('c2', 'cd /d repo && git log --oneline -5 2>&1')]
    commands = tool_dispatch._batch_bash_commands(batch)
    from policy_bash import is_readonly_command

    assert commands is not None
    assert all(is_readonly_command(c) for c in commands)
