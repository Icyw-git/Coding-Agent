"""Locally profile the per-turn pipeline on real sessions (no provider calls).

Stages mirror ``agent_loop`` before the model request:
history rebuild -> memory recall -> context/system prompt -> pre-compaction
-> request assembly. Timings are local CPU only; provider latency and prompt
cache hits come from ``scripts/profile_conversation.py`` (usage events).

Usage:
  python scripts/profile_turn_pipeline.py
  python scripts/profile_turn_pipeline.py --session .cache/sessions/session_x.jsonl
  python scripts/profile_turn_pipeline.py --limit full --json
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import sys
import time
import tracemalloc
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))

os.environ.setdefault('HARNESS_MEMORY_RECALL', 'local')

import harness  # noqa: E402
from session_store import SessionEventStore  # noqa: E402

STAGES = ('rebuild_ms', 'memory_ms', 'context_ms', 'system_ms',
          'compact_ms', 'assembly_ms')


def rss_mb() -> float | None:
    """Resident set size without third-party deps."""
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t)]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        get_info = kernel32.K32GetProcessMemoryInfo
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters),
                             wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        if not get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters),
                        counters.cb):
            return None
        return round(counters.WorkingSetSize / 1048576, 1)
    except Exception:
        pass
    try:
        import resource  # POSIX
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / (1048576 if sys.platform == 'darwin' else 1024), 1)
    except Exception:
        return None


def timed(call):
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    value = call()
    elapsed_ms = (time.perf_counter() - started) * 1000
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return value, elapsed_ms, peak


def _last_prompt_tokens(store) -> int | None:
    """Reuse the session's last confirmed prompt size, as agent_loop does."""
    last = None
    for event in store.iter_events():
        if event.get('type') == 'usage' and event.get('prompt'):
            last = int(event['prompt'])
    return last


def _compact(messages, size):
    return harness.pre_compact_messages(
        list(messages), 0, session_store=None, size=size, snip_state={})


def _assemble(messages, memories, system):
    return harness.expand_image_parts(
        harness.inject_memories(messages, memories, system))


def measure(path: Path, repeats: int = 5, *, compact_size: str = 'confirmed') -> dict:
    store = SessionEventStore.open(path)
    known_size = _last_prompt_tokens(store)
    runs = []
    rss = {}
    for index in range(repeats):
        before = rss_mb()
        messages, rebuild_ms, rebuild_peak = timed(store.rebuild_openai_history)
        memories, memory_ms, _ = timed(partial(harness.load_memories, messages))
        context, context_ms, _ = timed(
            partial(harness.update_context, {}, messages))
        system, system_ms, _ = timed(
            partial(harness.get_system_prompt, context))
        size = None if compact_size == 'full' else known_size
        _compacted, compact_ms, compact_peak = timed(
            partial(_compact, messages, size))
        assembled, assembly_ms, _ = timed(
            partial(_assemble, messages, memories, system))
        if index == repeats - 1:
            after = rss_mb()
            rss = {'before': before, 'after': after,
                   'delta': (round(after - before, 1)
                             if before is not None and after is not None else None)}
        runs.append({
            'rebuild_ms': rebuild_ms, 'memory_ms': memory_ms,
            'context_ms': context_ms, 'system_ms': system_ms,
            'compact_ms': compact_ms, 'assembly_ms': assembly_ms,
            'rebuild_peak_mb': rebuild_peak / 1048576,
            'compact_peak_mb': compact_peak / 1048576,
        })
    totals = [sum(r[key] for key in STAGES if key != 'rebuild_ms') for r in runs]
    return {
        'path': path.name,
        'disk_bytes': path.stat().st_size,
        'messages': len(messages),
        'memory_files': len(harness.list_memory_files()),
        'memory_block_chars': len(memories),
        'system_chars': len(system),
        'assembled_messages': len(assembled),
        'compact_size': compact_size,
        'compact_known_tokens': known_size,
        'stages_ms': {
            key: {
                'cold': round(runs[0][key], 2),
                'p50': round(statistics.median(r[key] for r in runs), 2),
                'max': round(max(r[key] for r in runs), 2),
            } for key in STAGES
        },
        'peaks_mb': {
            'rebuild': round(max(r['rebuild_peak_mb'] for r in runs), 2),
            'compact': round(max(r['compact_peak_mb'] for r in runs), 2),
        },
        'steady_state_ms_p50': round(statistics.median(totals), 2),
        'first_call_ms': round(totals[0], 2),
        'process_rss_mb': rss,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions', default=str(ROOT / '.cache' / 'sessions'))
    parser.add_argument('--session', default='largest')
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--limit', choices=('confirmed', 'full'),
                        default='confirmed',
                        help='pass confirmed prompt tokens (agent_loop steady state) '
                             'or force a full estimate (first turn / direct calls)')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    if args.session == 'largest':
        paths = sorted(Path(args.sessions).glob('session_*.jsonl'),
                       key=lambda path: path.stat().st_size, reverse=True)[:2]
    else:
        paths = [Path(args.session)]
    if not paths:
        raise SystemExit(f'no session logs under {args.sessions}')
    result = {
        'interpreter': sys.version.split()[0],
        'memory_recall_mode': os.getenv('HARNESS_MEMORY_RECALL', 'local'),
        'rss_after_import_mb': rss_mb(),
        'sessions': [measure(path, args.repeat, compact_size=args.limit)
                     for path in paths],
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"python {result['interpreter']} · rss after import "
          f"{result['rss_after_import_mb']} MB · recall {result['memory_recall_mode']}")
    for row in result['sessions']:
        stages = row['stages_ms']
        print(f"\n{row['path']}  {row['disk_bytes'] / 1048576:.1f} MB · "
              f"{row['messages']} msgs · {row['memory_files']} memories "
              f"({row['memory_block_chars']} chars injected)")
        print(f"  steady-state local {row['steady_state_ms_p50']} ms "
              f"(first {row['first_call_ms']} ms, compact size={row['compact_size']})")
        for key in STAGES:
            print(f"    {key[:-3]:8s} cold {stages[key]['cold']:7.1f}  "
                  f"p50 {stages[key]['p50']:7.1f}  max {stages[key]['max']:7.1f}")
        print(f"  peaks: rebuild {row['peaks_mb']['rebuild']} MB · "
              f"compact {row['peaks_mb']['compact']} MB · "
              f"rss delta {row['process_rss_mb']['delta']} MB")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
