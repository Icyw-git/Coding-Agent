"""围绕既有回合执行器的可复用会话服务。

runtime 只负责会话编排，实际回合执行由调用方注入；CLI、SDK 与 HTTP 适配器
因此共用一条路径，而核心循环不必依赖具体传输层。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Type

import workspaces
from agent_core import image_attachment_message
from workspace_choices import RECENT_LIMIT, workspace_choices
from workspace_context import resolve_workspace, use_workspace


@dataclass(frozen=True)
class AgentResult:
    """一个已完成用户回合的结果。"""

    session_id: str
    content: str


class SessionAlreadyExistsError(ValueError):
    """调用方试图创建已存在的会话 ID。"""


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
    return dict(hit)  # 浅拷贝：调用方（如 search_all 加 matched）不得污染缓存


class AgentRuntime:
    """编排持久化会话，并调用注入的回合执行器。"""

    def __init__(
        self,
        run_turn: Callable[..., str],
        session_store_cls: Type,
        sessions_dir: Path,
        resolve_session_path: Callable[[str], Path] | None = None,
        checkpoints_dir: Path | None = None,
        before_session_change: Callable | None = None,
        workspace: Path | None = None,
        workspace_file: Path | None = None,
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
        self._workspace_roots: list[Path] | None = None
        self._workspace_file = Path(workspace_file).expanduser() if workspace_file else None

    @property
    def sessions_dir(self) -> Path:
        return self._sessions_dir

    @property
    def default_workspace(self) -> Path:
        return self._default_workspace

    @property
    def allowed_roots(self) -> list[Path]:
        return list(self._workspace_roots or ())

    @property
    def workspace_file(self) -> Path:
        return self._workspace_file or workspaces.default_path(self._sessions_dir)

    def configure_workspace_roots(self, roots) -> None:
        """把远程可切换的工作区限制在启动时授权的根内。"""
        resolved = []
        for root in roots:
            path = resolve_workspace(root, base=Path.cwd())
            if path not in resolved:
                resolved.append(path)
        if not resolved:
            raise ValueError('At least one workspace root is required')
        self._workspace_roots = resolved
        self._default_workspace = resolved[0]

    def configure_workspace_root(self, root: Path) -> None:
        """兼容旧接口的单根配置。"""
        self.configure_workspace_roots([root])

    def create_session(self, session_id: str | None = None, *, workspace: str | None = None) -> str:
        """创建一个空的持久化会话并返回其标识。"""
        session_id = session_id or uuid.uuid4().hex
        if not session_id.replace('-', '').replace('_', '').isalnum():
            raise ValueError('Invalid session ID')
        with self._session_lock(session_id):
            path = self._sessions_dir / f'session_{session_id}.jsonl'
            if path.exists():
                raise SessionAlreadyExistsError(f'Session {session_id} already exists')
            directory = resolve_workspace(
                workspace if workspace is not None else self._default_workspace,
                base=self._default_workspace,
                allowed_roots=self._workspace_roots,
            )
            store = self._session_store_cls(self._sessions_dir, session_id=session_id)
            store.append('session_created')
            store.record_workspace(directory)
            workspaces.remember_workspace(directory, file=self.workspace_file)
            return store.session_id

    def _workspace_for(self, store) -> Path:
        saved = store.session_summary()['workspace']
        return resolve_workspace(
            saved or self._default_workspace, base=self._default_workspace, allowed_roots=self._workspace_roots
        )

    def get_workspace(self, session_id: str) -> str:
        return str(self._workspace_for(self._open_session(session_id)))

    def set_workspace(self, session_id: str, path: str) -> str:
        lock = self._session_lock(session_id)
        if not lock.acquire(blocking=False):
            raise RuntimeError('会话正在执行任务，请稍后切换工作区')
        try:
            store = self._open_session(session_id)
            base = Path(store.session_summary()['workspace'] or self._default_workspace)
            workspace = resolve_workspace(path, base=base, allowed_roots=self._workspace_roots)
            store.record_workspace(workspace)
            return str(workspace)
        finally:
            lock.release()

    def list_workspaces(self, current: str | None = None) -> dict:
        """为选择器/API 汇总当前、已登记与最近使用的工作区。"""
        current_path = Path(current).expanduser() if current else self._default_workspace
        saved = workspaces.list_workspaces(file=self.workspace_file)
        # 选择器只展示最近几条；/workspaces 需要完整登记表，所以一次算全、
        # 再按选择器上限截取，避免两套排序规则漂移。
        all_choices = workspace_choices(
            self.list_sessions(limit=1000), current_path, saved=saved, allowed_roots=self._workspace_roots, limit=None
        )
        picker_limit = RECENT_LIMIT + (1 if all_choices and all_choices[0]['current'] else 0)
        return {
            'current': str(current_path),
            'roots': [str(root) for root in self._workspace_roots or ()],
            'workspaces': all_choices[:picker_limit],
            'saved': [row for row in all_choices if row.get('saved')],
        }

    def add_workspace(self, path: str, name: str | None = None) -> dict:
        directory = resolve_workspace(path, base=self._default_workspace, allowed_roots=self._workspace_roots)
        return workspaces.add_workspace(directory, name=name, file=self.workspace_file)

    def remove_workspace(self, workspace_id: str) -> dict:
        return workspaces.remove_workspace(workspace_id, file=self.workspace_file)

    def send(
        self,
        session_id: str,
        content: str,
        on_delta=None,
        *,
        attachments: list[dict] | None = None,
        on_event=None,
        housekeeping: bool = True,
    ) -> AgentResult:
        """追加一条用户消息，执行一个回合，返回 assistant 内容。

        ``attachments`` 是 ``harness_image`` 引用（调用方已缓存），作为紧跟提示词
        的独立用户消息存放，与 view_image 结果的回放方式一致。``on_event`` 接收
        ``(event_type, payload)``，供 JSONL 等一次性传输使用。
        """
        if not isinstance(content, str) or not content.strip():
            raise ValueError('message content must be a non-empty string')
        with self._session_lock(session_id):
            store = self._open_session(session_id)
            workspace = self._workspace_for(store)
            store.record_workspace(workspace)
            messages = store.rebuild_openai_history(recover_pending=True)
            message = {'role': 'user', 'content': content}
            messages.append(message)
            store.append_message(message)
            if attachments:
                attachment = image_attachment_message(attachments)
                messages.append(attachment)
                store.append_message(attachment)
            turn_kwargs = {}
            if on_delta is not None:
                turn_kwargs['on_delta'] = on_delta
            if on_event is not None:
                turn_kwargs['on_event'] = on_event
            if not housekeeping:
                turn_kwargs['housekeeping'] = False
            with use_workspace(workspace):
                result = self._run_turn(messages, session_store=store, **turn_kwargs)
            return AgentResult(session_id=store.session_id, content=result)

    def history(self, session_id: str) -> list[dict]:
        """返回持久化会话可回放的 OpenAI 历史。"""
        return self._open_session(session_id).rebuild_openai_history(repair_gaps=True)

    def open_session(self, session_id: str):
        """打开既有会话的事件存储，用于实时 tail。"""
        return self._open_session(session_id)

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """按最后一条用户/assistant 消息时间排序并概括会话。"""
        if not self._sessions_dir.is_dir():
            return []
        paths = self._sessions_dir.glob('session_*.jsonl')
        rows = []
        for path in paths:
            try:
                hit = _summary_cached(path, path.stat())
            except OSError:
                continue  # 日志不可读：列表里直接跳过，不让一个坏文件拖垮整次列表
            if hit is not None:
                rows.append(hit)  # 缓存命中：连 store 都不必构造
                continue
            try:
                store = self._session_store_cls.open(path)
                rows.append(_cached_summary(store))
            except (OSError, ValueError):
                continue  # 日志不可读或损坏：同样跳过
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
        store.append_session_title(title[:80], source='user')  # 用户命名：异步重生成永不覆盖
        return store.session_summary()['title']

    def manage_session(self, action: str, row: dict):
        if action not in ('archive', 'restore', 'delete'):
            raise ValueError('未知会话操作')
        session_id = row.get('id')
        if (
            not isinstance(session_id, str)
            or not session_id
            or not session_id.replace('-', '').replace('_', '').isalnum()
        ):
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
            path = self._sessions_dir / f'session_{session_id}.jsonl'
            if not path.is_file():
                raise FileNotFoundError(f'Session {session_id} not found')
        return self._session_store_cls.open(path)

    def _session_lock(self, session_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.RLock())
