"""Append-only OpenAI session events and crash-safe replay helpers."""
import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path


class SessionReplayError(ValueError):
    """Raised when a session event stream is invalid before its final line."""


class SessionEventStore:
    """The recovery source of truth; transcript snapshots remain separate exports."""

    _locks: dict[Path, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, directory: Path, session_id: str | None = None, path: Path | None = None):
        self.directory = Path(directory)
        if path is not None:
            self.path = Path(path)
            self.session_id = self.path.stem.removeprefix("session_")
        else:
            self.session_id = session_id or uuid.uuid4().hex
            self.path = self.directory / f"session_{self.session_id}.jsonl"
        self._next_seq = self._read_last_seq() + 1

    @classmethod
    def open(cls, path: Path):
        return cls(Path(path).parent, path=path)

    @classmethod
    def _path_lock(cls, path: Path) -> threading.Lock:
        with cls._locks_guard:
            return cls._locks.setdefault(path.resolve(), threading.Lock())

    def _read_last_seq(self) -> int:
        if not self.path.exists():
            return 0
        events = self.read_events(allow_partial_tail=True)
        return max((int(event.get("seq", 0)) for event in events), default=0)

    def append(self, event_type: str, **payload) -> dict:
        event = {
            "v": 1,
            "seq": self._next_seq,
            "session_id": self.session_id,
            "type": event_type,
            "ts": time.time(),
            **payload,
        }
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._path_lock(self.path):
            # One line is written and flushed before the loop continues. A partial tail is ignored on replay.
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        self._next_seq += 1
        return event

    def append_message(self, message: dict) -> dict:
        return self.append("message", message=copy.deepcopy(message))

    def append_tool_started(self, tool_call) -> dict:
        return self.append(
            "tool_started",
            tool_call_id=tool_call.id,
            name=tool_call.function.name,
            arguments=tool_call.function.arguments,
        )

    def append_compaction(self, summary: str, strategy: str) -> dict:
        return self.append(
            "compaction",
            summary=summary,
            strategy=strategy,
            replaces_through_seq=self._next_seq - 1,
        )

    def read_events(self, *, allow_partial_tail: bool = True) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        events = []
        expected_seq = 1
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                if allow_partial_tail and index == len(lines) - 1:
                    break
                raise SessionReplayError(f"invalid JSONL event at line {index + 1}") from exc
            if not isinstance(event, dict):
                raise SessionReplayError(f"event at line {index + 1} is not an object")
            seq = event.get("seq")
            if seq != expected_seq:
                raise SessionReplayError(
                    f"event sequence mismatch at line {index + 1}: expected {expected_seq}, got {seq}"
                )
            expected_seq += 1
            events.append(event)
        return events

    def rebuild_openai_history(self) -> list[dict]:
        """Rebuild the current compacted OpenAI history from the append-only event stream."""
        messages = []
        for event in self.read_events():
            event_type = event.get("type")
            if event_type == "message":
                message = event.get("message")
                if not isinstance(message, dict) or "role" not in message:
                    raise SessionReplayError(f"invalid message event at seq {event.get('seq')}")
                messages.append(copy.deepcopy(message))
            elif event_type == "compaction":
                summary = event.get("summary")
                if not isinstance(summary, str):
                    raise SessionReplayError(f"invalid compaction event at seq {event.get('seq')}")
                messages = [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]
        self.validate_openai_history(messages, allow_pending=True)
        return messages

    @staticmethod
    def validate_openai_history(messages: list[dict], *, allow_pending: bool = False) -> None:
        pending: set[str] = set()
        for index, message in enumerate(messages):
            role = message.get("role")
            if role == "assistant":
                if pending:
                    raise SessionReplayError(f"missing tool results before assistant message {index}")
                for call in message.get("tool_calls") or []:
                    call_id = call.get("id") if isinstance(call, dict) else None
                    if not call_id or call_id in pending:
                        raise SessionReplayError(f"invalid tool call at assistant message {index}")
                    pending.add(call_id)
            elif role == "tool":
                call_id = message.get("tool_call_id")
                if call_id not in pending:
                    raise SessionReplayError(f"orphan tool result {call_id!r} at message {index}")
                pending.remove(call_id)
            elif pending:
                raise SessionReplayError(f"missing tool results before {role} message {index}")
        if pending and not allow_pending:
            raise SessionReplayError(f"pending tool calls: {', '.join(sorted(pending))}")

    def pending_tool_calls(self) -> list[dict]:
        pending: dict[str, dict] = {}
        for message in self.rebuild_openai_history():
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    if isinstance(call, dict) and call.get("id"):
                        pending[call["id"]] = call
            elif message.get("role") == "tool":
                pending.pop(message.get("tool_call_id"), None)
        return list(pending.values())

    def recovery_message(self) -> dict | None:
        pending = self.pending_tool_calls()
        if not pending:
            return None
        names = []
        for call in pending:
            function = call.get("function") or {}
            names.append(f"{function.get('name', 'unknown')} ({call.get('id')})")
        return {
            "role": "user",
            "content": (
                "<recovery>Session resumed after interruption. Pending tool calls: "
                + ", ".join(names)
                + ". Verify current state before repeating any operation with side effects.</recovery>"
            ),
        }
