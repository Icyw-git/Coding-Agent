"""审批的作用域：一次 / 定时 / 有限次数 / 到期失效。

为什么需要
----------
现在的会话白名单是**一个 set**：``session_id|tool:klass``。批准一次 ``rm``，
这个会话里所有同类 ``rm`` 就永久免审，直到手动 ``/revoke``。

Muse 的做法是审批必须是**能力（capability）而不是建议**：

> Approvals ... are strict capabilities, not conversational suggestions. They're
> bound to the particular connector/destination and use case. Muse has support
> for obtaining **one-time, session-scoped, task-scoped, time-bounded, or
> perpetual** permission.

> Approval should be bound to the specific proposed action, including its target
> and material parameters. **If the recipient, amount, or affected resource
> changes afterward, require a new decision.**

我们的缺口：批一次 → 同会话全部永久免审，**没有时限、没有次数**。

四个作用域
----------
- ``once``    ：只这一次，用完即失效（默认）
- ``times``  ：还能用 N 次
- ``until``  ：到期时间（会话最长寿命上限）
- ``session``：整个会话（旧的默认行为，保留为显式选择）

为什么 ``once`` 是默认
----------------------
用户点"同意"时心里想的是**眼前这一条**，不是"以后同类都免了"。把默认从
``session`` 改成 ``once`` 是最重要的一个改动——它让"多批一次"成为默认代价，
而不是"少批一次"。

兼容
----
``HARNESS_APPROVAL_SCOPE=session`` 可恢复旧的默认行为。旧的持久记录（纯 set）
不迁移：进程内的白名单本来就是易失的，重启后重新问一次更安全。
"""
from __future__ import annotations

import os
import time

#: 作用域常量。
ONCE = 'once'
TIMES = 'times'
UNTIL = 'until'
SESSION = 'session'

SCOPES = (ONCE, TIMES, UNTIL, SESSION)

#: 默认作用域。``once`` 是刻意的选择（见模块 docstring）。
DEFAULT_SCOPE = ONCE

ENV_SCOPE = 'HARNESS_APPROVAL_SCOPE'

#: 会话作用域的硬上限（12 小时）。Muse 的 perpetual 是产品级的信任积累，
#: 我们不做：一个会话跑一整天已经很长，再长的授权应该重新确认。
MAX_SESSION_S = 12 * 3600


def default_scope() -> str:
    """默认作用域：``once``，除非环境变量显式改成 ``session``。"""
    raw = os.getenv(ENV_SCOPE, DEFAULT_SCOPE).strip().lower()
    return raw if raw in SCOPES else DEFAULT_SCOPE


def describe(scope: str, *, times: int = 0, until: float = 0) -> str:
    """给审批卡片/回显用的一句话。"""
    if scope == ONCE:
        return '仅这一次'
    if scope == TIMES:
        return f'还能用 {max(0, int(times))} 次'
    if scope == UNTIL:
        left = max(0, int(until - time.time()))
        return f'还有 {left // 60} 分钟到期'
    if scope == SESSION:
        return '本会话内同类'
    return str(scope)


def make(scope: str = None, *, times: int = 0,
         until: float = 0, now: float = None) -> dict:
    """构造一条授权记录。

    参数越界一律收敛而不是报错：授权记录是审批路径上的东西，为参数问题让一次
    已经批准的调用失败，得不偿失。
    """
    stamp = time.time() if now is None else now
    kind = (scope or default_scope()).strip().lower()
    if kind not in SCOPES:
        kind = DEFAULT_SCOPE
    if kind == TIMES:
        remaining = max(1, int(times or 1))
    else:
        remaining = 0
    if kind == UNTIL:
        deadline = float(until) if until else stamp + MAX_SESSION_S
        # 不允许超长：到期时间必须落在上限内（含已过期的边界，让 is_active 判死）
        deadline = min(deadline, stamp + MAX_SESSION_S)
    else:
        deadline = 0.0
    return {'scope': kind, 'remaining': remaining,
            'expires_at': deadline, 'granted_at': stamp}


def is_active(record: dict, now: float = None) -> bool:
    """这条授权还能不能用。"""
    if not isinstance(record, dict):
        return False
    stamp = time.time() if now is None else now
    kind = str(record.get('scope') or '')
    if kind not in SCOPES:
        return False
    if kind == ONCE:
        return True          # 由 consume() 决定是否已用完
    if kind == TIMES:
        return int(record.get('remaining') or 0) > 0
    if kind == UNTIL:
        deadline = record.get('expires_at') or 0
        return float(deadline) > stamp
    if kind == SESSION:
        # 会话档同样有硬上限：不让"本会话"变成无限期。
        granted = float(record.get('granted_at') or 0)
        return stamp - granted < MAX_SESSION_S
    return False


def consume(record: dict) -> dict | None:
    """用掉一次授权，返回更新后的记录；已耗尽返回 None。"""
    if not is_active(record):
        return None
    kind = str(record.get('scope') or '')
    if kind == ONCE:
        return None
    if kind == TIMES:
        remaining = int(record.get('remaining') or 0) - 1
        if remaining <= 0:
            return None
        return {**record, 'remaining': remaining}
    return record
