"""Benchmark provisional plain-lexer rendering for streaming fenced code blocks."""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]

from ui.harness_app import (  # noqa: E402
    HarnessApp,
    _render_markdown,
    _streaming_markdown_source,
)
from ui.transcript.model import Block  # noqa: E402


def _median_render(source, width, repeats):
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        rendered = _render_markdown(source, width)
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples), rendered


def main() -> int:
    parser_source = (
        '# Streaming code review\n\n'
        'Check the implementation and preserve its Markdown context.\n\n'
        '```python\n'
        + ''.join(f'def item_{n}(value): return value + {n}\n' for n in range(600))
        + '```\n\n| State | Count |\n|---|---:|\n| ready | 600 |\n'
    )
    partial_source = parser_source[:-4]
    cases = {
        'python-code': '```python\n' + ''.join(
            f'def item_{n}(value): return value + {n}\n' for n in range(600)) + '```',
        'mixed-open': partial_source,
        'unlabelled': '```\n' + ''.join(f'line {n} value\n' for n in range(600)) + '```',
    }
    repeats = 5
    print(f"{'case':<14} {'width':>5} {'full_ms':>10} {'stream_ms':>10} "
          f"{'speedup':>8} {'text_equal':>11}")
    for name, source in cases.items():
        candidate, _ = _streaming_markdown_source(source)
        assert candidate.count('\n') == source.count('\n')
        for width in (60, 100):
            app = HarnessApp([])
            block = Block('bench-stream', 'assistant',
                          {'text': source, 'streaming': True})
            full_ms, full_lines = _median_render(source, width, repeats)
            samples = []
            for _ in range(repeats):
                started = time.perf_counter()
                stream_lines = app._render_block(block, width)
                samples.append((time.perf_counter() - started) * 1000)
            stream_ms = statistics.median(samples)
            equal = [line.plain for line in full_lines] == [
                line.plain for line in stream_lines]
            print(f'{name:<14} {width:5d} {full_ms:9.2f} {stream_ms:9.2f} '
                  f'{full_ms / max(stream_ms, .001):7.2f}x {str(equal):>11}')
            if not equal:
                return 1
            block.payload['streaming'] = False
            final_lines = app._render_block(block, width)
            assert [(line.plain, line.spans) for line in final_lines] == [
                (line.plain, line.spans) for line in full_lines]
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
