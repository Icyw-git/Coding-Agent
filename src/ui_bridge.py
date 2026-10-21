"""主循环 → UI 事件桥：把工具、流式文本、状态指标转成 `ui_events` 事件。

设计约束：
- 没有订阅者时 `emit` 是空操作，命令行/无头/测试不需要 UI 模块也能跑；
- 事件只携带展示数据，不改业务状态；渲染异常由总线吞掉；
- 成本不做估算：没有可靠价格表时不显示，避免把「0」当成真实花费。
"""
from __future__ import annotations

import threading

try:
    from ui_events import bus
except Exception:   # SDK / 独立使用 src/ 时允许没有 UI 资产
    bus = None


class SessionMetrics:
    """进程内累计指标：工具次数、usage、最近调用明细。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.tools = 0
            self.calls = 0
            self.tokens = 0
            self.prompt_tokens = 0
            self.completion_tokens = 0
            self.cached_tokens = 0
            self.recent: list[dict] = []
            self.ctx_tokens = 0
            self.ctx_source = 'estimated'

    def record_tool(self) -> None:
        with self._lock:
            self.tools += 1

    def record_usage(self, detail: dict | None) -> None:
        if not detail:
            return
        with self._lock:
            self.calls += 1
            self.tokens += int(detail.get('total', 0))
            self.prompt_tokens += int(detail.get('prompt', 0))
            self.completion_tokens += int(detail.get('completion', 0))
            self.cached_tokens += int(detail.get('cached', 0))
            self.recent = (self.recent + [dict(detail)])[-5:]
            self.ctx_tokens = int(detail.get('prompt', self.ctx_tokens))
            self.ctx_source = 'confirmed'

    def snapshot(self) -> dict:
        with self._lock:
            return {
                'tools': self.tools,
                'calls': self.calls,
                'tokens': self.tokens,
                'prompt_tokens': self.prompt_tokens,
                'completion_tokens': self.completion_tokens,
                'cached_tokens': self.cached_tokens,
                'ctx_tokens': self.ctx_tokens,
                'ctx_source': self.ctx_source,
                'recent_calls': list(self.recent),
                'rounds': self.calls,
                'tool_outcomes': {},
                'cost': '0',
                'cost_source': 'unavailable',
            }


METRICS = SessionMetrics()


def streaming_enabled() -> bool:
    """有 UI 渲染后端订阅时才为它开启流式（无订阅者走非流式）。"""
    return bus is not None and bus.has_subscribers()

def emit(event_type: str, **payload) -> None:
    """转发到 UI 总线；没有 UI 资产时静默跳过。"""
    if bus is not None:
        bus.emit(event_type, **payload)


def status_payload(*, model: str, workdir: str, ctx_tokens: int,
                   ctx_source: str, window: int) -> dict:
    """状态行需要的一份快照（ctx 百分比在这里算，渲染层只负责画）。"""
    snapshot = METRICS.snapshot()
    window = max(1, int(window or 1))
    snapshot.update({
        'model': model,
        'workdir': str(workdir),
        'ctx_tokens': int(ctx_tokens),
        'ctx_window': window,
        'ctx_pct': min(100.0, 100.0 * int(ctx_tokens) / window),
        'ctx_source': ctx_source,
    })
    return snapshot
