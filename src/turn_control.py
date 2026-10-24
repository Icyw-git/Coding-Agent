"""跨线程的回合状态：回合身份、取消请求与「回合进行中」标志。

输入线程在 Ctrl-C / Esc 时只置取消位，不把 KeyboardInterrupt 塞进消息队列；主循环
在流式回调里检查取消位并抛出 KeyboardInterrupt，让当前回合尽快收敛。

阶段 1 起每个回合有独立 token（turn_id/session_id/source），取消既支持旧的全局
request_cancel()（TUI/CLI），也支持按 token 定向取消（HTTP /interrupt）。token 登记在
进程内注册表里，供 /interrupt 按会话查找；全局单回合阶段最多只有一个活动 token。
"""
from __future__ import annotations

import threading
import uuid

_CANCEL = threading.Event()          # 兼容旧入口：未绑定 token 的取消请求
_ACTIVE = threading.Event()
_LOCK = threading.RLock()
_ACTIVE_TOKENS: dict[int, 'TurnToken'] = {}
_CURRENT = threading.local()


class TurnToken:
    """一次回合的取消令牌与身份（turn_id / session / 来源）。"""

    __slots__ = ('cancel', 'session_id', 'turn_id', 'source')

    def __init__(self, *, session_id: str = '', turn_id: str = '',
                 source: str = '') -> None:
        self.cancel = threading.Event()
        self.session_id = session_id or ''
        self.turn_id = turn_id or f't{uuid.uuid4().hex[:12]}'
        self.source = source or 'cli'

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def request_cancel(self) -> None:
        self.cancel.set()

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return (f'TurnToken(turn_id={self.turn_id!r}, '
                f'session={self.session_id!r}, source={self.source!r})')


def begin_turn(*, session_id: str = '', turn_id: str = '',
               source: str = 'cli') -> TurnToken:
    """开始一个回合并返回 token。

    ``_CANCEL`` 不在这里清除：用户在回合启动前按下的 Esc（``request_cancel()`` 时还没有
    活动 token）应该取消即将开始的回合；上一回合的取消位由 ``end_turn()`` 负责清掉，
    因此不会跨回合泄漏。
    """
    _ACTIVE.set()
    existing = getattr(_CURRENT, 'token', None)
    if existing is not None:
        # 同一线程重入（测试直接 begin_turn() 后调用 agent_loop）：复用同一个 token，
        # 保留已经发出的取消请求，只补全身份信息。
        if session_id:
            existing.session_id = session_id
        if turn_id:
            existing.turn_id = turn_id
        if source:
            existing.source = source
        return existing
    token = TurnToken(session_id=session_id, turn_id=turn_id, source=source)
    with _LOCK:
        _ACTIVE_TOKENS[id(token)] = token
    _CURRENT.token = token
    return token


def end_turn(token: TurnToken | None = None) -> None:
    token = token if token is not None else getattr(_CURRENT, 'token', None)
    with _LOCK:
        if token is not None:
            _ACTIVE_TOKENS.pop(id(token), None)
        if not _ACTIVE_TOKENS:
            _ACTIVE.clear()
    _CANCEL.clear()
    if getattr(_CURRENT, 'token', None) is token:
        _CURRENT.token = None


def active() -> bool:
    return _ACTIVE.is_set()


def current_token() -> TurnToken | None:
    return getattr(_CURRENT, 'token', None)


def first_token(session_id: str | None = None) -> TurnToken | None:
    """查看（不取消）活动 token；可按会话过滤。全局单回合时最多一个。"""
    with _LOCK:
        for token in _ACTIVE_TOKENS.values():
            if session_id is None or token.session_id == session_id:
                return token
    return None


def request_cancel(token: TurnToken | None = None) -> None:
    """取消指定 token；不传时优先命中活动 token，没有活动回合才置全局位。

    优先定向到活动 token 是为了不留下“回合结束后才被读到的”全局取消位：
    TUI/CLI 的 Esc 在回合结束时才被处理时，不应该影响下一个回合。
    """
    if token is None:
        token = first_token()
    if token is not None:
        token.request_cancel()
    else:
        _CANCEL.set()


def reset() -> None:
    """测试/极端恢复用：清空所有回合状态（进程内）。"""
    with _LOCK:
        _ACTIVE_TOKENS.clear()
    _ACTIVE.clear()
    _CANCEL.clear()
    _CURRENT.token = None


def cancel_requested() -> bool:
    token = getattr(_CURRENT, 'token', None)
    if token is not None and token.cancelled:
        return True
    return _CANCEL.is_set()
