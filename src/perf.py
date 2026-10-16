"""轻量分段计时器：HARNESS_PERF=1 启用。

用法：
    with segment('cp.git'): ...        # 上下文分段
    record('llm.ttfb', 0.42)           # 一次性打点（如流式首字延迟）
    reset()                            # 回合开始清零
    snapshot()                         # 回合结束取 {'name': (count, total)}

聚合是平铺的：嵌套分段会重复计入外层与内层（如 cp.begin_turn 与其内部的
cp.git/cp.write），相加前先看清楚包含关系。
"""
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar

_stats: ContextVar[dict[str, tuple[int, float]] | None] = ContextVar('perf_stats', default=None)


@contextmanager
def segment(name: str):
    if os.getenv('HARNESS_PERF', '') != '1':
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        record(name, time.perf_counter() - t0)


def record(name: str, seconds: float) -> None:
    if os.getenv('HARNESS_PERF', '') != '1':
        return
    # Copy on write also isolates child tasks that inherit the current context.
    stats = dict(_stats.get() or {})
    count, total = stats.get(name, (0, 0.0))
    stats[name] = (count + 1, total + seconds)
    _stats.set(stats)


def reset() -> None:
    _stats.set({})


def snapshot() -> dict[str, tuple[int, float]]:
    return dict(_stats.get() or {})


def fmt(snapshot: dict[str, tuple[int, float]]) -> str:
    parts = []
    for name, (count, total) in sorted(snapshot.items(),
                                       key=lambda kv: kv[1][1], reverse=True):
        parts.append(f'{name}={total * 1000:.0f}ms'
                     + (f'x{count}' if count > 1 else ''))
    return '  '.join(parts)
