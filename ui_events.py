"""UIEventBus：业务代码与渲染解耦的事件总线（UI 改进方案 P0 第 1 层）。

业务侧（harness / agent_teams / subagent ...）只 emit，不直接渲染；
渲染侧（renderer.BaseRenderer 子类）订阅事件流。

关键设计：emit 为同步调用，执行点 = 原渲染调用点，保证显示顺序与时序
与改造前一致；渲染侧异常被吞掉，不拖垮业务。线程安全，所有线程只写
事件流，由渲染后端统一消费（消除多线程 print 竞争）。
"""

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List

EVENT_TYPES = {
    'user_message',  # 用户输入（多轮可见性）{text}
    'assistant_start',  # 一条助手消息开始（流式）{mid}
    'assistant_delta',  # 助手消息增量文本（流式）{mid, delta}
    'assistant_end',  # 助手消息结束（流式）{mid}
    'assistant_text',  # 模型最终回复文本（整块，非流式兼容）{text}
    'tool_start',  # 工具开始 {name, args}
    'tool_end',  # 工具结束 {name, args, output_preview, ok}
    'permission_request',  # 权限请求 {req_id, tool, args, allow_session}
    'permission_resolve',  # 权限应答 {req_id, approved, scope}
    'status',  # 会话状态指标 {model, tools, ctx_pct, tokens, cost}
    'diff',  # diff 文本 {path, diff_text}
    'debug',  # 过程/调试信息 {msg}
    'error',  # 会话错误 {msg}
    'activity',  # 会话活动 {state, detail}
    'session_start',  # 会话开始 {prompt}
    'round_end',  # 单轮模型调用指标 {round, duration_ms, cost, ctx_*}
    'session_end',  # 会话结束
    'background_start',  # 后台任务开始 {task_id, command}
    'background_end',  # 后台任务结束 {task_id, command, ok, output_len}
}


@dataclass
class UIEvent:
    type: str
    payload: dict
    ts: float = field(default_factory=time.time)


class UIEventBus:
    """线程安全事件总线：emit 同步分发到所有订阅者（渲染后端）。"""

    def __init__(self):
        self._subs: List[Callable[[UIEvent], None]] = []
        self._lock = threading.RLock()  # 可重入：回调内再 emit（如 resolve 应答）不死锁

    def emit(self, type_: str, **payload) -> None:
        if type_ not in EVENT_TYPES:
            raise ValueError(f'unknown ui event type: {type_}')
        e = UIEvent(type_, payload)
        with self._lock:
            for cb in list(self._subs):
                try:
                    cb(e)
                except Exception:
                    pass  # 渲染异常不拖垮业务

    def has_subscribers(self) -> bool:
        with self._lock:
            return bool(self._subs)

    def subscribe(self, cb: Callable[[UIEvent], None]) -> None:
        with self._lock:
            self._subs.append(cb)

    def unsubscribe(self, cb: Callable[[UIEvent], None]) -> None:
        with self._lock:
            if cb in self._subs:
                self._subs.remove(cb)


# 模块级单例：业务代码 `from ui_events import bus` 后 bus.emit(...)
bus = UIEventBus()


# Session cancellation is intentionally separate from the event bus: the
# worker loop needs a cheap, thread-safe signal while it is blocked in a
# provider stream.
_cancel_event = threading.Event()


def request_cancel() -> None:
    _cancel_event.set()


def clear_cancel() -> None:
    _cancel_event.clear()


def cancel_requested() -> bool:
    return _cancel_event.is_set()


class PermissionGate:
    """权限确认挂起（UI 改进方案 P1）：业务线程请求 → 渲染层弹窗应答 → 唤醒。

    事件模式（use_ui=True，Textual）：emit permission_request 后队列挂起，
    渲染层收到事件弹权限 Modal，用户 y/n/a 后 emit permission_resolve 唤醒——
    等价参考骨架的 asyncio.Future，适配同步架构。a 只在 allow_session=True
    （该类操作有「同类」可记）时生效，scope 由请求方决定，不信任事件载荷。
    input 模式（默认）：打印提示后读 stdin，行为与旧版 input() 一致。

    无论哪种模式，超时（300s）都自动拒绝，避免无人应答时永久挂起。"""

    REQUEST_TIMEOUT = 300

    def __init__(self, bus: UIEventBus, use_ui: bool = False):
        self._bus = bus
        self.use_ui = use_ui  # True=事件弹窗模式；False=stdin 模式
        # req_id -> (应答队列, 是否允许本会话同类)；scope 只在登记时允许才放行。
        self._pending: Dict[str, tuple[queue.Queue, bool]] = {}
        self._policies: Dict[str, str] = {}
        self._seq = 0
        self._lock = threading.RLock()
        bus.subscribe(self._on_resolve)

    def _on_resolve(self, e: UIEvent) -> None:
        if e.type != 'permission_resolve':
            return
        with self._lock:
            entry = self._pending.pop(e.payload.get('req_id'), None)
        if not entry:
            return
        q, allow_session = entry
        scope = e.payload.get('scope') if allow_session else None
        q.put((bool(e.payload.get('approved', False)), scope if scope == 'session' else None))
        self._bus.emit('activity', state='thinking', detail='Continuing after permission')

    def cancel_pending(self) -> None:
        """拒绝并唤醒所有待确认请求，用于 Textual 会话退出。"""
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for q, _allow_session in pending:
            q.put((False, None))
        if pending:
            self._bus.emit('activity', state='idle', detail='Permission requests cancelled')

    def request(self, tool: str, args: dict, title: str = 'Permission needed') -> bool:
        """请求一次权限确认，返回是否批准。业务线程调用，阻塞直到应答。"""
        return self.request_scoped(tool, args, title)[0]

    def request_scoped(
        self, tool: str, args: dict, title: str = 'Permission needed', *, allow_session: bool = True
    ) -> tuple[bool, str | None]:
        """返回 (是否批准, scope)；scope='session' 表示本会话同类免审。

        allow_session 由调用方按 approval_class 是否为空决定。无头/控制台调用方
        没有「同类」概念时传 False，弹窗不会显示 a，事件里伪造的 scope 也被丢弃。
        """
        if not self.use_ui:
            return self._ask_input(tool, args, title), None
        policy = self._policies.get(tool)
        if policy == 'allow':
            return True, None
        if policy == 'deny':
            return False, None
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._seq += 1
            req_id = f'perm_{self._seq}'
            self._pending[req_id] = (q, bool(allow_session))
        self._bus.emit('activity', state='awaiting_permission', detail=title)
        self._bus.emit(
            'permission_request', req_id=req_id, tool=tool, args=args, title=title, allow_session=bool(allow_session)
        )
        try:
            return q.get(timeout=self.REQUEST_TIMEOUT)
        except queue.Empty:
            with self._lock:
                self._pending.pop(req_id, None)
            return False, None  # 无人应答：拒绝比等待安全

    def set_policy(self, tool: str, policy: str) -> None:
        if policy not in ('allow', 'deny', 'ask'):
            raise ValueError('policy must be allow, deny, or ask')
        if policy == 'ask':
            self._policies.pop(tool, None)
        else:
            self._policies[tool] = policy

    def policy(self, tool: str) -> str:
        return self._policies.get(tool, 'ask')

    @staticmethod
    def _ask_input(tool: str, args: dict, title: str) -> bool:
        print(f'\n\033[33m\u26a0  {title}\033[0m')
        print(f'   Tool: {tool}({args})')
        choice = input('Allow? (y/n) ').strip().lower()
        return choice in ('y', 'yes')
