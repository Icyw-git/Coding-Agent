"""Compare full replay with bounded incremental projection on identical events.

Uses isolated temporary storage and no model calls. Times replay only, excluding
fsync. Memory measurements use tracemalloc and are Python allocations, not RSS.
Run: python scripts/bench_projection.py --rounds 30 --events 1000
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from session_store import SessionEventStore  # noqa: E402


def measure(root: Path, *, incremental: bool, rounds: int) -> dict:
    store = SessionEventStore(root, session_id='baseline')
    rebuild = (store.rebuild_openai_history if incremental
               else store._replay_openai_history)
    tracemalloc.start()
    started = time.perf_counter()
    history = rebuild()
    cold_ms = (time.perf_counter() - started) * 1000
    samples = []
    for index in range(rounds):
        store.append_message({'role': 'user', 'content': f'next {index}'})
        started = time.perf_counter()
        history = rebuild()
        samples.append((time.perf_counter() - started) * 1000)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        'cold_ms': round(cold_ms, 2),
        'warm_median_ms': round(statistics.median(samples), 2),
        'current_kib': round(current / 1024, 1),
        'peak_kib': round(peak / 1024, 1),
        'messages': len(history),
        'history': history,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds', type=int, default=30)
    parser.add_argument('--events', type=int, default=1000)
    args = parser.parse_args()
    if args.rounds < 1 or args.events < 1:
        parser.error('rounds and events must be positive')
    with tempfile.TemporaryDirectory(prefix='harness-projection-') as temporary:
        root = Path(temporary)
        seed = SessionEventStore(root / 'seed', session_id='baseline')
        for index in range(args.events):
            seed.append_message({'role': 'user', 'content': f'{index}: ' + 'x' * 6000})
        seed.append_compaction('preserved goal', 'benchmark', tail=[
            {'role': 'user', 'content': 'recent task'},
            {'role': 'assistant', 'content': 'ready'},
        ])
        source = seed.path.read_bytes()
        rows = {}
        for mode in ('full', 'incremental'):
            directory = root / mode
            directory.mkdir()
            (directory / seed.path.name).write_bytes(source)
            rows[mode] = measure(directory, incremental=mode == 'incremental',
                                 rounds=args.rounds)
        assert rows['full'].pop('history') == rows['incremental'].pop('history')
        print(json.dumps({'source_bytes': len(source), 'rounds': args.rounds,
                          'modes': rows, 'equal_histories': True}, indent=2))


if __name__ == '__main__':
    main()
