"""Summarize persisted conversation latency, tool time, and provider cache usage."""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from session_store import SessionEventStore


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def summarize(paths: list[Path]) -> dict:
    usage_rows, tool_rows = [], []
    for path in paths:
        for event in SessionEventStore.open(path).iter_events():
            if event.get('type') == 'usage':
                usage_rows.append(event)
            elif event.get('type') == 'tool_profile':
                tool_rows.append(event)
    prompt = sum(int(row.get('prompt', 0) or 0) for row in usage_rows)
    cached = sum(int(row.get('cached', 0) or 0) for row in usage_rows)
    timings = {}
    for field in ('memory_load_ms', 'compact_ms', 'prompt_assembly_ms',
                  'prepare_ms', 'request_ms', 'ttfb_ms', 'stream_ms'):
        values = [float(row[field]) for row in usage_rows if row.get(field) is not None]
        timings[field] = {'count': len(values), 'p50': statistics.median(values) if values else None,
                          'p95': percentile(values, .95)}
    prefix_status = collections.Counter(
        row.get('cache_prefix_status') for row in usage_rows
        if row.get('cache_prefix_status'))
    low_hit_by_prefix = collections.Counter(
        row.get('cache_prefix_status') for row in usage_rows
        if row.get('cache_prefix_status') and int(row.get('prompt', 0) or 0) > 0
        and int(row.get('cached', 0) or 0) / int(row['prompt']) < .5)
    return {
        'sessions': len(paths), 'model_calls': len(usage_rows),
        'profiled_calls': sum('request_ms' in row for row in usage_rows),
        'prompt_tokens': prompt, 'cached_tokens': cached,
        'weighted_cache_hit_rate': cached / prompt if prompt else None,
        'low_hit_calls': sum(
            1 for row in usage_rows if int(row.get('prompt', 0) or 0) > 0
            and int(row.get('cached', 0) or 0) / int(row['prompt']) < .5),
        'timings_ms': timings,
        'cache_prefix_status': dict(prefix_status),
        'low_hit_by_prefix_status': dict(low_hit_by_prefix),
        'cache_prefix_diagnostic_calls': sum(prefix_status.values()),
        'tool_batches': len(tool_rows),
        'tool_batch_ms': {
            'p50': statistics.median([float(row['duration_ms']) for row in tool_rows]) if tool_rows else None,
            'p95': percentile([float(row['duration_ms']) for row in tool_rows], .95),
        },
        'tool_calls': sum(int(row.get('tool_count', 0)) for row in tool_rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sessions', nargs='*', type=Path, help='session JSONL paths')
    parser.add_argument('--dir', type=Path, default=Path('.cache/sessions'))
    args = parser.parse_args()
    paths = args.sessions or sorted(args.dir.glob('session_*.jsonl'))
    print(json.dumps(summarize(paths), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
