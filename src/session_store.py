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


# 纯确认语不配当会话标题（generate_session_title 的忽略清单同源）
_GENERIC_ACKS = {'go', 'ok', 'okay', 'yes', 'y', '嗯', '行', '好的',
                 '继续', 'continue', 'do it', 'start'}


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
        # 锁在初始化时解析一次（每次 append 都 resolve 约 10ms 开销）；
        # 同路径的多实例仍从类级 _locks 取到同一把锁
        self._lock = self._path_lock(self.path)
        self._known_size = self.path.stat().st_size if self.path.exists() else 0

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
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            # Re-read under the path lock so separate store instances cannot reuse a sequence number.
            current_size = self.path.stat().st_size if self.path.exists() else 0
            if current_size != self._known_size:
                self._next_seq = self._read_last_seq() + 1
            next_seq = self._next_seq
            event = {
                "v": 1,
                "seq": next_seq,
                "session_id": self.session_id,
                "type": event_type,
                "ts": time.time(),
                **payload,
            }
            encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            # One line is written and flushed before the loop continues. A partial tail is ignored on replay.
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._next_seq = next_seq + 1
            self._known_size = current_size + len(encoded.encode('utf-8'))
        return event

    def append_message(self, message: dict) -> dict:
        return self.append("message", message=copy.deepcopy(message))

    def append_session_title(self, title: str) -> dict:
        """Persist a short human-readable topic for session listings."""
        return self.append("session_title", title=title)

    def record_workspace(self, workspace: Path) -> None:
        """Record changes only; browsing a legacy session must not invent its cwd."""
        value = str(Path(workspace).resolve())
        if self.session_summary()['workspace'] != value:
            self.append('workspace_changed', workspace=value)

    def session_summary(self) -> dict:
        """Local listing metadata; never calls a model or replays checkpoints."""
        events = self.read_events()
        workspace = None
        title = None
        prompts = []
        created = events[0]['ts'] if events else 0
        last_message = None
        last_user = last_assistant = ''
        last_useful_user = ''
        for event in events:
            if event.get('workspace'):
                workspace = str(event['workspace'])
            if event.get('type') == 'session_title' and event.get('title'):
                title = str(event['title'])
            message = event.get('message') or {}
            content = message.get('content')
            if event.get('type') == 'message' and message.get('role') in ('user', 'assistant'):
                last_message = event['ts']
                if isinstance(content, str) and content.strip():
                    if message['role'] == 'user':
                        stripped = content.strip()
                        last_user = stripped[:1000]
                        # 「最近任务」跳过纯确认语/单字符噪声，露出最后一条有效输入
                        if stripped.casefold() not in _GENERIC_ACKS and len(stripped) > 1:
                            last_useful_user = stripped[:1000]
                    else:
                        last_assistant = content.strip()[:2000]
            if (event.get('type') == 'message' and message.get('role') == 'user'
                    and isinstance(content, str) and content.strip()):
                prompts.append(' '.join(content.split()))
        # session_title 事件是正式生成的标题（LLM/用户设置）：非系统注入、
        # 非纯确认语（'go'/'好的'/…——显式宣告本会话无好标题）时直接采用。
        # _useful_title_line 的最短长度规则只约束从 prompts 提取的 fallback；
        # 兜底链最后保留 prompts[0]（第一条用户消息比 '新会话' 有信息量）。
        system_junk = bool(title) and title.lstrip().startswith(('[', '<'))
        title_is_ack = bool(title) and title.strip().casefold() in _GENERIC_ACKS
        meaningful = [p for p in prompts
                      if p.casefold() not in _GENERIC_ACKS]
        if title and not system_junk and not title_is_ack:
            chosen = title[:80]
        elif title_is_ack:
            chosen = '新会话'
        else:
            chosen = next((p[:80] for p in meaningful if _useful_title_line(p)),
                          meaningful[0][:80] if meaningful else '新会话')
        return {
            'id': self.session_id,
            'title': chosen,
            'workspace': workspace,
            'preview': ((' '.join((last_useful_user or last_user).split()))[:160]
                        if (last_useful_user or last_user) else '尚无任务'),
            'last_user': last_useful_user or last_user,
            'last_assistant': last_assistant,
            'events': len(events),
            'created': created,
            # Empty sessions use creation time; metadata and tool events never
            # advance the last conversational activity timestamp.
            'updated': last_message if last_message is not None else created,
        }

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
        # 最终回退：首条用户消息原文（短也无妨，比 "(untitled session)" 更有辨识度）
        if user_messages:
            return user_messages[0][:60]
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

    def tail_events(self, offset: int = 0, *, strict: bool = False) -> tuple[list[dict], int]:
        """Incrementally read complete JSONL events from a byte offset.

        Returns (events, new_offset). A trailing partial line (mid-write) is
        left unconsumed until it is completed, so a live tailer never sees
        torn events. Corrupt lines are skipped to keep the tail resilient.
        ``strict=True`` validates an initial snapshot from offset zero.
        """
        if offset < 0:
            offset = 0
        if not self.path.exists():
            return [], offset
        with open(self.path, 'rb') as handle:
            handle.seek(offset)
            chunk = handle.read()
        if not chunk:
            return [], offset
        events = []
        expected_seq = 1
        lines = chunk.split(b'\n')
        lines.pop()   # trailing piece: complete only when chunk ended with \n
        for raw in lines:
            offset += len(raw) + 1
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                if strict:
                    raise SessionReplayError('invalid JSONL event in snapshot') from exc
                continue   # torn/corrupt line: skip, keep the tail alive
            if strict:
                if not isinstance(event, dict) or event.get('seq') != expected_seq:
                    raise SessionReplayError('invalid event sequence in snapshot')
                expected_seq += 1
            if isinstance(event, dict):
                events.append(event)
        return events, offset

    def rebuild_openai_history(self, *, recover_pending: bool = False,
                               repair_gaps: bool = False) -> list[dict]:
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
            elif event_type == "tool_recovery":
                messages = self.close_interrupted_tools(messages)
        if repair_gaps and not recover_pending:
            # Read-only views repair old gaps, but an active tail may still be executing.
            messages = self.close_interrupted_tools(messages, close_tail=False)
        if recover_pending:
            recovered = self.close_interrupted_tools(messages)
            self.validate_openai_history(recovered)
            if recovered != messages:
                self.append('tool_recovery')
                messages = recovered
        self.validate_openai_history(messages, allow_pending=True)
        return messages

    @staticmethod
    def close_interrupted_tools(messages: list[dict], *, close_tail: bool = True) -> list[dict]:
        """Close missing tool results without claiming success or repeating tools."""
        result = []
        pending = {}

        def close_pending():
            for call_id in pending:
                result.append({
                    'role': 'tool', 'tool_call_id': call_id,
                    'content': ('Error: Tool execution was interrupted; no result was recorded. '
                                'The outcome is unknown. Verify current state before retrying '
                                'any operation with side effects.'),
                })
            pending.clear()

        for message in messages:
            if message.get('role') != 'tool':
                close_pending()
            result.append(message)
            if message.get('role') == 'assistant':
                for call in message.get('tool_calls') or []:
                    pending[call['id']] = call
            elif message.get('role') == 'tool':
                pending.pop(message.get('tool_call_id'), None)
        if close_tail:
            close_pending()
        return result

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

    # ---------- 分叉与跨会话搜索 ----------

    FORK_EVENT_TYPES = ('message', 'compaction', 'session_title', 'workspace_changed')

    @classmethod
    def fork(cls, source: 'SessionEventStore', upto_seq: int | None = None,
             session_id: str | None = None) -> 'SessionEventStore':
        """从源会话分叉出新会话：复制 ≤upto_seq 的可重放事件，原会话不动。

        checkpoint/rewind/tool_started 等与旧会话绑定的管理事件不复制；
        截断处的孤儿工具消息由 rebuild 的 repair 逻辑闭合。标题原样保留，
        需要区分时用户可用 /rename 修改。
        """
        new = cls(source.directory, session_id=session_id)
        if new.path.exists():
            raise FileExistsError(f'fork target already exists: {new.session_id}')
        for event in source.read_events():
            if upto_seq is not None and event.get('seq', 0) > upto_seq:
                break
            etype = event.get('type')
            if etype not in cls.FORK_EVENT_TYPES:
                continue
            payload = {key: value for key, value in event.items()
                       if key not in ('v', 'seq', 'session_id', 'type', 'ts')}
            new.append(etype, **payload)
        new.rebuild_openai_history()   # 触发孤儿修复与历史校验
        return new

    @classmethod
    def search_all(cls, directory: Path, query: str, limit: int = 10) -> list[dict]:
        """跨会话内容搜索：按最近优先返回命中会话（摘要 + 命中片段）。

        先对原始文本做一次粗筛（避免逐文件全量 JSON 解析），命中后再
        解析事件并定位首个命中的 user/assistant 消息片段。
        """
        needle = query.strip().casefold()
        if not needle:
            return []
        hits: list[dict] = []
        for path in sorted(Path(directory).glob('session_*.jsonl'),
                           key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                if needle not in path.read_text(encoding='utf-8', errors='ignore').casefold():
                    continue
                store = cls.open(path)
            except (OSError, ValueError):
                continue
            summary = store.session_summary()
            snippet = None
            for event in store.read_events():
                if event.get('type') != 'message':
                    continue
                message = event.get('message') or {}
                content = message.get('content')
                if isinstance(content, str):
                    pos = content.casefold().find(needle)
                    if pos >= 0:
                        role = {'user': '用户', 'assistant': '助手'}.get(
                            message.get('role'), message.get('role') or '?')
                        snippet = f"{role}: {content[max(0, pos - 40):pos + 80]}".strip()
                        break
            if snippet is None and needle in summary['title'].casefold():
                # 粗筛按全文命中（含标题事件）：正文找不到时标题命中也算命中，
                # 否则只改过标题的会话会从搜索里消失
                snippet = f"主题: {summary['title']}"
            if snippet is None:
                continue
            summary['matched'] = ' '.join(snippet.split())[:160]
            hits.append(summary)
            if len(hits) >= limit:
                break
        return hits


def _useful_title_line(content: str) -> str | None:
    """Choose a useful single-line fallback, ignoring short acknowledgments."""
    # 压缩转储/系统注入类消息整条跳过：它们的正文行（如 '- read completed'）
    # 不是会话主题，逐行筛选会把碎片误当标题。
    content_head = ' '.join(content.split()).casefold()
    if content_head.startswith((
        'tool results executed before compaction:',
        '[compacted]',
        '[reactive compact]',
    )):
        return None
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
        # 单行消息：整行就是标题本身，不截断；多行才限长
        if len([l for l in content.splitlines() if l.strip()]) == 1:
            return line
        return line[:60]
    return None
