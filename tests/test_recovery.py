"""恢复语义的守卫：持锁进程死掉之后，后来者必须能继续。

为什么单独一个文件
------------------
`file_lock` 用的是**操作系统的建议锁**（Windows `msvcrt.locking` / POSIX `flock`），
进程退出由内核释放。这条性质是整个并发模型的地基：回合锁、会话写锁、授权台账锁
都靠它，一旦有人把它换成"看锁文件在不在"，崩溃一次就会永久卡住所有后续回合，
而现有测试全都会照常通过（它们都在同一进程里成对加解锁）。

这里用**真的另一个进程**去持有锁再被强杀，钉住"锁属于进程，不属于文件"。
失败时先看 `file_lock._try_acquire` 是否被改成了基于文件存在性的实现。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

import session_locks
import turn_coordinator
from file_lock import exclusive_file_lock

SRC = Path(__file__).resolve().parent.parent / 'src'


def _spawn_holder(code: str) -> subprocess.Popen:
    """起一个子进程执行 code，并等它把 READY 打到 stdout。"""
    script = (
        'import sys, time\n'
        f'sys.path.insert(0, {str(SRC)!r})\n'
        'from pathlib import Path\n'
        'import file_lock, session_locks, turn_coordinator\n'
        + code +
        '\nprint("READY", flush=True)\n'
        'while True:\n'
        '    time.sleep(0.05)\n'
    )
    proc = subprocess.Popen([sys.executable, '-c', script],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    assert proc.stdout is not None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        if 'READY' in line:
            return proc
    proc.kill()
    pytest.fail(f'holder 进程没能就绪：{proc.stderr.read()[:400] if proc.stderr else ""}')
    raise AssertionError('unreachable')


def _kill(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait(timeout=10)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def test_a_lock_file_on_disk_does_not_block_anybody(tmp_path):
    """锁文件存在 ≠ 有人持有。这条是"不要退回文件存在性实现"的底线。"""
    path = tmp_path / 'locks' / 'sessions' / 'x.lock'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'\0')

    started = time.monotonic()
    with exclusive_file_lock(path, timeout=2.0):
        pass
    assert time.monotonic() - started < 1.0


def test_turn_slot_is_released_when_the_holder_dies(tmp_path):
    """持有回合槽的进程被强杀 → 后来者立刻能拿到（不会留下"永久占用"）。"""
    proc = _spawn_holder(
        'HOLDER = turn_coordinator.acquire(Path(r"%s"))\n' % (tmp_path,))
    try:
        with pytest.raises(turn_coordinator.TurnBusy):
            turn_coordinator.acquire(tmp_path, timeout=0.3)
    finally:
        _kill(proc)

    started = time.monotonic()
    slot = turn_coordinator.acquire(tmp_path, timeout=5.0)
    try:
        assert time.monotonic() - started < 4.0
    finally:
        slot.release()


def test_session_write_lock_is_released_when_the_holder_dies(tmp_path):
    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    # 必须把上下文管理器存到模块级：临时对象被回收时会退出 with、把锁放掉
    proc = _spawn_holder(
        'HOLDER = session_locks.session_write(Path(r"%s"), "abc")\n'
        'HOLDER.__enter__()\n' % (sessions,))
    try:
        with pytest.raises(session_locks.SessionBusy):
            with session_locks.session_write(sessions, 'abc', timeout=0.3):
                pass
    finally:
        _kill(proc)

    started = time.monotonic()
    with session_locks.session_write(sessions, 'abc', timeout=5.0):
        pass
    assert time.monotonic() - started < 4.0
