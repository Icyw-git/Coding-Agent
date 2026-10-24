"""事件归约：把 UIEvent 队列折叠成会话块模型与状态字典。

`HarnessApp` 只负责把线程安全的事件塞进队列，真正的 `event.type` 分支、
历史分页与状态累积都在这里；这样 App 文件只剩布局、视图交互与回合控制。
"""
from __future__ import annotations

import os
import queue
import time

from ui.modals.approval import PermissionModal
from ui_events import UIEvent


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


# 首次只把最近一段历史转成 UI 块；完整消息仍保留在 _messages 供 Agent 使用。
# 滚动到顶部或 /more 时按页向前补，和 Codex 的分页 history、Pi 的 active
# branch 投影一样，避免启动时把整份长历史渲染进内存。
HISTORY_INITIAL_MESSAGES = _env_int('HARNESS_HISTORY_INITIAL_MESSAGES', 200)
HISTORY_PAGE_MESSAGES = _env_int('HARNESS_HISTORY_PAGE_MESSAGES', 200)


class EventReductionMixin:
    """把 UI 事件队列应用到 `ConversationModel`/`_status`，不碰布局与控件。"""

    def _on_event(self, event: UIEvent) -> None:
        session_id = event.payload.get('session_id')
        if session_id and self._session_id and session_id != self._session_id:
            return
        self._events.put(event)

    def _drain_events(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                self._apply(event)
            except Exception:
                pass
        if self._dirty:
            self._refresh_footer()

    def _apply(self, event: UIEvent) -> None:
        kind = event.type
        payload = event.payload
        if kind == 'history_messages':
            self._load_history(payload.get('messages', []))
        elif kind == 'user_message':
            self.model.append('user', {'text': str(payload.get('text', ''))})
            self._mark_dirty()
        elif kind == 'assistant_start':
            mid = str(payload.get('mid', f'a{time.time_ns()}'))
            self.model.append('assistant', {'text': '', 'streaming': True},
                              block_id=mid)
            self._mark_dirty()
        elif kind == 'assistant_delta':
            mid = str(payload.get('mid', ''))
            if self.model.get(mid) is None:
                self.model.append('assistant', {'text': '', 'streaming': True},
                                  block_id=mid)
            self.model.append_text(mid, str(payload.get('delta', '')))
            self._mark_dirty()
        elif kind == 'assistant_end':
            mid = str(payload.get('mid', ''))
            if self.model.get(mid) is not None:
                self.model.update(mid, streaming=False)
            self._mark_dirty()
        elif kind == 'assistant_text':
            self.model.append('assistant',
                              {'text': str(payload.get('text', ''))})
            self._mark_dirty()
        elif kind == 'tool_start':
            call_id = str(payload.get('call_id') or f't{time.time_ns()}')
            block_id = f'tool-{call_id}'
            self._tool_blocks[call_id] = block_id
            self.model.append('tool', {
                'name': payload.get('name', 'tool'),
                'args': payload.get('args') or {},
                'state': 'running', 'output': '',
                'expanded': False,
            }, block_id=block_id)
            self._mark_dirty()
        elif kind == 'tool_end':
            call_id = str(payload.get('call_id') or '')
            block_id = self._tool_blocks.pop(call_id, None)
            block = self.model.get(block_id) if block_id else None
            if block is None:
                for candidate in reversed(self.model.snapshot()):
                    if (candidate.kind == 'tool'
                            and candidate.payload.get('state') == 'running'):
                        block = candidate
                        break
            if block is not None:
                state = (payload.get('outcome')
                         or ('complete' if payload.get('ok') else 'failed'))
                self.model.update(
                    block.id, state=state,
                    output=str(payload.get('output_preview', '')),
                    diff=str(payload.get('diff') or ''),
                    duration_ms=payload.get('duration_ms', 0))
            self._mark_dirty()
        elif kind == 'diff':
            text = str(payload.get('diff_text') or '')
            if text:
                self.model.append('diff', {'path': payload.get('path', ''),
                                           'text': text})
                self._mark_dirty()
        elif kind == 'error':
            self.model.append('notice', {'text': str(payload.get('msg', '')),
                                         'level': 'error'})
            self._mark_dirty()
        elif kind == 'permission_request':
            self.push_screen(
                PermissionModal(
                    str(payload.get('req_id', '')),
                    str(payload.get('tool', '')),
                    payload.get('args') or {},
                    str(payload.get('title', 'Permission needed')),
                    str(payload.get('risk', 'warning')),
                    allow_session=bool(payload.get('allow_session', True)),
                )
            )
        elif kind == 'round_end':
            prompt_tokens = int(payload.get('prompt_tokens') or 0)
            cached_tokens = int(payload.get('cached_tokens') or 0)
            completion_tokens = int(payload.get('completion_tokens') or 0)
            self._status.update({
                'prompt_tokens': prompt_tokens,
                'cached_tokens': cached_tokens,
                'input_tokens': max(0, prompt_tokens - cached_tokens),
                'output_tokens': completion_tokens,
                'tokens': payload.get('total_tokens'),
                'latest_prompt_tokens': prompt_tokens,
                'latest_cached_tokens': cached_tokens,
                'latest_cache_hit_rate': (
                    cached_tokens / prompt_tokens if prompt_tokens else None),
                'cache_seen': True,
                'cost': payload.get('cost'),
                'cost_source': payload.get('cost_source'),
            })
            window = self._status.get('ctx_window')
            if window and prompt_tokens:
                self._status['ctx_tokens'] = prompt_tokens
                self._status['ctx_pct'] = min(
                    100.0, 100.0 * prompt_tokens / max(1, int(window)))
            self._refresh_footer()
        elif kind == 'status':
            self._status.update(payload)
            self._refresh_footer()
        elif kind == 'activity':
            self._status['phase'] = payload.get('state')
            self._refresh_footer()

    def _append_history_message(self, message: dict, *, prepend: bool = False) -> None:
        role = message.get('role')
        content = message.get('content')
        if role == 'user':
            kind, payload = 'user', {'text': str(content or '')}
        elif role == 'assistant':
            kind, payload = 'assistant', {'text': str(content or '')}
        elif role == 'tool':
            kind, payload = 'tool', {
                'name': 'tool', 'args': {}, 'state': 'complete',
                'output': str(content or ''), 'expanded': False}
        else:
            return
        if prepend:
            self.model.prepend(kind, payload)
        else:
            self.model.append(kind, payload)

    def _load_history(self, messages: list) -> None:
        self._stream_render_cache = None
        self._history_messages = list(messages)
        self._history_start = max(0, len(messages) - HISTORY_INITIAL_MESSAGES)
        self._history_loading = False
        self._history_auto_armed = False
        self._history_last_offset = 0
        self.model.clear()
        for message in messages[self._history_start:]:
            self._append_history_message(message)
        self._mark_dirty()

    def load_older_history(self) -> int:
        """向模型前补一页历史，返回新增的消息数；0 表示已到顶部/无历史。"""
        if self._history_loading or self._history_start <= 0:
            return 0
        self._history_loading = True
        try:
            start = max(0, self._history_start - HISTORY_PAGE_MESSAGES)
            older = self._history_messages[start:self._history_start]
            before = self.view.content_line_count
            offset = int(self.view.scroll_offset.y)
            for message in reversed(older):
                self._append_history_message(message, prepend=True)
            self._history_start = start
            self._mark_dirty()
            self._flush_layout(force=True)
            added = max(0, self.view.content_line_count - before)
            if added:
                self.view.scroll_to(y=offset + added, animate=False)
            self.view.refresh()
            return len(older)
        finally:
            self._history_loading = False

    def _mark_dirty(self) -> None:
        self._dirty = True
