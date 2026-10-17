"""Reusable session service around the existing agent turn executor.

The runtime deliberately owns session orchestration, while the caller supplies
the actual turn executor. This keeps CLI, SDK, and HTTP adapters on one path
without making the core loop depend on a particular transport.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Type
from workspace_context import resolve_workspace, use_workspace


@dataclass(frozen=True)
class AgentResult:
    """Result returned by one completed user turn."""

    session_id: str
    content: str


class SessionAlreadyExistsError(ValueError):
    """Raised when a caller tries to create an existing session ID."""


# 会话摘要缓存 (path, size, mtime_ns) → summary：飞书 /sessions 每次列表都要
# 概括全部会话文件，store 的实例级缓存跨列表失效；key 含 size/mtime，
# 追加自动过期。上限兜底防爆炸（陈旧 key 只占少量内存）。
_summary_cache: dict[tuple, dict] = {}


def _summary_cached(path, stat) -> dict | None:
    """摘要缓存命中则返回浅拷贝；未命中返回 None（调用方再决定是否解析）。"""
    hit = _summary_cache.get((str(path), stat.st_size, stat.st_mtime_ns))
    return dict(hit) if hit is not None else None


def _cached_summary(store) -> dict:
    stat = store.path.stat()
    hit = _summary_cached(store.path, stat)
    if hit is not None:
        return hit
    hit = store.session_summary()
    stat = store.path.stat()
    if len(_summary_cache) > 512:
        _summary_cache.clear()
    _summary_cache[(str(store.path), stat.st_size, stat.st_mtime_ns)] = hit
    return dict(hit)   # 浅拷贝：调用方（如 search_all 加 matched）不得污染缓存


class AgentRuntime:
    """Coordinate persisted sessions and invoke a supplied agent turn runner."""

    def __init__(
        self,
        run_turn: Callable[..., str],
        session_store_cls: Type,
        sessions_dir: Path,
        resolve_session_path: Callable[[str], Path] | None = None,
        checkpoints_dir: Path | None = None,
        before_session_change: Callable | None = None,
        workspace: Path | None = None,
    ) -> None:
        self._run_turn = run_turn
        self._session_store_cls = session_store_cls
        self._sessions_dir = Path(sessions_dir)
        self._resolve_session_path = resolve_session_path
        from session_cleanup import SessionArchive
        self.archive = SessionArchive(self._sessions_dir, checkpoints_dir or self._sessions_dir.parent / 'checkpoints')
        self._before_session_change = before_session_change
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._default_workspace = Path(workspace or Path.cwd()).resolve()
        self._workspace_root: Path | None = None

    def configure_workspace_root(self, root: Path) -> None:
        """Constrain remote workspaces during application setup."""
        self._default_workspace = resolve_workspace(root, base=Path.cwd())
        self._workspace_root = self._default_workspace

    def create_session(self, session_id: str | None = None, *, workspace: str | None = None) -> str:
        """Create an empty persisted session and return its identifier."""
        session_id = session_id or uuid.uuid4().hex
        if not session_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Invalid session ID")
        with self._session_lock(session_id):
            path = self._sessions_dir / f"session_{session_id}.jsonl"
            if path.exists():
                raise SessionAlreadyExistsError(f"Session {session_id} already exists")
            directory = resolve_workspace(workspace if workspace is not None else self._default_workspace,
                                          base=self._default_workspace, allowed_root=self._workspace_root)
            store = self._session_store_cls(self._sessions_dir, session_id=session_id)
            store.append("session_created")
            store.record_workspace(directory)
            return store.session_id

    def _workspace_for(self, store) -> Path:
        saved = store.session_summary()['workspace']
        return resolve_workspace(saved or self._default_workspace,
                                 base=self._default_workspace, allowed_root=self._workspace_root)

    def get_workspace(self, session_id: str) -> str:
        return str(self._workspace_for(self._open_session(session_id)))

    def set_workspace(self, session_id: str, path: str) -> str:
        lock = self._session_lock(session_id)
        if not lock.acquire(blocking=False):
            raise RuntimeError('会话正在执行任务，请稍后切换工作区')
        try:
            store = self._open_session(session_id)
            base = Path(store.session_summary()['workspace'] or self._default_workspace)
            workspace = resolve_workspace(path, base=base, allowed_root=self._workspace_root)
            store.record_workspace(workspace)
            return str(workspace)
        finally:
            lock.release()

    def send(self, session_id: str, content: str, on_delta=None) -> AgentResult:
        """Append a user message, run one turn, and return the assistant content."""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("message content must be a non-empty string")
        with self._session_lock(session_id):
            store = self._open_session(session_id)
            workspace = self._workspace_for(store)
            store.record_workspace(workspace)
            messages = store.rebuild_openai_history(recover_pending=True)
            message = {"role": "user", "content": content}
            messages.append(message)
            store.append_message(message)
            with use_workspace(workspace):
                if on_delta is None:
                    result = self._run_turn(messages, session_store=store)
                else:
                    result = self._run_turn(messages, session_store=store, on_delta=on_delta)
            return AgentResult(session_id=store.session_id, content=result)

    def history(self, session_id: str) -> list[dict]:
        """Return the replayable OpenAI history for a persisted session."""
        return self._open_session(session_id).rebuild_openai_history(repair_gaps=True)

    def open_session(self, session_id: str):
        """Open the persisted event store of an existing session (live tailing)."""
        return self._open_session(session_id)

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """Summarize sessions ordered by last user/assistant message time."""
        if not self._sessions_dir.is_dir():
            return []
        paths = self._sessions_dir.glob('session_*.jsonl')
        rows = []
        for path in paths:
            try:
                hit = _summary_cached(path, path.stat())
            except OSError:
                continue   # unreadable log: leave it out of the listing
            if hit is not None:
                rows.append(hit)   # 缓存命中：连 store 都不必构造
                continue
            try:
                store = self._session_store_cls.open(path)
                rows.append(_cached_summary(store))
            except (OSError, ValueError):
                continue   # unreadable/corrupt log: leave it out of the listing
        rows.sort(key=lambda row: (row['updated'], row['id']), reverse=True)
        return rows[:limit]

    def search_sessions(self, query: str, limit: int = 10) -> list[dict]:
        """跨会话内容搜索（含命中片段），按最近优先。"""
        return self._session_store_cls.search_all(self._sessions_dir, query, limit)

    def fork_session(self, session_id: str, upto_seq: int | None = None) -> str:
        """从既有会话分叉出新会话（原会话不动），返回新会话 ID。"""
        source = self.open_session(session_id)
        forked = self._session_store_cls.fork(source, upto_seq=upto_seq)
        return forked.session_id

    def rename_session(self, session_id: str, title: str) -> str:
        """设置会话主题（覆盖自动生成的标题），返回生效后的标题。"""
        title = (title or '').strip()
        if not title:
            raise ValueError('标题不能为空')
        store = self._open_session(session_id)
        store.append_session_title(title[:80], source='user')   # 用户命名：异步重生成永不覆盖
        return store.session_summary()['title']

    def manage_session(self, action: str, row: dict):
        if action not in ('archive', 'restore', 'delete'):
            raise ValueError('未知会话操作')
        session_id = row.get('id')
        if (not isinstance(session_id, str) or not session_id
                or not session_id.replace('-', '').replace('_', '').isalnum()):
            raise ValueError('无效的会话标识')
        lock = self._session_lock(row['id'])
        if not lock.acquire(blocking=False):
            raise RuntimeError('会话正在执行任务，请稍后重试')
        try:
            if self._before_session_change:
                self._before_session_change()
            if action == 'restore':
                if row not in self.archive.list():
                    raise ValueError('归档已变化，请重新选择')
                self.archive.restore(row['id'])
            elif action == 'archive':
                self.archive.archive(row, '')
            else:
                self.archive.delete(row, '')
        finally:
            lock.release()

    def _open_session(self, session_id: str):
        if self._resolve_session_path is not None:
            path = self._resolve_session_path(session_id)
        else:
            path = self._sessions_dir / f"session_{session_id}.jsonl"
            if not path.is_file():
                raise FileNotFoundError(f"Session {session_id} not found")
        return self._session_store_cls.open(path)

    def _session_lock(self, session_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.RLock())
