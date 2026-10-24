"""UI 历史窗口对比：全量加载 vs 初始窗口 + 分页。

用法：python scripts/bench_history.py [messages]
需要 Textual；直接复用 HarnessApp 的 run_test，不启动真实模型。
"""
from __future__ import annotations

import asyncio
import gc
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui import event_reduction  # noqa: E402
from ui import harness_app as module  # noqa: E402


def _messages(count: int) -> list[dict]:
    return [
        {'role': 'user' if index % 2 == 0 else 'assistant',
         'content': f'history-{index}'}
        for index in range(count)
    ]


async def _measure(label: str, limit: int, messages: list[dict]) -> dict:
    event_reduction.HISTORY_INITIAL_MESSAGES = limit
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    app = module.HarnessApp(messages)
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(100):
                await pilot.pause()
                await asyncio.sleep(0.01)
                if app.view.content_line_count:
                    break
            elapsed_ms = (time.perf_counter() - started) * 1000
            current, peak = tracemalloc.get_traced_memory()
            return {
                'label': label,
                'model': len(app.model),
                'lines': app.view.content_line_count,
                'ready_ms': elapsed_ms,
                'current_mb': current / 1048576,
                'peak_mb': peak / 1048576,
                'start': app._history_start,
            }
    finally:
        app._exiting = True
        tracemalloc.stop()


async def main_async(count: int) -> list[dict]:
    messages = _messages(count)
    initial_messages = event_reduction.HISTORY_INITIAL_MESSAGES
    full = await _measure('full', 10 ** 9, messages)
    paged = await _measure('paged', initial_messages, messages)
    return [full, paged]


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
    rows = asyncio.run(main_async(count))
    print(f'messages={count}')
    print(f"{'mode':<8} {'blocks':>7} {'lines':>7} {'ready':>9} "
          f"{'current':>8} {'peak':>7} {'start':>7}")
    for row in rows:
        print(f"{row['label']:<8} {row['model']:7d} {row['lines']:7d} "
              f"{row['ready_ms']:8.1f}ms {row['current_mb']:7.1f}M "
              f"{row['peak_mb']:6.1f}M {row['start']:7d}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
