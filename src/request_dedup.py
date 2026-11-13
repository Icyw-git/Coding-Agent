"""入站请求去重：同一条消息重复送达，只能受理一次。

为什么需要它
------------
飞书 / API / SDK 都会重发：网络抖动后客户端重试、网关重启后重放未 ack 的消息、
用户手滑发两遍。没有去重键时，同一条"帮我跑一下测试"会开两个回合——第一个已经在
跑，第二个排队，用户看到两次执行。

形状为什么是 append-only
------------------------
调研（`scripts/bench_request_dedup.py`，结论见 PERFORMANCE_PROFILE.md）：

| 记录数 | 单文件 JSON 读-改-写 | append-only JSONL（含 fsync） |
| --- | --- | --- |
| 1000 | 19.9ms | 0.88ms |
| 10000 | **50.5ms** | **0.85ms** |

单文件重写在 1 万级就要 50ms，而它落在**每条入站请求**的热路径上——和"每次判定都读
授权账本"（R1-b）是同一个错误形状。所以：追加写文件、索引放内存、容量判定读内存。

容量
----
真实数据 10 天 752 条 · 日均 75 · 单日峰值 341 → 按峰值外推 7 天 = 2387 条。
10000 条给了约 4 倍余量。反过来说，撞上限需要约 1428 条/天（持续每分钟一条）。

满容量时**拒绝新的带 id 受理**，不淘汰未过期的记录：抢先淘汰会让"刚受理过的请求"
重新变成新请求，那正是去重要防的事。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import paths

STORE_PATH = paths.cache_path('requests', 'dedup.jsonl')

#: 保留期。过期后不承诺去重（§9.4：第一版 7 天）。
RETENTION_SECONDS = 7 * 24 * 3600
#: 每状态根最多多少条。见模块文档的容量推导。
MAX_RECORDS = 10_000

_LOCK = threading.RLock()
#: request_id → 记录。**只在内存里**建索引；文件是追加的事实源。
_INDEX: dict[str, dict] | None = None


class _State:
    """小幅可变容器，便于测试里 reset 而不必重导入模块。"""


def reset_cache() -> None:
    """丢掉内存索引（测试/诊断用）。下次访问会从文件重建。"""
    global _INDEX
    with _LOCK:
        _INDEX = None


def _now() -> float:
    return time.time()


def fingerprint(*, text: str = '', workspace: str = '', mode: str = '',
                attachments=()) -> str:
    """内容指纹：同 id 的请求要用它判断"是不是同一次请求"。

    包含执行相关输入（文本、工作区、模式、附件名），不含时间戳/来源——
    同一条消息从飞书重发到 SDK 也该算同一个请求。
    """
    parts = [str(text or ''), str(workspace or ''), str(mode or ''),
             ','.join(str(item) for item in attachments or ())]
    return hashlib.sha256('\x00'.join(parts).encode('utf-8')).hexdigest()[:32]


def _read_rows() -> list[dict]:
    path = Path(STORE_PATH)
    if not path.is_file():
        return []
    rows = []
    try:
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue          # 写了一半的行：跳过，不影响已有索引
                if isinstance(row, dict) and row.get('request_id'):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _index() -> dict[str, dict]:
    """从文件重建索引（只保留未过期的）。

    同一 id 会有多行（受理 + 绑定回合），必须**按 id 合并**后建索引——直接逐行覆盖
    会让后写的绑定行把指纹冲掉，重启后同一条消息就被判成「同 id 异内容」。
    """
    global _INDEX
    with _LOCK:
        if _INDEX is not None:
            return _INDEX
        cutoff = _now() - RETENTION_SECONDS
        index = {}
        for key, row in _latest(_read_rows()).items():
            try:
                stamp = float(row.get('created_at') or 0)
            except (TypeError, ValueError):
                continue
            if stamp < cutoff:
                continue
            index[key] = row
        _INDEX = index
        return _INDEX


def _latest(rows: list[dict]) -> dict[str, dict]:
    """同一 id 可能有多行（受理 + 绑定回合）：后写的覆盖先写的。"""
    merged: dict[str, dict] = {}
    for row in rows:
        current = merged.get(str(row['request_id']), {})
        merged[str(row['request_id'])] = {**current, **row}
    return merged


def _append(row: dict) -> None:
    path = Path(STORE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n'
    with path.open('a', encoding='utf-8', newline='\n') as handle:
        handle.write(payload)
        handle.flush()


def claim(request_id: str, *, fingerprint: str, session_id: str = '',
          origin: str = '') -> dict:
    """尝试受理一次入站请求。

    返回 ``{status, turn_id, reason}``，``status`` 是：

    - ``passthrough``：没有 request_id（旧客户端），不参与去重；
    - ``accepted``：新请求，可以继续执行；
    - ``duplicate``：同 id 同内容，``turn_id`` 是原回合（可能为空=已受理未开始）；
    - ``conflict``：同 id 异内容，拒绝；
    - ``full``：满容量，拒绝新的带 id 受理。
    """
    request_id = str(request_id or '').strip()
    if not request_id:
        return {'status': 'passthrough', 'turn_id': '', 'reason': ''}

    with _LOCK:
        index = _index()
        existing = index.get(request_id)
        if existing is not None:
            if str(existing.get('fingerprint') or '') == str(fingerprint):
                return {'status': 'duplicate',
                        'turn_id': str(existing.get('turn_id') or ''),
                        'reason': ''}
            return {'status': 'conflict', 'turn_id': '',
                    'reason': 'same request id with different content'}
        if len(index) >= MAX_RECORDS:
            return {'status': 'full', 'turn_id': '',
                    'reason': (f'request dedup store is at capacity '
                               f'({MAX_RECORDS} records); unexpired records are '
                               'not evicted to make room')}
        row = {'request_id': request_id, 'fingerprint': str(fingerprint),
               'session_id': str(session_id or ''), 'origin': str(origin or ''),
               'turn_id': '', 'status': 'accepted', 'created_at': _now()}
        _append(row)
        index[request_id] = row
        return {'status': 'accepted', 'turn_id': '', 'reason': ''}


def bind(request_id: str, *, turn_id: str) -> bool:
    """把受理过的请求绑到实际回合上；之后重复请求能查到"原来的那一次"。"""
    request_id = str(request_id or '').strip()
    if not request_id:
        return False
    with _LOCK:
        index = _index()
        existing = index.get(request_id)
        if existing is None:
            return False
        row = {'request_id': request_id, 'turn_id': str(turn_id or ''),
               'status': 'bound', 'created_at': _now()}
        _append(row)
        index[request_id] = {**existing, **row}
        return True


def lookup(request_id: str) -> dict | None:
    with _LOCK:
        row = _index().get(str(request_id or '').strip())
        return dict(row) if row is not None else None


def count() -> int:
    with _LOCK:
        return len(_index())


def compact(*, now: float | None = None) -> int:
    """把过期记录真正删掉（低频调用，不在请求路径上）。返回剩余条数。"""
    stamp = _now() if now is None else float(now)
    cutoff = stamp - RETENTION_SECONDS
    with _LOCK:
        merged = _latest(_read_rows())
        alive = {key: row for key, row in merged.items()
                 if float(row.get('created_at') or 0) >= cutoff}
        path = Path(STORE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(''.join(
            json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n'
            for row in alive.values()), encoding='utf-8')
        tmp.replace(path)
        global _INDEX
        _INDEX = alive
        return len(alive)
