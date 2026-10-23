"""代理运行日志：子代理/teammate 独立事件流 + 父会话里的轻量引用事件。

子代理和 teammate 都是嵌套循环，失败时只有最终异常或一句汇报能回到父会话，
中途做了什么没有记录。这里把系统提示、每轮助手消息、工具结果写入会话目录下的
`subagents/`、`teammates/`，父会话只留 started/finished 两个小事件：

- `/sessions` 列表仍只扫 `session_*.jsonl`，不会被内部运行污染；
- 父事件带 `log` 相对路径，排查时能直接定位到完整子会话；
- 子会话事件流本身可被 `SessionEventStore.rebuild_openai_history` 还原，
  进程在工具调用中途退出时也能按 pending 规则检查。

记录失败绝不能改变代理的结果：所有写入都是 best-effort。
"""
from __future__ import annotations

import re
import time
import uuid

from session_store import SessionEventStore

_MAX_DESCRIPTION = 4000
_MAX_PARENT_DESCRIPTION = 500
_MAX_ERROR = 500


def active_parent_store():
    """当前回合的会话存储；teammate 线程没有 checkpoint manager 时为 None。"""
    try:
        import harness
        manager = harness.active_checkpoint_manager()
        return getattr(manager, 'session_store', None)
    except Exception:
        return None


def _path_component(value) -> str:
    """会话文件名片段：teammate 名字已校验，这里再兜一层防路径分隔符。"""
    cleaned = re.sub(r'[^A-Za-z0-9_-]', '_', str(value or ''))[:64]
    return cleaned or 'agent'


class AgentRunRecorder:
    """一次代理运行的事件记录器；无父会话时退化为空操作。"""

    kind = 'agent'
    directory_name = 'agents'

    def __init__(self, description: str, *, parent=None):
        self.id = uuid.uuid4().hex[:12]
        self.description = str(description or '')[:_MAX_DESCRIPTION]
        self.turns = 0
        self._started = time.perf_counter()
        self._finished = False
        self.parent = None
        self.store = None
        parent = parent if parent is not None else active_parent_store()
        if parent is None:
            return
        try:
            directory = parent.path.parent / self.directory_name
            store = SessionEventStore(directory, session_id=self._session_id())
            store.append(f'{self.kind}_started', **self._start_payload())
        except Exception:
            # 事件记录是旁路：目录不可写/磁盘满时继续执行代理。
            return
        self.store = store
        try:
            parent.append(
                f'{self.kind}_started',
                **self._parent_reference(),
                description=self.description[:_MAX_PARENT_DESCRIPTION],
                log=self._log_name(store, parent),
            )
            self.parent = parent
        except Exception:
            # 子日志已可写，父引用失败不影响它继续记录。
            self.parent = None

    def _session_id(self) -> str:
        return f'{self.kind}_{self.id}'

    def _start_payload(self) -> dict:
        return {'description': self.description}

    def _parent_reference(self) -> dict:
        return {f'{self.kind}_id': self.id}

    @staticmethod
    def _log_name(store, parent) -> str:
        try:
            return store.path.relative_to(parent.path.parent).as_posix()
        except (AttributeError, ValueError):
            return str(store.path)

    def _append(self, store, event_type: str, **payload) -> None:
        if store is None:
            return
        try:
            store.append(event_type, **payload)
        except Exception:
            pass

    def log_message(self, message) -> None:
        """记录一条 OpenAI 消息；非 dict（SDK 对象）在 model_dump 后传入。"""
        if self.store is None or not isinstance(message, dict):
            return
        try:
            self.store.append_message(message)
        except Exception:
            pass

    def log_messages(self, messages) -> None:
        for message in messages or ():
            self.log_message(message)

    def finish(self, status: str, *, result: str = '', error_type: str = '',
               error: str = '') -> None:
        """写结束事件；重复调用只生效一次。内容只在子会话里，父会话不复制。"""
        if self._finished:
            return
        self._finished = True
        duration_ms = round((time.perf_counter() - self._started) * 1000, 3)
        common = {
            'turns': self.turns,
            'duration_ms': duration_ms,
            'result_chars': len(result or ''),
            'status': status,
        }
        self._append(
            self.store, f'{self.kind}_finished', **common,
            error_type=error_type, error=str(error or '')[:_MAX_ERROR],
        )
        self._append(
            self.parent, f'{self.kind}_finished',
            **self._parent_reference(), **common,
            log=self._log_name(self.store, self.parent) if self.store else '',
            error_type=error_type,
        )


class SubagentRecorder(AgentRunRecorder):
    """一次子代理运行的事件记录器；无父会话时退化为空操作。"""

    kind = 'subagent'
    directory_name = 'subagents'

    def _session_id(self) -> str:
        return f'subagent_{self.id}'

    def _parent_reference(self) -> dict:
        return {'subagent_id': self.id}


class TeammateRecorder(AgentRunRecorder):
    """一次 teammate 线程运行；父 store 由派发方显式传入（ContextVar 不跨线程）。"""

    kind = 'teammate'
    directory_name = 'teammates'

    def __init__(self, name: str, role: str, description: str, *, parent=None):
        self.name = _path_component(name)
        self.role = str(role or '')[:200]
        super().__init__(description, parent=parent)

    def _session_id(self) -> str:
        return f'teammate_{self.name}_{self.id}'

    def _start_payload(self) -> dict:
        return {'name': self.name, 'role': self.role,
                'description': self.description}

    def _parent_reference(self) -> dict:
        return {'teammate': self.name, 'run_id': self.id, 'role': self.role}
