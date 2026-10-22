"""流式 Markdown 重排成本对比：固定 120ms vs 自适应间隔。

用法：python scripts/bench_stream.py
只测 Rich Markdown 渲染成本与调度间隔，不启动 Textual。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.harness_app import (  # noqa: E402
    STREAM_MAX_INTERVAL,
    STREAM_MIN_INTERVAL,
    _render_markdown,
)


def main() -> int:
    print(f"{'chars':>9} {'render':>9} {'fixed':>9} {'adaptive':>10} {'fps':>7}")
    for chars in (1_000, 5_000, 10_000, 30_000, 100_000):
        text = ('word ' * (chars // 5 + 1))[:chars]
        started = time.perf_counter()
        _render_markdown(text, 100)
        render_ms = (time.perf_counter() - started) * 1000
        adaptive = min(STREAM_MAX_INTERVAL,
                       max(STREAM_MIN_INTERVAL, render_ms / 1000 * 2))
        print(f'{chars:9d} {render_ms:8.1f}ms {120.0:8.1f}ms '
              f'{adaptive * 1000:9.1f}ms {1 / adaptive:6.1f}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
