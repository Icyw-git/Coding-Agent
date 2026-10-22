"""Agent 存储/加载内存 profile。

用法：
  python scripts/profile_storage.py
  python scripts/profile_storage.py --json
  python scripts/profile_storage.py --session .cache/sessions/session_x.jsonl
  python scripts/profile_storage.py --session largest
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import storage_profile  # noqa: E402
from session_store import SessionEventStore  # noqa: E402


def _session_memory(path: Path) -> dict:
    tracemalloc.start()
    try:
        started = time.perf_counter()
        store = SessionEventStore.open(path)
        events = store.read_events()
        read_ms = (time.perf_counter() - started) * 1000
        read_current, read_peak = tracemalloc.get_traced_memory()
        event_count = len(events)
        del events
        tracemalloc.reset_peak()
        started = time.perf_counter()
        messages = store.rebuild_openai_history()
        rebuild_ms = (time.perf_counter() - started) * 1000
        current, rebuild_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {
        'path': str(path),
        'disk_bytes': path.stat().st_size,
        'events': event_count,
        'messages': len(messages),
        'read_ms': round(read_ms, 1),
        'rebuild_ms': round(rebuild_ms, 1),
        'read_current_bytes': read_current,
        'read_peak_bytes': read_peak,
        'current_bytes': current,
        'rebuild_peak_bytes': rebuild_peak,
        'peak_bytes': max(read_peak, rebuild_peak),
    }


def _largest_session(root: Path) -> Path:
    files = [path for path in (root / 'sessions').glob('session_*.jsonl')
             if path.is_file()]
    if not files:
        raise SystemExit(f'no session logs under {root / "sessions"}')
    return max(files, key=lambda path: path.stat().st_size)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(ROOT / '.cache'))
    parser.add_argument('--session', nargs='?', const='largest',
                        help='session JSONL path, or "largest"')
    parser.add_argument('--shallow', action='store_true',
                        help='skip reference/checkpoint reclaimable scans')
    parser.add_argument('--keep', type=int, default=20,
                        help='checkpoint retention used in the preview')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    profile = storage_profile.scan(
        root, sessions_dir=root / 'sessions', checkpoint_keep=args.keep,
        deep=not args.shallow)
    result = {
        'profile': {
            key: (str(value) if isinstance(value, Path) else value)
            for key, value in profile.items()
            if key not in ('entries', 'unmanaged')
        },
        'entries': [
            {key: (str(value) if isinstance(value, Path) else value)
             for key, value in entry.items() if key != 'path'}
            for entry in profile['entries']
        ],
        'unmanaged': [
            {key: (str(value) if isinstance(value, Path) else value)
             for key, value in entry.items() if key != 'path'}
            for entry in profile['unmanaged']
        ],
    }
    if args.session:
        path = (_largest_session(root) if args.session == 'largest'
                else Path(args.session).expanduser())
        if not path.is_file():
            raise SystemExit(f'session not found: {path}')
        result['memory'] = _session_memory(path)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(storage_profile.format_profile(profile))
    if 'memory' in result:
        memory = result['memory']
        print('  会话加载内存 profile')
        print(f"  {Path(memory['path']).name}: 磁盘 {storage_profile._size(memory['disk_bytes'])} · "
              f"{memory['events']} 事件 / {memory['messages']} 消息")
        print(f"  读取 {memory['read_ms']} ms（峰值 {storage_profile._size(memory['read_peak_bytes'])}）· "
              f"重建历史 {memory['rebuild_ms']} ms"
              f"（峰值 {storage_profile._size(memory['rebuild_peak_bytes'])}）· "
              f"当前 {storage_profile._size(memory['current_bytes'])}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
