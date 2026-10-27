"""资源限额：环境解析、Job Object 真实约束、失败降级。"""
import os
import subprocess
import sys
import textwrap

import pytest

import limits

pytestmark = pytest.mark.skipif(os.name != 'nt',
                                reason='Job Objects are Windows-only')


# ---------- 环境解析 ----------

def test_defaults():
    parsed = limits.from_env({})
    assert parsed.memory_mb == limits.DEFAULT_MEMORY_MB
    assert parsed.active_processes == limits.DEFAULT_ACTIVE_PROCESSES
    assert parsed.cpu_seconds == 0
    assert parsed.enabled is True


@pytest.mark.parametrize('raw,expected', [
    ('512', 512),
    ('', limits.DEFAULT_MEMORY_MB),
    ('abc', limits.DEFAULT_MEMORY_MB),
    ('-1', limits.DEFAULT_MEMORY_MB),
    ('0', 0),
])
def test_memory_parsing(raw, expected):
    assert limits.from_env({limits.ENV_MEMORY_MB: raw}).memory_mb == expected


def test_all_disabled_is_possible():
    parsed = limits.from_env({limits.ENV_MEMORY_MB: '0',
                              limits.ENV_ACTIVE_PROCESSES: '0',
                              limits.ENV_CPU_SECONDS: '0'})
    assert parsed.enabled is False
    assert parsed.describe() == '资源限额：关'


def test_describe_mentions_active_limits():
    text = limits.Limits(memory_mb=512, active_processes=8,
                         cpu_seconds=30).describe()
    assert '512 MB' in text and '4096' not in text
    assert '进程数 8' in text
    assert 'CPU 30s' in text


# ---------- 真实约束 ----------

def _spawn(code: str):
    return subprocess.Popen([sys.executable, '-c', code],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_memory_limit_blocks_large_allocation():
    """512MB 限额下分配 1GB 必须失败——这是限额存在的理由。"""
    proc = _spawn('b = bytearray(1024*1024*1024); print("allocated")')
    job = limits.apply_to_process(
        proc, limits.Limits(memory_mb=512, active_processes=0, cpu_seconds=0))
    assert job is not None
    try:
        out, err = proc.communicate(timeout=120)
    finally:
        limits.close(job)
    assert proc.returncode != 0
    assert b'allocated' not in out
    assert b'MemoryError' in err


def test_small_allocation_passes_under_the_same_limit():
    """同一限额下 10MB 必须成功：限额不能误伤正常任务。"""
    proc = _spawn('b = bytearray(10*1024*1024); print("ok")')
    job = limits.apply_to_process(
        proc, limits.Limits(memory_mb=512, active_processes=0, cpu_seconds=0))
    assert job is not None
    try:
        out, err = proc.communicate(timeout=120)
    finally:
        limits.close(job)
    assert proc.returncode == 0
    assert b'ok' in out


def test_process_count_limit_blocks_fork_bomb():
    """进程数 5 时，起 20 个子进程必须中途失败。"""
    code = textwrap.dedent('''
        import subprocess, sys
        started = 0
        try:
            for _ in range(20):
                subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(2)'])
                started += 1
        except OSError:
            print('blocked at', started)
        else:
            print('started', started)
    ''')
    proc = _spawn(code)
    job = limits.apply_to_process(
        proc, limits.Limits(memory_mb=0, active_processes=5, cpu_seconds=0))
    assert job is not None
    try:
        out, _ = proc.communicate(timeout=120)
    finally:
        limits.close(job)
    assert b'blocked at' in out, out


def test_cpu_time_limit_terminates_busy_loop():
    """CPU 1s 限额下死循环必须被终止（不是靠墙钟超时）。"""
    proc = _spawn('x = 0\nwhile True:\n    x += 1')
    job = limits.apply_to_process(
        proc, limits.Limits(memory_mb=0, active_processes=0, cpu_seconds=1))
    assert job is not None
    try:
        proc.communicate(timeout=60)
    finally:
        limits.close(job)
    assert proc.returncode != 0


# ---------- 失败降级 ----------

def test_disabled_limits_raise_instead_of_returning_handle():
    with pytest.raises(limits.LimitError, match='disabled'):
        limits.create(limits.Limits(memory_mb=0, active_processes=0,
                                    cpu_seconds=0))


def test_apply_to_process_returns_none_when_unavailable(monkeypatch):
    """限额套不上时必须返回 None（命令照常跑），而不是抛错。"""
    monkeypatch.setattr(limits, 'create', lambda *a, **k: (_ for _ in ()).throw(
        limits.LimitError('boom')))
    proc = _spawn('print(1)')
    try:
        assert limits.apply_to_process(proc) is None
    finally:
        proc.communicate(timeout=60)


def test_status_line_reports_limits():
    line = limits.status_line(limits.Limits(memory_mb=256,
                                            active_processes=4,
                                            cpu_seconds=0))
    assert '资源限额' in line and '256 MB' in line


def test_close_process_tolerates_mock_process():
    """Mock 替换 Popen 时不能栈溢出。

    测试常把 `sandbox.subprocess.Popen` 换成 Mock。Mock 对任意属性都返回新
    Mock（不会 AttributeError），若把那个 Mock 当内核句柄传给 CloseHandle，
    ctypes 会一路递归到 stack overflow——整轮测试直接崩，且报错指向 threading
    而不是这里。close_process 因此只认 __dict__ 里的真实整数句柄。
    """
    from unittest.mock import Mock

    import process_utils

    proc = Mock()
    proc.wait.return_value = 0
    process_utils.close_process(proc)      # 不该抛，也不该崩
    process_utils.close_process(proc)      # 幂等


def test_close_process_closes_real_job_handle(tmp_path):
    """真实 job 句柄要被关掉并从 proc 上摘掉。"""
    import subprocess

    import process_utils

    proc = subprocess.Popen([sys.executable, '-c', 'print(1)'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    job = limits.create(limits.Limits(memory_mb=256, active_processes=4,
                                      cpu_seconds=0))
    limits.assign(job, proc.pid)
    proc._harness_job = job
    try:
        proc.communicate(timeout=60)
    finally:
        process_utils.close_process(proc)
    assert '_harness_job' not in proc.__dict__
