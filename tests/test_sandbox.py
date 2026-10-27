"""OS 沙箱后端的单元测试与 Windows 集成测试。

集成用例在 pytest 的临时工作区（C: 用户目录）上真实打 Low 完整性标签，
命令只用 cmd 内建/native 工具：Git Bash（MSYS）的 ls/grep/bash 在 Low IL 下
无法初始化共享内存，属于已知限制，不作为沙箱故障处理。
"""
import io
import os
import subprocess
import tempfile
import time
from types import SimpleNamespace

import pytest

import sandbox
import tools
from task_manager import TaskManager

WINDOWS = pytest.mark.skipif(os.name != 'nt', reason='Low-IL 后端仅支持 Windows')


@pytest.fixture(autouse=True)
def _clean_sandbox_state():
    sandbox.reset_cache()
    yield
    sandbox.reset_cache()


# ============================================================
# 配置与回退
# ============================================================

def test_configured_mode_defaults_to_off():
    """默认 off：仓库作为工作区时打标有真实副作用，不能默认开启。

    见 sandbox.py 顶部「为什么没有默认开启」。
    """
    assert sandbox.configured_mode({}) == 'off'
    assert sandbox.configured_mode({'HARNESS_OS_SANDBOX': ' AUTO '}) == 'auto'
    assert sandbox.configured_mode({'HARNESS_OS_SANDBOX': 'require'}) == 'require'


def test_unknown_mode_is_off_not_a_silent_upgrade(capsys):
    assert sandbox.configured_mode({'HARNESS_OS_SANDBOX': 'on'}) == 'off'
    assert 'HARNESS_OS_SANDBOX' in capsys.readouterr().err


def test_off_mode_uses_plain_popen(monkeypatch):
    calls = []
    fake = object()

    def popen(*args, **kwargs):
        calls.append((args, kwargs))
        return fake

    monkeypatch.setenv('HARNESS_OS_SANDBOX', 'off')
    monkeypatch.setattr(sandbox.subprocess, 'Popen', popen)
    out = io.BytesIO()
    assert sandbox.spawn_shell('echo hi', cwd='.', stdout=out) is fake
    assert calls[0][0][0] == 'echo hi'
    assert calls[0][1]['shell'] is True
    assert calls[0][1]['stderr'] is subprocess.STDOUT


def test_require_rejects_when_the_backend_is_missing(monkeypatch):
    monkeypatch.setenv('HARNESS_OS_SANDBOX', 'require')
    monkeypatch.setattr(sandbox, '_WINDOWS', False)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.spawn_shell('echo hi', cwd='.', stdout=io.BytesIO())


@pytest.mark.parametrize('error', [
    sandbox.SandboxUnavailable('no label'),
    OSError(5, 'access denied'),
])
def test_auto_falls_back_when_setup_fails(monkeypatch, error):
    fake = object()
    monkeypatch.setenv('HARNESS_OS_SANDBOX', 'auto')
    monkeypatch.setattr(sandbox, '_WINDOWS', True)

    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(sandbox, '_spawn_sandboxed', unavailable)
    monkeypatch.setattr(sandbox.subprocess, 'Popen', lambda *a, **k: fake)
    assert sandbox.spawn_shell('echo hi', cwd='.', stdout=io.BytesIO()) is fake


# ============================================================
# Windows 集成：真实创建 Low 令牌与标签
# ============================================================

@pytest.fixture()
def sandbox_ws(tmp_path, monkeypatch):
    ws = tmp_path / 'ws'
    outside = tmp_path / 'outside'
    ws.mkdir()
    outside.mkdir()
    (outside / 'secret.txt').write_text('secret-ok', encoding='utf-8')
    monkeypatch.setenv('HARNESS_OS_SANDBOX', 'require')
    ready, reason = sandbox._ensure_workspace(ws.resolve())
    if not ready:
        pytest.skip(f'无法建立 Low 完整性工作区：{reason}')
    return SimpleNamespace(ws=ws, outside=outside, tmp_path=tmp_path)


def _spawn(ws, command):
    out = tempfile.TemporaryFile()
    proc = sandbox.spawn_shell(command, cwd=str(ws), stdout=out,
                               workspace=str(ws))
    return proc, out


def _run(ws, command, timeout=30):
    proc, out = _spawn(ws, command)
    try:
        code = proc.wait(timeout)
        out.seek(0)
        return code, out.read().decode('utf-8', errors='replace')
    finally:
        proc.close()
        out.close()


@WINDOWS
def test_workspace_writes_succeed_and_outside_writes_are_denied(sandbox_ws):
    ws, outside = sandbox_ws.ws, sandbox_ws.outside
    code, _ = _run(ws, 'echo inside> inside.txt')
    assert code == 0
    assert (ws / 'inside.txt').read_text(encoding='utf-8').strip() == 'inside'

    blocked = outside / 'blocked.txt'
    code, _ = _run(ws, f'echo no> "{blocked}"')
    assert code != 0
    assert not blocked.exists()


@WINDOWS
def test_reads_outside_the_workspace_still_work(sandbox_ws):
    ws, outside = sandbox_ws.ws, sandbox_ws.outside
    code, output = _run(ws, f'type "{outside / "secret.txt"}"')
    assert code == 0
    assert 'secret-ok' in output


@WINDOWS
def test_cmd_redirection_and_pipes_survive(sandbox_ws):
    ws = sandbox_ws.ws
    assert _run(ws, 'echo hi 2>nul')[0] == 0
    code, output = _run(ws, 'echo abc | findstr abc')
    assert code == 0 and 'abc' in output


@WINDOWS
def test_temp_points_inside_the_workspace(sandbox_ws):
    ws = sandbox_ws.ws
    code, output = _run(ws, 'echo %TEMP%')
    assert code == 0
    expected = str((ws / '.cache' / 'sandbox-tmp').resolve()).casefold()
    assert expected in output.casefold()


@WINDOWS
def test_timeout_then_kill_tree(sandbox_ws):
    proc, out = _spawn(sandbox_ws.ws, 'ping -n 10 127.0.0.1 >nul')
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(0.5)
        proc.kill_tree()
        assert proc.poll() is not None
    finally:
        proc.close()
        out.close()


@WINDOWS
def test_run_bash_uses_the_sandbox(sandbox_ws, monkeypatch):
    ws = sandbox_ws.ws
    monkeypatch.setattr(tools, '_h', lambda: SimpleNamespace(get_workdir=lambda: ws))
    tools.run_bash('echo sandboxed> runbash.txt')
    assert (ws / 'runbash.txt').read_text(encoding='utf-8').strip() == 'sandboxed'

    outside = sandbox_ws.tmp_path / 'runbash-outside.txt'
    tools.run_bash(f'echo no> "{outside}"')
    assert not outside.exists()


@WINDOWS
def test_run_bash_fails_closed_in_require_mode(sandbox_ws, monkeypatch):
    ws = sandbox_ws.ws
    monkeypatch.setattr(sandbox, '_prepare_workspace', lambda path: (False, '测试不可用'))
    sandbox.reset_cache()
    monkeypatch.setattr(tools, '_h', lambda: SimpleNamespace(get_workdir=lambda: ws))
    result = tools.run_bash('echo x> should-not-exist.txt')
    assert result.startswith('Error:') and '沙箱' in result
    assert not (ws / 'should-not-exist.txt').exists()


@WINDOWS
def test_task_manager_uses_the_sandbox(sandbox_ws, tmp_path):
    ws = sandbox_ws.ws
    manager = TaskManager(tmp_path / 'tasks', allowed_root=ws)
    try:
        record = manager.create('echo task-ok> task.txt')
        for _ in range(100):
            record = manager.get(record.id)
            if record.status in ('succeeded', 'failed'):
                break
            time.sleep(0.05)
        assert record.status == 'succeeded', record.detail
        assert (ws / 'task.txt').read_text(encoding='utf-8').strip() == 'task-ok'

        outside = ws.parent / 'task-outside.txt'
        record = manager.create(f'echo no> "{outside}"')
        for _ in range(100):
            record = manager.get(record.id)
            if record.status in ('succeeded', 'failed'):
                break
            time.sleep(0.05)
        assert record.status == 'failed'
        assert not outside.exists()
    finally:
        manager.close()


def test_environment_block_is_cached(tmp_path):
    """环境块重复构建占每次 spawn Python 侧开销的 90%，必须缓存。"""
    import sandbox

    sandbox.reset_cache()
    first = sandbox._environment_block(tmp_path)
    second = sandbox._environment_block(tmp_path)
    assert first is second        # 同一对象 = 命中缓存


def test_environment_block_cache_invalidates_on_env_change(tmp_path, monkeypatch):
    """改了环境变量必须重建：否则沙箱里看不到新值。"""
    import sandbox

    sandbox.reset_cache()
    first = sandbox._environment_block(tmp_path)
    monkeypatch.setenv('HARNESS_CACHE_PROBE', 'yes')
    second = sandbox._environment_block(tmp_path)
    assert first is not second
    assert 'HARNESS_CACHE_PROBE=yes' in second[:]


def test_environment_block_overrides_temp(tmp_path):
    """TEMP/TMP/TMPDIR 必须指向沙箱临时目录，否则命令写临时文件会失败。"""
    import sandbox

    sandbox.reset_cache()
    block = sandbox._environment_block(tmp_path)[:]
    for name in ('TEMP', 'TMP', 'TMPDIR'):
        assert f'{name}={tmp_path}' in block


def test_environment_block_is_sorted_case_insensitively(tmp_path):
    """Windows 要求环境块按大小写不敏感排序，否则 CreateProcess 行为未定义。"""
    import sandbox

    sandbox.reset_cache()
    block = sandbox._environment_block(tmp_path)[:]
    names = [entry.split('=', 1)[0] for entry in block.split(chr(0)) if '=' in entry]
    assert names == sorted(names, key=str.upper)


def test_reset_cache_clears_environment_block_cache(tmp_path):
    import sandbox

    sandbox._environment_block(tmp_path)
    assert sandbox._ENV_BLOCK_CACHE
    sandbox.reset_cache()
    assert not sandbox._ENV_BLOCK_CACHE
