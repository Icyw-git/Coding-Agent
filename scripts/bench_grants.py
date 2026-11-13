"""目录写授权热路径基准（next-design §9.4 R1-b）。

用法::

    python scripts/bench_grants.py            # 默认 0/5/20/200 条
    python scripts/bench_grants.py --quick

只做本地计时：在临时 HARNESS_HOME 里写一份合成授权账本，然后反复跑
`hooks.evaluate_call`。不读真实账本、不发网络请求、不碰桌面。

计时与 tracer **分开跑**：tracemalloc 会把每次分配的追踪开销算进墙钟。
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))

import console  # noqa: E402

console.harden_stdio()

REPEAT = 40
GROUPS = 3


def _pct(samples, q):
    ordered = sorted(samples)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


def _measure(fn, *, repeat: int) -> tuple[float, float, float]:
    """返回 (p50, p95, 峰值 KB)。时间与内存分开量。"""
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    tracemalloc.start()
    fn()
    peak = tracemalloc.get_traced_memory()[1] / 1024
    tracemalloc.stop()
    return _pct(samples, 0.5), _pct(samples, 0.95), peak


def _setup_home(count: int):
    """每个规模独立一个临时 HOME，账本里放 `count` 条合成授权。"""
    home = Path(tempfile.mkdtemp(prefix=f'grants-{count}-'))
    os.environ['HARNESS_HOME'] = str(home)
    return home


def run(counts, *, repeat: int, before: bool = False) -> None:
    home = _setup_home(max(counts))
    import grants
    import hooks

    if before:
        # 改前对照：每次都读账本（R1-b 之前的 `evaluate_call`）。
        # 同一次运行里两个口径可比，不拿历史数字跨机器比。
        hooks.needs_write_grants = lambda tool, args, zone: True
    import session_store

    workspace = home / 'ws'
    workspace.mkdir()
    outside_base = home / 'outside'
    outside_base.mkdir()
    os.chdir(workspace)

    store = session_store.SessionEventStore(workspace / 'sessions', session_id='bench')
    read_args = {'path': str(workspace / 'a.txt')}
    write_args = {'path': str(outside_base / 'z.txt'), 'content': 'x'}
    bash_args = {'command': 'ls -la'}

    print(f'python {sys.version.split()[0]} · 每组 {repeat} 次 × {GROUPS} 组')
    print(f"{'grants':>7} {'case':<22} {'p50':>9} {'p95':>9} {'peak':>10} {'ledger':>7}")
    for count in counts:
        paths = [outside_base / f'dir-{index}' for index in range(count)]
        for path in paths:
            path.mkdir(exist_ok=True)
        grants._save([{'path': str(path), 'scope': 'write', 'expires': '',
                       'created_by': '', 'note': '', 'session_id': '',
                       'created': time.time()} for path in paths])

        for label, tool, args in (
                ('read (workspace)', 'read', read_args),
                ('bash readonly', 'bash', bash_args),
                ('write (outside)', 'write', write_args),
        ):
            calls = {'n': 0}
            real = grants.active_write_paths

            def counting(*a, _real=real, _calls=calls, **k):
                _calls['n'] += 1
                return _real(*a, **k)

            grants.active_write_paths = counting
            try:
                rows = [_measure(lambda t=tool, a=args: hooks.evaluate_call(t, a),
                                 repeat=repeat) for _ in range(GROUPS)]
            finally:
                grants.active_write_paths = real
            p50 = statistics.median(row[0] for row in rows)
            p95 = max(row[1] for row in rows)
            peak = max(row[2] for row in rows)
            ledger = calls['n'] / (GROUPS * (repeat + 1))
            print(f'{count:>7} {label:<22} {p50:8.3f}ms {p95:8.3f}ms '
                  f'{peak:9.1f}KB {ledger:7.2f}')
    print('\n（ledger = 每次判定读账本的平均次数；0 表示这次判定压根不需要授权）')
    del store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='目录写授权热路径基准')
    parser.add_argument('--quick', action='store_true', help='只跑 0 与 200 条')
    parser.add_argument('--repeat', type=int, default=REPEAT)
    parser.add_argument('--before', action='store_true',
                        help='改前对照：每次判定都读账本')
    args = parser.parse_args(argv)
    label = '（改前对照）' if args.before else ''
    print(f'模式{label or "：按需求读账本"}')
    run([0, 200] if args.quick else [0, 5, 20, 200], repeat=args.repeat,
        before=args.before)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
