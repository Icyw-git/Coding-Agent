"""子代理运行日志：独立事件流 + 父会话里的轻量引用事件。

子代理是 30 轮的嵌套循环，失败时只有最终异常能回到 `task` 工具，中途做了什么
没有记录。这里把系统提示、每轮助手消息、工具结果写入会话目录下的
`subagents/`，父会话只留 `subagent_started` / `subagent_finished` 两个小事件：

- `/sessions` 列表仍只扫 `session_*.jsonl`，不会被内部运行污染；
- 父事件带 `log` 相对路径，排查时能直接定位到完整子会话；
- 子会话事件流本身可被 `SessionEventStore.rebuild_openai_history` 还原，
  进程在工具调用中途退出时也能按 pending 规则检查。

记录失败绝不能改变子代理的结果：所有写入都是 best-effort。
"""
from __future__ import annotations

import time
import uuid

from session_store import SessionEventStore

_MAX_DESCRIPTION = 4000
_MAX_PARENT_DESCRIPTION = 500
_MAX_ERROR = 500


def _parent_store():
    """当前回合的会话存储；teammate 线程没有 checkpoint manager 时为 None。"""
    try:
        import harness
        manager = harness.active_checkpoint_manager()
        return getattr(manager, 'session_store', None)
    except Exception:
        return None


class SubagentRecorder:
    """一次子代理运行的事件记录器；无父会话时退化为空操作。"""

    def __init__(self, description: str):
        self.id = uuid.uuid4().hex[:12]
        self.description = str(description or '')[:_MAX_DESCRIPTION]
        self.turns = 0
        self._started = time.perf_counter()
        self._finished = False
        self.parent = None
        self.store = None
        parent = _parent_store()
        if parent is None:
            return
        try:
            directory = parent.path.parent / 'subagents'
            store = SessionEventStore(directory, session_id=f'subagent_{self.id}')
            store.append('subagent_started', description=self.description)
        except Exception:
            # 事件记录是旁路：目录不可写/磁盘满时继续执行子代理。
            return
        self.store = store
        try:
            parent.append(
                'subagent_started',
                subagent_id=self.id,
                description=self.description[:_MAX_PARENT_DESCRIPTION],
                log=self._log_name(store, parent),
            )
            self.parent = parent
        except Exception:
            # 子日志已可写，父引用失败不影响它继续记录。
            self.parent = None

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
            self.store, 'subagent_finished', **common,
            error_type=error_type, error=str(error or '')[:_MAX_ERROR],
        )
        self._append(
            self.parent, 'subagent_finished', subagent_id=self.id,
            log=self._log_name(self.store, self.parent) if self.store else '',
            error_type=error_type, **common,
        )
