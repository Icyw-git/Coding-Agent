"""跨进程单回合协调：同一时刻只允许一个 agent 回合执行。

阶段 0 的进程级状态（turn_control 取消位、METRICS、permission_gate.use_ui）还没有
按会话隔离，所以入口之间先保守串行：TUI worker、HTTP ``runtime.send``、ANSI REPL、
cron worker 和 headless 共用同一把 OS 文件锁。竞争时立即失败（``TurnBusy``），不排队
等待一个可能很长的模型调用。

锁文件在 ``<sessions_dir>/../locks/agent-turn.lock``；进程退出时由 OS 释放，残留的
锁文件不代表仍被持有，因此不做 PID 存活推断。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

from file_lock import FileLockTimeout, exclusive_file_lock

DEFAULT_TIMEOUT = 0.0   # 0 = 竞争立即失败
LOCK_SUBDIR = 'locks'
LOCK_NAME = 'agent-turn.lock'

_STATE = threading.RLock()
_DEPTH = 0
_OWNER: int | None = None
_UNLOCK = None   # 最外层 OS 锁的 context manager


class TurnBusy(RuntimeError):
    """另一个入口正在执行回合。"""


class TurnSlot:
    """已持有的回合槽；release() 幂等，可从任意线程调用（TUI 会转交给 worker）。"""

    def __init__(self, release):
        self._release = release
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._release()

    def __enter__(self) -> 'TurnSlot':
        return self

    def __exit__(self, *exc_info) -> bool:
        self.release()
        return False


def lock_path(base) -> Path:
    """回合锁路径；``base`` 通常是 ``SESSIONS_DIR.parent``（即 .cache）。"""
    return Path(base) / LOCK_SUBDIR / LOCK_NAME


def acquire(base, *, timeout: float = DEFAULT_TIMEOUT) -> TurnSlot:
    """占用全局回合槽；竞争时抛 ``TurnBusy``。同线程重入只增加嵌套计数。"""
    global _DEPTH, _OWNER, _UNLOCK
    ident = threading.get_ident()
    with _STATE:
        if _DEPTH:
            if _OWNER != ident:
                raise TurnBusy('另一个线程正在执行回合')
            _DEPTH += 1

            def release_nested() -> None:
                global _DEPTH
                with _STATE:
                    _DEPTH = max(0, _DEPTH - 1)

            return TurnSlot(release_nested)

        manager = exclusive_file_lock(lock_path(base), timeout=timeout)
        try:
            manager.__enter__()
        except FileLockTimeout as exc:
            raise TurnBusy('另一个入口正在执行回合') from exc
        _DEPTH = 1
        _OWNER = ident
        _UNLOCK = manager

        def release_all() -> None:
            global _DEPTH, _OWNER, _UNLOCK
            with _STATE:
                _DEPTH = 0
                _OWNER = None
                manager_to_close = _UNLOCK
                _UNLOCK = None
            if manager_to_close is not None:
                manager_to_close.__exit__(None, None, None)

        return TurnSlot(release_all)


@contextmanager
def turn_slot(base, *, timeout: float = DEFAULT_TIMEOUT):
    slot = acquire(base, timeout=timeout)
    try:
        yield slot
    finally:
        slot.release()


def held() -> bool:
    """当前进程是否持有回合槽（诊断/测试用）。"""
    with _STATE:
        return _DEPTH > 0


def reset_for_tests() -> None:
    """清空进程级的回合槽状态（仅测试用）。

    `_DEPTH` / `_OWNER` / `_UNLOCK` 是模块级全局：某个用例如果没走到 release
    （异常、提前 return，或者只是被 Ctrl-C 打断），残留的 `_DEPTH > 0` 会让
    **下一个用例**在 acquire 时命中「同线程重入」分支——它于是拿到一个空槽，
    回合根本没跑，断言就随机失败。

    conftest 里的 autouse fixture 会调用这里，把每个用例的起点拉回干净状态。
    """
    global _DEPTH, _OWNER, _UNLOCK
    with _STATE:
        manager = _UNLOCK
        _DEPTH = 0
        _OWNER = None
        _UNLOCK = None
    if manager is not None:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            pass
