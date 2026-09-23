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

    @property
    def last_seq(self) -> int:
        """最后一个已写入事件的 seq（还没写过事件时为 0）。"""
        return self._next_seq - 1

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

    def append_session_title(self, title: str) -> dict:
        """Persist a short human-readable topic for session listings."""
        return self.append("session_title", title=title)

    def session_title(self) -> str:
        """Return the saved topic, or derive one from the first user message for older logs."""
        title = self.saved_session_title()
        if title:
            return title
        events = self.read_events()
        user_messages = []
        assistant_messages = []
        for event in events:
            if event.get("type") != "message":
                continue
            message = event.get("message") or {}
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if message.get("role") == "user":
                user_messages.append(content.strip())
            elif message.get("role") == "assistant":
                assistant_messages.append(content.strip())
        for content in user_messages:
            title = _useful_title_line(content)
            if title:
                return title
        for content in assistant_messages:
            title = _useful_title_line(content)
            if title:
                return title
        return "(untitled session)"

    def saved_session_title(self) -> str | None:
        """Return only an explicitly persisted title, if present."""
        for event in reversed(self.read_events()):
            if event.get("type") == "session_title" and event.get("title"):
                return str(event["title"])
        return None


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

    def append_checkpoint(self, checkpoint_id: str, through_seq: int | None = None, *,
                          label: str = "", trigger: str = "turn") -> dict:
        """记录一个可回溯节点；快照本体在 .cache/checkpoints/<id>/ 下，不进事件流。"""
        return self.append(
            "checkpoint",
            checkpoint_id=checkpoint_id,
            through_seq=self.last_seq if through_seq is None else through_seq,
            label=label,
            trigger=trigger,
        )

    def append_rewind(self, checkpoint_id: str, *, scope: str = "session+files",
                      snapshot_path: str | None = None, restored_files: list | None = None) -> dict:
        """记录一次回溯；重放时用它把历史替换回该节点的快照。"""
        return self.append(
            "rewind",
            checkpoint_id=checkpoint_id,
            scope=scope,
            snapshot_path=snapshot_path,
            restored_files=list(restored_files or []),
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
            elif event_type == "rewind":
                messages = self._rewound_history(event)
        self.validate_openai_history(messages, allow_pending=True)
        return messages

    def _rewound_history(self, event: dict) -> list[dict]:
        """回溯事件：用 checkpoint 的 messages 快照替换此前历史，并标注回滚点。"""
        checkpoint_id = event.get("checkpoint_id")
        raw_path = event.get("snapshot_path")
        if not checkpoint_id or not raw_path:
            raise SessionReplayError(f"invalid rewind event at seq {event.get('seq')}")
        snapshot_path = Path(raw_path)
        if not snapshot_path.exists():
            raise SessionReplayError(
                f"rewind at seq {event.get('seq')}: snapshot missing for {checkpoint_id}")
        messages = []
        for line in snapshot_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            message = json.loads(line)
            if not isinstance(message, dict) or "role" not in message:
                raise SessionReplayError(f"invalid message in snapshot {checkpoint_id}")
            messages.append(message)
        if self._pending_ids(messages):
            # 快照停在「工具已发出、结果未回来」的位置：此时插入 user 消息会破坏配对
            return messages
        return messages + [{
            "role": "user",
            "content": f"[Rewound to {checkpoint_id}] Conversation history restored to this checkpoint.",
        }]

    @staticmethod
    def _pending_ids(messages: list[dict]) -> set[str]:
        pending: set[str] = set()
        for message in messages:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    if isinstance(call, dict) and call.get("id"):
                        pending.add(call["id"])
            elif message.get("role") == "tool":
                pending.discard(message.get("tool_call_id"))
        return pending

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


def _useful_title_line(content: str) -> str | None:
    """Choose a useful single-line fallback, ignoring short acknowledgments."""
    for line in content.splitlines():
        line = " ".join(line.split()).strip()
        normalized = line.casefold()
        if len(line) < 12 or line.startswith(("[", "<")):
            continue
        if normalized.startswith((
            "tool results executed before compaction:",
            "- out:",
            "done after compact",
            "final answer",
            "recovered",
            "<task_notification>",
            "<reminder>",
            "[scheduled]",
        )):
            continue
        words = normalized.split()
        if len(words) >= 4 and len(set(words)) == 1:
            continue
        return line[:60]
    return None
