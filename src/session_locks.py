"""跨进程会话写锁：同一会话的写入与元数据变更串行化。

全局回合槽（``turn_coordinator``）保证同一时刻只有一个回合在跑，但它不阻止另一个
进程同时 rename / 切 workspace / fork / 归档同一会话。这里给每个会话一把 OS 文件锁：

- 回合持有它覆盖整轮读写（``AgentRuntime.send``、TUI worker）；
- 元数据操作（rename/workspace/fork/archive/delete）以非阻塞方式获取，失败抛
  ``SessionBusy``，由 API 映射成 409、TUI 提示；
- 回合后的 title 写入（housekeeping）用带超时的锁，避免偶发 ``Sequence`` 竞争。

锁文件在 ``<sessions_dir>/../locks/sessions/<id>.lock``，进程退出由 OS 释放。
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from file_lock import FileLockTimeout, exclusive_file_lock

LOCK_TIMEOUT = 0.0        # 元数据操作：竞争立即失败
WRITE_TIMEOUT = 10.0      # 后台 title 等单次写：允许短暂等待


class SessionBusy(RuntimeError):
    """会话正被另一个进程/入口写入。"""


def lock_path(sessions_dir, session_id: str) -> Path:
    safe = ''.join(ch if (ch.isalnum() or ch in '-_') else '_'
                   for ch in str(session_id))
    return Path(sessions_dir).parent / 'locks' / 'sessions' / f'{safe}.lock'


@contextmanager
def session_write(sessions_dir, session_id: str, *,
                  timeout: float = LOCK_TIMEOUT, action: str = '修改会话'):
    """持有会话写锁；获取失败抛 ``SessionBusy``。"""
    path = lock_path(sessions_dir, session_id)
    try:
        with exclusive_file_lock(path, timeout=timeout):
            yield
    except FileLockTimeout as exc:
        raise SessionBusy(f'{action}：会话正在执行任务，请稍后重试') from exc
