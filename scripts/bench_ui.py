"""TUI 性能基准：块布局/视口切片/流式增量。

用法：python scripts/bench_ui.py [blocks] [width] [frames]
不依赖 Textual 运行，只测虚拟化的纯计算部分，便于 CI/回归对比。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from ui.transcript import ConversationModel, TranscriptLayout  # noqa: E402


def main() -> int:
    blocks = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    frames = int(sys.argv[3]) if len(sys.argv) > 3 else 100

    model = ConversationModel()
    for index in range(blocks):
        kind = 'user' if index % 4 == 0 else 'assistant'
        text = '\n'.join(f'line {index}.{row}' for row in range(3))
        model.append(kind, {'text': text}, block_id=f'b{index}')

    def render(block, width):
        return str(block.payload.get('text', '')).splitlines() or ['']

    layout = TranscriptLayout(render)
    started = time.perf_counter()
    layout.update(model, width)
    layout_ms = (time.perf_counter() - started) * 1000

    viewport = 40
    max_scroll = layout.max_scroll(viewport)
    started = time.perf_counter()
    for frame in range(frames):
        offset = (max_scroll // frames) * frame
        layout.visible(offset, viewport, overscan=40)
    scroll_ms = (time.perf_counter() - started) * 1000 / max(1, frames)

    started = time.perf_counter()
    for _ in range(200):
        model.append_text('b1', 'x')
        layout.update(model, width)
    stream_ms = (time.perf_counter() - started) * 1000 / 200

    print(f'blocks={blocks} width={width} height={layout.total_height}')
    print(f'layout={layout_ms:.1f}ms  scroll_frame={scroll_ms:.3f}ms  '
          f'stream_update={stream_ms:.3f}ms  render_calls={layout.render_calls}')
    ok = layout_ms < 1500 and scroll_ms < 10 and stream_ms < 20
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
