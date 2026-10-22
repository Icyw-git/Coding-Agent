"""Compare full Rich Markdown rendering against bounded last-frame reuse.

Covers plain text, paragraphs, lists, fenced code, and tables. Every phase checks
text and Rich spans. The cache may reuse only an unchanged finalized frame; the
first and changed frames still use the existing complete renderer.

Run: python scripts/bench_markdown_prefix.py --repeats 5
"""
from __future__ import annotations

import argparse
import gc
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]

from ui import harness_app as ui_module  # noqa: E402
from ui.harness_app import HarnessApp, _render_markdown  # noqa: E402
from ui.transcript.model import Block  # noqa: E402


def signature(lines):
    return [(line.plain, line.spans) for line in lines]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('repeats must be positive')
    samples = {
        'plain': 'word ' * 4000,
        'paragraphs': '**bold** 中文 text\n\n' * 500,
        'list': '- item with **bold**\n' * 800,
        'fenced-code': '```python\n' + 'print(1)\n' * 500 + '```',
        'table': '| A | B |\n|---|---|\n' + '| 1 | 2 |\n' * 250,
    }
    print(f"{'shape':<12} {'phase':<8} {'full_ms':>10} {'app_ms':>10} "
          f"{'ratio':>7} {'hit':>5} {'equal':>6}")
    for name, source in samples.items():
        full_times = {phase: [] for phase in ('first', 'changed', 'final')}
        app_times = {phase: [] for phase in full_times}
        for _trial in range(args.repeats):
            app = HarnessApp([])
            block = Block('stream', 'assistant',
                          {'text': source, 'streaming': True})
            render_calls = [0]
            original = ui_module._render_markdown

            def counted(text, width, *, _calls=render_calls, _render=original):
                _calls[0] += 1
                return _render(text, width)

            ui_module._render_markdown = counted
            try:
                for phase in full_times:
                    if phase == 'changed':
                        block.payload['text'] += '\nnext **token**'
                    if phase == 'final':
                        block.payload['streaming'] = False
                    before = render_calls[0]
                    expected_start = time.perf_counter()
                    expected = _render_markdown(block.payload['text'], 100)
                    full_ms = (time.perf_counter() - expected_start) * 1000
                    app_start = time.perf_counter()
                    actual = app._render_block(block, 100)
                    app_ms = (time.perf_counter() - app_start) * 1000
                    hit = render_calls[0] == before
                    assert signature(actual) == signature(expected), (name, phase)
                    if phase == 'final':
                        assert hit, (name, 'unchanged final frame missed cache')
                    full_times[phase].append(full_ms)
                    app_times[phase].append(app_ms)
            finally:
                ui_module._render_markdown = original
            del app, block
            gc.collect()
        for phase in full_times:
            full = statistics.median(full_times[phase])
            cached = statistics.median(app_times[phase])
            print(f'{name:<12} {phase:<8} {full:9.2f} {cached:9.2f} '
                  f'{cached / full:6.3f} {"yes" if phase == "final" else "no":>5} '
                  '  True')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
