"""提取本回合产生的截图元数据。

只回传内容寻址的文件名与尺寸，不暴露截图绝对路径；是否/如何发送由调用方决定。
"""
from __future__ import annotations

import re

MAX_PER_TURN = 4
_NAME = re.compile(r'^[0-9a-f]{40}\.png$')
_SHA1 = re.compile(r'^[0-9a-f]{40}$')


def valid_name(name: str) -> bool:
    return bool(_NAME.fullmatch(str(name or '')))


def offset(store) -> int:
    """回合开始前的文件字节偏移；tail 从这里读就不会带回旧截图。"""
    try:
        return int(store.path.stat().st_size)
    except (AttributeError, OSError):
        return 0


def collect(events, *, limit: int = MAX_PER_TURN) -> list[dict]:
    """从事件列表里取 screen_captured，按 sha1 去重并限制数量。"""
    if limit <= 0:
        return []
    shots: list[dict] = []
    seen: set[str] = set()
    for event in events or ():
        if not isinstance(event, dict) or event.get('type') != 'screen_captured':
            continue
        sha1 = str(event.get('sha1') or '').strip().lower()
        if not _SHA1.fullmatch(sha1) or sha1 in seen:
            continue
        seen.add(sha1)
        shots.append({
            'name': f'{sha1}.png',
            'sha1': sha1,
            'bytes': _count(event.get('bytes')),
            'width': _count(event.get('width')),
            'height': _count(event.get('height')),
        })
        if len(shots) >= limit:
            break
    return shots


def collect_tail(store, after: int, *, limit: int = MAX_PER_TURN) -> list[dict]:
    if store is None:
        return []
    try:
        events, _ = store.tail_events(max(0, int(after)))
    except Exception:
        return []
    return collect(events, limit=limit)


def _count(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
