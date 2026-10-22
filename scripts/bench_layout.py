"""布局行缓存对比：完整缓存 vs 有界离屏缓存。

用法：python scripts/bench_layout.py [blocks] [width]
只测 ConversationModel + TranscriptLayout，不启动 Textual。
"""
from __future__ import annotations

import gc
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from ui.transcript import ConversationModel, TranscriptLayout  # noqa: E402


def run(blocks: int, width: int, limit: int | None) -> dict:
    tracemalloc.start()
    try:
        model = ConversationModel()
        for index in range(blocks):
            model.append('user' if index % 4 == 0 else 'assistant', {
                'text': '\n'.join(f'line {index}.{row}' for row in range(3)),
            }, block_id=f'b{index}')
        model_current, _ = tracemalloc.get_traced_memory()
        layout = TranscriptLayout(
            lambda block, _width: str(block.payload['text']).splitlines() or [''],
            line_cache_limit=(10 ** 9 if limit is None else limit))
        started = time.perf_counter()
        layout.update(model, width)
        layout_ms = (time.perf_counter() - started) * 1000
        after_update, update_peak = tracemalloc.get_traced_memory()
        started = time.perf_counter()
        layout.visible(0, 40, 40)
        layout.visible(layout.max_scroll(40), 40, 40)
        visible_ms = (time.perf_counter() - started) * 1000
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {
        'limit': limit,
        'model_mb': model_current / 1048576,
        'after_update_mb': after_update / 1048576,
        'current_mb': current / 1048576,
        'peak_mb': peak / 1048576,
        'layout_ms': layout_ms,
        'visible_ms': visible_ms,
        'cached_lines': layout._cached_lines,
    }


def main() -> int:
    blocks = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    rows = []
    for label, limit in (('full', None), ('bounded', 6000)):
        gc.collect()
        row = run(blocks, width, limit)
        rows.append((label, row))
    print(f'blocks={blocks} width={width}')
    print(f"{'mode':<8} {'model':>7} {'after':>7} {'current':>8} {'peak':>7} "
          f"{'layout':>8} {'visible':>8} {'lines':>7}")
    for label, row in rows:
        print(f"{label:<8} {row['model_mb']:6.1f}M {row['after_update_mb']:6.1f}M "
              f"{row['current_mb']:7.1f}M {row['peak_mb']:6.1f}M "
              f"{row['layout_ms']:7.1f}ms {row['visible_ms']:7.1f}ms "
              f"{row['cached_lines']:7d}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
