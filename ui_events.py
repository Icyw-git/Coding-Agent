"""UIEventBus：业务代码与渲染解耦的事件总线（UI 改进方案 P0 第 1 层）。

业务侧（harness / agent_teams / subagent ...）只 emit，不直接渲染；
渲染侧（renderer.BaseRenderer 子类）订阅事件流。

关键设计：emit 为同步调用，执行点 = 原渲染调用点，保证显示顺序与时序
与改造前一致；渲染侧异常被吞掉，不拖垮业务。线程安全，所有线程只写
事件流，由渲染后端统一消费（消除多线程 print 竞争）。
"""
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List

EVENT_TYPES = {
    'assistant_text',      # 模型最终回复文本 {text}
    'tool_start',          # 工具开始 {name, args}
    'tool_end',            # 工具结束 {name, args, output_preview, ok}
    'permission_request',  # 权限请求 {req_id, tool, args}
    'permission_resolve',  # 权限应答 {req_id, approved}
    'status',              # 会话状态指标 {model, tools, ctx_pct, tokens, cost}
    'diff',                # diff 文本 {path, diff_text}
    'debug',               # 过程/调试信息 {msg}
    'session_start',       # 会话开始 {prompt}
    'session_end',         # 会话结束
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
        self._lock = threading.Lock()

    def emit(self, type_: str, **payload) -> None:
        if type_ not in EVENT_TYPES:
            raise ValueError(f'unknown ui event type: {type_}')
        e = UIEvent(type_, payload)
        with self._lock:
            for cb in list(self._subs):
                try:
                    cb(e)
                except Exception:
                    pass    # 渲染异常不拖垮业务

    def subscribe(self, cb: Callable[[UIEvent], None]) -> None:
        with self._lock:
            self._subs.append(cb)

    def unsubscribe(self, cb: Callable[[UIEvent], None]) -> None:
        with self._lock:
            if cb in self._subs:
                self._subs.remove(cb)


# 模块级单例：业务代码 `from ui_events import bus` 后 bus.emit(...)
bus = UIEventBus()
