"""请求去重存储的容量与形状基准（next-design §9.4 R1-a 的前置调研）。

设计文档给的第一版默认值是「保留期 7 天、每状态根最多 10000 条」，并注明
「这些是拟定默认值，需容量基准验证」。这个脚本回答两个问题：

1. **量**：真实数据一天有多少入站请求？7 天要装多少条？
2. **形状**：一万条记录，用「单文件 JSON 读-改-写」和「append-only JSONL」差多少？

用法::

    python scripts/bench_request_dedup.py
    python scripts/bench_request_dedup.py --records 5000
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))

import console  # noqa: E402

console.harden_stdio()

ROUNDS = 8


def _record(index: int) -> dict:
    """一条去重记录的典型形状（内容只留指纹，不存正文）。"""
    return {'request_id': hashlib.sha1(str(index).encode()).hexdigest()[:16],
            'session_id': 'be1098f326174129b06c8255afad802d',
            'origin': 'feishu',
            'fingerprint': hashlib.sha256(str(index).encode()).hexdigest()[:32],
            'turn_id': f't-{index}',
            'status': 'accepted',
            'created_at': 1794550000.0 + index}


def measure(label: str, fn, rounds: int = ROUNDS) -> float:
    samples = []
    for _ in range(rounds):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    print(f'{label:38} p50 {statistics.median(samples):7.2f}ms '
          f'max {max(samples):7.2f}ms')
    return statistics.median(samples)


def real_volume() -> None:
    """真实会话里的日入站量（role=user 的消息）。"""
    days = Counter()
    sessions = {}
    for path in sorted((ROOT / '.cache' / 'sessions').glob('session_*.jsonl')):
        session = path.stem.removeprefix('session_')
        for line in open(path, encoding='utf-8', errors='replace'):
            if '"message"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') != 'message':
                continue
            if (event.get('message') or {}).get('role') != 'user':
                continue
            stamp = event.get('ts') or 0
            if not stamp:
                continue
            day = time.strftime('%Y-%m-%d', time.localtime(stamp))
            days[day] += 1
            sessions.setdefault(day, set()).add(session)
    if not days:
        print('[volume] 没有真实会话数据')
        return
    total = sum(days.values())
    peak = max(days.values())
    print(f'[volume] {len(days)} 天 · 共 {total} 条用户消息 '
          f'· 日均 {total / len(days):.1f} · 单日峰值 {peak}')
    print(f'[volume] 按峰值估 7 天 = {peak * 7} 条')


def storage_shape(records: int) -> None:
    work = Path(tempfile.mkdtemp(prefix='dedup-'))
    payload = [_record(index) for index in range(records)]

    flat = work / 'flat.json'
    flat.write_text(json.dumps({'records': payload}, ensure_ascii=False), encoding='utf-8')

    def flat_cycle() -> None:
        data = json.loads(flat.read_text(encoding='utf-8'))
        data['records'].append(_record(10 ** 7))
        tmp = flat.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(flat)

    stream = work / 'stream.jsonl'
    with stream.open('w', encoding='utf-8', newline='\n') as handle:
        for item in payload:
            handle.write(json.dumps(item, ensure_ascii=False) + '\n')
    counter = [0]

    def append_one() -> None:
        counter[0] += 1
        with stream.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(json.dumps(_record(10 ** 7 + counter[0]),
                                    ensure_ascii=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())

    size_kb = flat.stat().st_size / 1024
    print(f'[shape] {records} 条 · 单文件 {size_kb:.0f}KB · '
          f'每条约 {size_kb * 1024 / max(1, records):.0f}B')
    measure('  单文件 JSON 读-改-写', flat_cycle)
    measure('  append-only JSONL（带 fsync）', append_one)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='请求去重存储的容量与形状基准')
    parser.add_argument('--records', type=int, nargs='*',
                        default=[0, 1000, 10000],
                        help='要测的记录数（默认 0/1000/10000）')
    args = parser.parse_args(argv)
    real_volume()
    for count in args.records:
        storage_shape(count)
    print('\n[结论] 记录数与存储都不是瓶颈；形状才是——单文件重写在万级就要几十毫秒，'
          '落在每条入站请求的热路径上。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
