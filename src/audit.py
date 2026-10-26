"""审计查询：把散在各会话事件流里的「我批准过 / 我授权过 / 它动过什么」汇总出来。

范围与取舍：

- **只读**。这里没有任何写入口，也不改变策略状态；审计是观察层。
- **按事件类型白名单**。不返回整条会话（消息内容、工具输出都在里面），只挑出安全
  相关的事件，并按键名裁剪字段——避免把用户正文或剪贴板内容顺手导出去。
- **跨会话聚合**。审批、授权、屏幕、桌面、定时任务的事件分布在各自的会话 jsonl 里，
  逐条打开不会成为热点：按修改时间从新到旧扫，命中 ``since`` 或条数上限就停。
- **不信任文件内容**。损坏行逐条跳过；单个会话读失败不影响其它会话。

返回的事件统一补齐 ``session_id`` 与 ``title``（会话标题可能为空，就留空）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

# 允许出现在审计流里的事件类型 → 对外暴露的字段（其余字段一律丢弃）。
# 新增安全相关事件时在这里登记；不登记的事件不会出现在 /v1/audit。
AUDIT_EVENTS: dict[str, tuple[str, ...]] = {
    'approval_timeout': ('request_id', 'tool', 'reason', 'source', 'timeout_s'),
    'screen_captured': ('sha1', 'bytes', 'screen', 'region', 'width', 'height',
                        'duration_ms'),
    'screen_ocr': ('chars', 'lang', 'region', 'lines', 'duration_ms'),
    'clipboard_read': ('bytes', 'chars', 'truncated', 'duration_ms'),
    'clipboard_written': ('chars', 'bytes', 'duration_ms'),
    'desktop_snapshot': ('process', 'pid', 'window_id', 'title_sha1',
                         'sensitive_target'),
    'desktop_snapshot_failed': ('reason',),
    'desktop_action': ('action', 'process', 'pid', 'window_id', 'steps',
                       'duration_ms', 'title_sha1', 'sensitive_target'),
    'desktop_action_failed': ('action', 'reason'),
    'grant_added': ('path', 'scope', 'expires', 'by', 'note'),
    'grant_removed': ('path', 'scope', 'expires', 'by', 'note'),
    'domain_added': ('host', 'scope', 'expires', 'by', 'note'),
    'domain_removed': ('host', 'scope', 'expires', 'by', 'note'),
    'policy_denied': ('tool', 'code', 'zone'),
    'permission_revoked': ('count', 'by'),
}

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
MAX_FIELD_CHARS = 300


@dataclass(frozen=True)
class AuditEvent:
    session_id: str
    type: str
    ts: float
    seq: int
    fields: dict

    def to_dict(self, title: str = '') -> dict:
        return {
            'session_id': self.session_id,
            'title': title,
            'type': self.type,
            'ts': self.ts,
            'seq': self.seq,
            **self.fields,
        }


def _clip(value):
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + '…'
    return value


def project(event: dict) -> AuditEvent | None:
    """把一条原始事件投影成审计事件；类型不在白名单时返回 None。"""
    kind = str(event.get('type') or '')
    fields = AUDIT_EVENTS.get(kind)
    if fields is None:
        return None
    picked = {}
    for name in fields:
        if name in event:
            picked[name] = _clip(event[name])
    try:
        ts = float(event.get('ts') or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return AuditEvent(
        session_id=str(event.get('session_id') or ''),
        type=kind, ts=ts,
        seq=int(event.get('seq') or 0),
        fields=picked,
    )


def _session_title(events: list[dict]) -> str:
    for event in events:
        if event.get('type') == 'session_title':
            return str(event.get('title') or '')
    return ''


def iter_session(path: Path):
    """逐行读一个会话文件，跳过损坏行；不抛异常给调用方。"""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(event, dict):
                    yield event
    except OSError:
        return


def collect(sessions_dir, *, since: float = 0, limit: int = DEFAULT_LIMIT,
            types=None, session_id: str | None = None) -> list[dict]:
    """跨会话收集审计事件，新的在前。

    ``types`` 过滤事件类型；``session_id`` 只看一个会话。扫描按文件修改时间
    从新到旧，一旦攒够 ``limit`` 条且已越过 ``since`` 就停——审计查询不该
    成为大目录的全量解析。
    """
    directory = Path(sessions_dir)
    if not directory.is_dir():
        return []
    wanted = set(types) if types else None
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    pattern = f'session_{session_id}.jsonl' if session_id else 'session_*.jsonl'
    try:
        paths = sorted(directory.glob(pattern),
                       key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return []

    results: list[dict] = []
    for path in paths:
        if len(results) >= limit:
            break
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        events = list(iter_session(path))
        title = _session_title(events)
        picked = []
        for event in events:
            projected = project(event)
            if projected is None:
                continue
            if wanted is not None and projected.type not in wanted:
                continue
            if since and projected.ts < since:
                continue
            picked.append(projected.to_dict(title))
        results.extend(picked)
    results.sort(key=lambda item: (item['ts'], item['seq']), reverse=True)
    return results[:limit]


def summarize(events: list[dict]) -> dict[str, int]:
    """按类型计数，给卡片标题用。"""
    counts: dict[str, int] = {}
    for event in events:
        counts[event['type']] = counts.get(event['type'], 0) + 1
    return counts


def since_from(text: str | None) -> float:
    """`since` 参数：支持 epoch 秒或 ``YYYY-MM-DD``/ISO 时间；非法值返回 0。"""
    text = str(text or '').strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def now() -> float:
    return time.time()
