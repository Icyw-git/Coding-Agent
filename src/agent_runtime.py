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


@dataclass(frozen=True)
class AgentResult:
    """Result returned by one completed user turn."""

    session_id: str
    content: str


class SessionAlreadyExistsError(ValueError):
    """Raised when a caller tries to create an existing session ID."""


class AgentRuntime:
    """Coordinate persisted sessions and invoke a supplied agent turn runner."""

    def __init__(
        self,
        run_turn: Callable[..., str],
        session_store_cls: Type,
        sessions_dir: Path,
        resolve_session_path: Callable[[str], Path] | None = None,
    ) -> None:
        self._run_turn = run_turn
        self._session_store_cls = session_store_cls
        self._sessions_dir = Path(sessions_dir)
        self._resolve_session_path = resolve_session_path
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def create_session(self, session_id: str | None = None) -> str:
        """Create an empty persisted session and return its identifier."""
        session_id = session_id or uuid.uuid4().hex
        if not session_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Invalid session ID")
        with self._session_lock(session_id):
            path = self._sessions_dir / f"session_{session_id}.jsonl"
            if path.exists():
                raise SessionAlreadyExistsError(f"Session {session_id} already exists")
            store = self._session_store_cls(self._sessions_dir, session_id=session_id)
            store.append("session_created")
            return store.session_id

    def send(self, session_id: str, content: str, on_delta=None) -> AgentResult:
        """Append a user message, run one turn, and return the assistant content."""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("message content must be a non-empty string")
        store = self._open_session(session_id)
        with self._session_lock(session_id):
            messages = store.rebuild_openai_history()
            message = {"role": "user", "content": content}
            messages.append(message)
            store.append_message(message)
            if on_delta is None:
                result = self._run_turn(messages, session_store=store)
            else:
                result = self._run_turn(messages, session_store=store, on_delta=on_delta)
            return AgentResult(session_id=store.session_id, content=result)

    def history(self, session_id: str) -> list[dict]:
        """Return the replayable OpenAI history for a persisted session."""
        return self._open_session(session_id).rebuild_openai_history()

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
                store = self._session_store_cls.open(path)
                title = store.session_title()
                summary = store.session_summary()
            except (OSError, ValueError):
                continue   # unreadable/corrupt log: leave it out of the listing
            rows.append({
                'id': store.session_id,
                'title': title,
                'events': store.last_seq,
                'created': summary['created'],
                'updated': summary['updated'],
            })
        rows.sort(key=lambda row: (row['updated'], row['id']), reverse=True)
        return rows[:limit]

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
