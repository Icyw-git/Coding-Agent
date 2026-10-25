"""会话管理工具：把 /find、/rename、/revoke 的能力交给模型。

这三个操作以前只存在于飞书斜杠命令和 TUI 里；聊天用户不会输入命令，
所以补成工具。读操作直接扫会话目录，写操作只碰当前回合已打开的
SessionEventStore（标题事件是 append-only，不需要再抢会话文件锁）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from session_store import SessionEventStore


def _h():
    import harness
    return harness


def _gate():
    """延迟导入：hooks 在模块级依赖 tools，反向只能按需取。"""
    import hooks
    return hooks


def run_find_sessions(query: str, limit: Optional[int] = 5) -> str:
    query = (query or '').strip()
    if not query:
        return 'Error: query must not be empty'
    try:
        count = int(limit) if limit is not None else 5
    except (TypeError, ValueError):
        count = 5
    count = min(max(count, 1), 20)
    rows = SessionEventStore.search_all(
        Path(_h().SESSIONS_DIR), query, count)
    if not rows:
        return f'No sessions contain {query!r}.'
    lines = []
    for row in rows:
        title = row.get('title') or '(untitled)'
        workspace = row.get('workspace') or '(unknown workspace)'
        lines.append(f"{row['id'][:12]} · {title}\n"
                     f"  workspace: {workspace}\n"
                     f"  match: {row.get('matched', '')}")
    return '\n'.join(lines)


def run_rename_session(title: str) -> str:
    title = (title or '').strip()
    if not title:
        return 'Error: title must not be empty'
    manager = _h().active_checkpoint_manager()
    store = getattr(manager, 'session_store', None)
    if store is None:
        return 'Error: no active session to rename'
    title = title[:80]
    # source='user'：自动标题整理不会覆盖用户显式命名的会话。
    store.append_session_title(title, source='user')
    return f'Renamed current session to: {title}'


def run_revoke_permissions() -> str:
    try:
        gate = _gate()
        count = gate.GATE.revoke_session(gate.current_session_id() or None)
    except Exception as exc:
        return f'Error: {type(exc).__name__}: {exc}'
    return f'Revoked {count} permission grant(s) for this session.'
