"""新 TUI（P2/P3）：行模型 + 虚拟视口 + 浮层补全 + 队列条。

入口：交互模式默认由 `HARNESS_UI=textual` 启动（`textual-next` 为兼容别名）；
非 TTY 或 Textual 不可用时自动回退 ANSI。
"""
from __future__ import annotations

import contextlib
import io
import os
import queue
import threading
import time
from functools import partial

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static, TextArea

from file_mentions import workspace_files
from ui.command_actions import CommandActionsMixin
from ui.composer.completions import apply_suggestion, suggestions
from ui.modals.approval import PermissionModal
from ui.modals.confirm import ConfirmModal
from ui.modals.queue import QueueModal
from ui.renderers.tools import TOOL_MORE_LINES, TOOL_PREVIEW_LINES, tool_view
from ui.rendering import _render_lines, _render_markdown, _short_count, _streaming_markdown_source
from ui.session_actions import SessionActionsMixin
from ui.session_admin_actions import SessionAdminActionsMixin
from ui.transcript import ConversationModel, TranscriptLayout
from ui.widgets import SUGGESTION_LIMIT, CompletionPopup, Composer, PendingQueue, Transcript
from ui_events import UIEvent, bus

FOLLOW_END = True
# 首次只把最近一段历史转成 UI 块；完整消息仍保留在 _messages 供 Agent 使用。
# 滚动到顶部或 /more 时按页向前补，和 Codex 的分页 history、Pi 的 active
# branch 投影一样，避免启动时把整份长历史渲染进内存。
def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


HISTORY_INITIAL_MESSAGES = _env_int('HARNESS_HISTORY_INITIAL_MESSAGES', 200)
HISTORY_PAGE_MESSAGES = _env_int('HARNESS_HISTORY_PAGE_MESSAGES', 200)
# 流式 Markdown 每次都要重新解析累计全文。根据上一帧耗时动态选择下一帧
# 间隔：短文本更快出字，长文本自动退让，避免固定 120ms 让小回复也显得迟滞。
STREAM_MIN_INTERVAL = 0.05
STREAM_MAX_INTERVAL = 0.30
STREAM_DEFAULT_INTERVAL = 0.12
SPINNERS = ('⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')

class HarnessApp(SessionActionsMixin, SessionAdminActionsMixin,
                 CommandActionsMixin, App):
    """行模型驱动的 TUI：聊天区只有一个自绘视口。"""

    # 子类 BINDINGS 会覆盖 App 默认绑定（默认里本来有 ctrl+c），必须显式写回。
    BINDINGS = [
        Binding('ctrl+c', 'interrupt', 'Copy selection / quit', show=False,
                priority=True),
        Binding('ctrl+q', 'quit_app', 'Quit', show=False, priority=True),
        Binding('ctrl+shift+c', 'copy_selection', 'Copy', show=False,
                priority=True),
    ]

    CSS = """
    Screen { background: $background; }
    ModalScreen { background: $background 60%; align: center middle; }
    #pending { height: 1; padding: 0 2; color: #d7af5f;
               background: $surface; display: none; }
    #footer { height: 1; padding: 0 2; color: $text-muted;
              background: $surface; }
    #dock { dock: bottom; height: auto; background: $surface; }
    #composer { height: auto; max-height: 8; padding: 0 1;
                border: round $accent; background: $surface; }
    #completions { position: absolute; overlay: screen; layer: overlay;
                   display: none; background: $surface; border: round $accent;
                   padding: 0 1; }
    #perm-box { width: 76; max-height: 80%; padding: 2 3;
                border: round $warning; background: $boost; }
    #perm-box.risk-danger { border: round $error; }
    #session-box { width: 90; max-height: 28; padding: 1 2;
                   border: round $accent; background: $boost; }
    #session-list { height: auto; max-height: 18; }
    #session-search { margin: 0 0 1 0; }
    #fork-box { width: 76; max-height: 24; padding: 1 2;
                border: round $accent; background: $boost; }
    #fork-list { height: auto; max-height: 16; }
    #model-box { width: 60; max-height: 20; padding: 1 2;
                 border: round $accent; background: $boost; }
    #model-list { height: auto; max-height: 14; }
    .confirmation-body { height: auto; max-height: 10; }
    #confirm-box { width: 78; max-height: 28; padding: 1 2;
                   border: round $warning; background: $boost; }
    #choice-box { width: 90; max-height: 28; padding: 1 2;
                  border: round $accent; background: $boost; }
    #choice-search { margin: 0 0 1 0; }
    #choice-list { height: auto; max-height: 18; }
    #cleanup-box { width: 90; max-height: 28; padding: 1 2;
                   border: round $accent; background: $boost; }
    #cleanup-search { margin: 0 0 1 0; }
    #cleanup-list { height: auto; max-height: 18; }
    #typed-box { width: 78; max-height: 28; padding: 1 2;
                 border: round $error; background: $boost; }
    #typed-input { margin: 1 0 0 0; }
    #queue-box { width: 90; max-height: 28; padding: 1 2;
                 border: round $accent; background: $boost; }
    #queue-search { margin: 0 0 1 0; }
    #queue-list { height: auto; max-height: 18; }
    """

    def __init__(self, messages: list | None = None,
                 session_id: str | None = None, title: str = '',
                 session_store=None) -> None:
        super().__init__()
        self._source_messages = list(messages or [])
        self._messages = list(messages or [])
        self._history_messages: list[dict] = []
        self._history_start = 0
        self._history_loading = False
        self._history_auto_armed = False
        self._history_last_offset = 0
        self._session_id = session_id or ''
        self._title = title
        self._store = session_store
        self.model = ConversationModel()
        self.doc_layout = TranscriptLayout(self._render_block)
        self.view = Transcript(self.model, self.doc_layout,
                               follow_end=FOLLOW_END)
        self._events: queue.Queue = queue.Queue()
        self._pending = PendingQueue()
        self._steering = PendingQueue()
        self._tool_blocks: dict[str, str] = {}
        self._queue_row_cache: dict[str, dict] = {}
        self._dirty = True
        self._busy = False
        self._operation = 'agent'
        self._cleanup_block_id = None
        self._exiting = False
        self._status: dict = {}
        self._footer = None
        self._composer = None
        self._popup = None
        self._queue_widget = None
        self._suggestions: list[tuple[str, str]] = []
        self._suggestion_index = 0
        self._programmatic_text: str | None = None
        self._mention_files: list[str] = []
        self._mention_root = None
        self._mention_at = 0.0
        self._turn_started = 0.0
        self._last_stream_render = 0.0
        self._stream_interval = STREAM_DEFAULT_INTERVAL
        self._stream_render_cache = None

    # ---------- 布局 ----------

    def compose(self) -> ComposeResult:
        yield self.view
        with Vertical(id='dock'):
            yield Static('', id='pending')
            yield Composer(id='composer')
            yield Static('', id='footer')
        yield CompletionPopup('', id='completions')

    def on_mount(self) -> None:
        bus.subscribe(self._on_event)
        self._composer = self.query_one('#composer', Composer)
        self._footer = self.query_one('#footer', Static)
        self._popup = self.query_one('#completions', CompletionPopup)
        self._queue_widget = self.query_one('#pending', Static)
        self._load_history(self._source_messages)
        self._refresh_footer()
        self._composer.focus()
        self.set_interval(0.03, self._drain_events)
        self.set_interval(0.05, self._flush_layout)
        self.set_interval(0.2, self._tick_border_status)
        self.set_interval(1.0, self._refresh_footer)

    def action_quit_app(self) -> None:
        self.exit()

    def _selected_text(self) -> str | None:
        try:
            return self.screen.get_selected_text()
        except Exception:
            return None

    def _copy_text(self, text: str, *, destination: str = 'to clipboard') -> None:
        # 显示字符数不含布局填充、代码缩进或换行；复制内容保留原有结构。
        count = sum(not char.isspace() for char in text)
        self.copy_to_clipboard(text)
        self._notice(f'Copied {count} non-whitespace chars {destination}.')

    def action_copy_selection(self) -> None:
        text = self._selected_text()
        if text:
            self._copy_text(text)
        else:
            self._notice('Select transcript text first, or use /copy for the last tool output.')

    def action_interrupt(self) -> None:
        # 有选中文本时 Ctrl-C 复制，不再直接退出；无选中保持原来的退出行为。
        text = self._selected_text()
        if text:
            self._copy_text(text)
            return
        self.exit()

    def on_unmount(self) -> None:
        self._stream_render_cache = None
        self._exiting = True
        bus.unsubscribe(self._on_event)
        self._pending.put(None)

    def on_resize(self, event: events.Resize) -> None:
        self._place_popup()

    def _flush_layout(self, force: bool = False) -> None:
        if not self._dirty:
            return
        streaming = any(block.kind == 'assistant'
                        and block.payload.get('streaming')
                        for block in reversed(self.model.snapshot()[-3:]))
        if not force and streaming:
            if time.monotonic() - self._last_stream_render < self._stream_interval:
                return      # 保留 dirty，下个 tick 再合并重排
        started = time.monotonic()
        self._last_stream_render = started
        self._dirty = False
        self.view.refresh_layout()
        if streaming:
            cost = time.monotonic() - started
            self._stream_interval = min(
                STREAM_MAX_INTERVAL,
                max(STREAM_MIN_INTERVAL, cost * 2))
        # 首次贴底布局完成后才允许自动上翻，避免启动瞬间 offset 仍为 0
        # 时把历史页连续加载进来。
        if (not self._history_auto_armed
                and self.view.content_line_count > self.view.size.height):
            self._history_auto_armed = True
            self._history_last_offset = int(self.view.scroll_offset.y)

    def _refresh_footer(self) -> None:
        if self._footer is None:
            return
        parts = []
        # 目录/分支常驻，不会被会话标题挤掉（标题只在有实际内容时显示）。
        try:
            import harness
            workspace = harness.get_workdir()
            branch = harness.current_branch(workspace)
            label = f'{workspace.name} ({branch})' if branch else workspace.name
            parts.append(str(label))
        except Exception:
            pass
        if self._title and self._title not in ('新会话', '新对话', 'New session', 'Untitled'):
            parts.append(self._title[:28])
        try:
            import harness
            parts.append(harness.os.getenv('LLM_MODEL_ID', '') or '(model unset)')
        except Exception:
            pass
        ctx_pct = self._status.get('ctx_pct')
        ctx_tokens = self._status.get('ctx_tokens')
        ctx_window = self._status.get('ctx_window')
        if ctx_pct is not None:
            detail = f'ctx {float(ctx_pct):.0f}%'
            if ctx_tokens and ctx_window:
                detail += f' {_short_count(ctx_tokens)}/{_short_count(ctx_window)}'
            parts.append(detail)
        input_tokens = self._status.get('input_tokens')
        output_tokens = self._status.get('output_tokens')
        prompt_tokens = self._status.get('prompt_tokens')
        cached_tokens = self._status.get('cached_tokens')
        cache_write = self._status.get('cache_write_tokens') or 0
        if input_tokens is None and prompt_tokens is not None:
            input_tokens = max(0, prompt_tokens - (cached_tokens or 0)
                               - cache_write)
        if input_tokens:
            parts.append(f'↑{_short_count(input_tokens)}')
        if output_tokens:
            parts.append(f'↓{_short_count(output_tokens)}')
        if cached_tokens:
            parts.append(f'R{_short_count(cached_tokens)}')
        if cache_write:
            parts.append(f'W{_short_count(cache_write)}')
        rate = self._status.get('latest_cache_hit_rate')
        if rate is None and prompt_tokens:
            rate = min(1.0, (cached_tokens or 0) / prompt_tokens)
        if rate is not None and (cached_tokens or self._status.get('cache_seen')):
            parts.append(f'cache {100 * float(rate):.0f}%')
        total = self._status.get('tokens')
        if total and not (input_tokens or output_tokens):
            parts.append(f'tok {_short_count(total)}')
        if self._status.get('cost') and self._status.get('cost_source') != 'unavailable':
            symbol = '¥' if self._status.get('currency', 'CNY') == 'CNY' else ''
            parts.append(f"~{symbol}{self._status['cost']}")
        queued = len(self._pending) + len(self._steering)
        if queued:
            parts.append(f'{queued} queued')
        if self._busy:
            parts.append('working')
        self._footer.update('  ·  '.join(part for part in parts if part))

    # ---------- 补全 ----------

    def _workspace_files(self) -> list[str]:
        import harness
        root = harness.get_workdir()
        now = time.monotonic()
        if root != self._mention_root or now - self._mention_at > 5.0:
            self._mention_files = workspace_files(root)
            self._mention_root = root
            self._mention_at = now
        return self._mention_files

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if getattr(event.text_area, 'id', '') != 'composer':
            return
        if self._programmatic_text is not None:
            if event.text_area.text == self._programmatic_text:
                return    # 程序化写入：保持补全关闭，直到用户再次编辑
            self._programmatic_text = None
        self._update_suggestions(event.text_area.text)

    def _update_suggestions(self, value: str) -> None:
        files = self._workspace_files() if '@' in (value or '') else []
        self._suggestions = suggestions(value, files)
        self._suggestion_index = 0
        if self._suggestions:
            self._popup.show(self._suggestions, self._suggestion_index)
            self._place_popup()
        else:
            self._popup.hide_popup()

    def _place_popup(self) -> None:
        if self._popup is None or self._composer is None or not self._suggestions:
            return
        region = self._composer.region
        if region.width <= 0:
            return
        rows = min(len(self._suggestions), SUGGESTION_LIMIT) + 2
        width = min(max(24, region.width), 100)
        x = region.x
        y = max(0, region.y - rows)
        self._popup.styles.offset = (x, y)
        self._popup.styles.width = width
        self._popup.styles.height = rows

    def has_suggestions(self) -> bool:
        return bool(self._suggestions)

    def accept_suggestion(self) -> None:
        if not self._suggestions or self._composer is None:
            return
        value, _label = self._suggestions[self._suggestion_index]
        text = self._composer.text
        new_text = apply_suggestion(text, value)
        self._programmatic_text = new_text
        self._composer.text = new_text
        self.close_suggestions()
        self._composer.cursor_location = (0, len(new_text))
        self._composer.focus()

    def choose_suggestion(self, index: int) -> None:
        if 0 <= index < len(self._suggestions):
            self._suggestion_index = index
            self.accept_suggestion()

    def cycle_suggestion(self, delta: int) -> None:
        if not self._suggestions:
            return
        self._suggestion_index = ((self._suggestion_index + delta)
                                  % len(self._suggestions))
        self._popup.show(self._suggestions, self._suggestion_index)
        self._place_popup()

    def close_suggestions(self) -> None:
        self._suggestions = []
        if self._popup is not None:
            self._popup.hide_popup()

    # ---------- 队列 ----------

    def take_last_queued(self) -> None:
        item = self._pending.take_last()
        if item is None or self._composer is None:
            return
        self._programmatic_text = item
        self._composer.text = item
        self._composer.cursor_location = (0, len(item))
        self._refresh_queue()

    def dequeue_all(self) -> None:
        """Esc / 选择器 Ctrl+R：把 steering 和 follow-up 全部取回输入框。"""
        if self._composer is None:
            return
        items = self._steering.drain() + self._pending.drain()
        items = [item for item in items if item]
        if not items:
            self._notice('No queued messages to restore.')
            return
        self._restore_to_composer(items)
        self._notice(f'Restored {len(items)} queued message(s) to editor.')

    def _restore_to_composer(self, items: list[str]) -> None:
        """保留当前草稿，把消息按顺序接在后面，并同步队列状态。"""
        if self._composer is None:
            return
        current = self._composer.text.strip()
        text = chr(10).join(items)
        if current:
            text = current + chr(10) + text
        self._programmatic_text = text
        self._composer.text = text
        self._composer.cursor_location = (0, len(text))
        self._composer.focus()
        self._refresh_queue()

    def _queued_rows(self) -> list[dict]:
        """合并两个队列：值带队列名和位置，另存预期文本防消费后误取。"""
        rows = []
        for name, pending, prefix in (
                ('steer', self._steering, 'steer'),
                ('next', self._pending, 'next')):
            for index, text in enumerate(pending.snapshot()):
                preview = text.replace(chr(10), ' ')[:60]
                rows.append({
                    'value': f'{name}:{index}', 'queue': name,
                    'index': index, 'text': text,
                    'label': f'{prefix}  {preview}',
                    'search': text,
                })
        return rows

    def open_queue_picker(self) -> None:
        """Ctrl+G / Alt+↑ / `/dequeue`：单条直接取回，多条用选择器管理。"""
        rows = self._queued_rows()
        if not rows:
            self._notice('No queued messages to restore.')
            return
        if len(rows) == 1:
            self._restore_queue_row(rows[0])
            return
        self._queue_row_cache = {row['value']: row for row in rows}
        self.push_screen(QueueModal(rows), self._on_queue_action)

    def _queue_for(self, row: dict):
        return self._steering if row['queue'] == 'steer' else self._pending

    def _restore_queue_row(self, row: dict) -> None:
        text = self._queue_for(row).take_at(row['index'], expected=row['text'])
        if text is None:
            self._notice('队列已变化（消息可能已被消费），请重新打开。')
            return
        self._restore_to_composer([text])
        self._notice('Restored 1 queued message to editor.')

    def _on_queue_action(self, result) -> None:
        if not result:
            return
        action, value = result
        if action == 'all':
            self.dequeue_all()
            return
        row = self._queue_row_cache.get(value)
        if row is None:
            self._notice('队列已变化，请重新打开。')
            return
        if action == 'restore':
            self._restore_queue_row(row)
            return
        if action == 'delete':
            body = ('将删除这条排队消息，不会发送：' + chr(10) * 2
                    + row['text'])
            self.push_screen(
                ConfirmModal('Delete queued message?', body, 'Delete'),
                partial(self._delete_queue_row, row=row))

    def _delete_queue_row(self, confirmed, row: dict) -> None:
        if not confirmed:
            return
        text = self._queue_for(row).take_at(row['index'], expected=row['text'])
        if text is None:
            self._notice('队列已变化（消息可能已被消费），请重新打开。')
            return
        self._refresh_queue()
        preview = text.replace(chr(10), ' ')[:40]
        self._notice(f'Deleted queued message: {preview}')

    def _refresh_queue(self) -> None:
        if self._queue_widget is None:
            return
        steering = self._steering.snapshot()
        followup = self._pending.snapshot()
        items = steering + followup
        if not items:
            self._queue_widget.display = False
            self._queue_widget.update('')
            return
        preview = ' | '.join(
            ('steer:' if index < len(steering) else 'next:')
            + item.replace(chr(10), ' ')[:20]
            for index, item in enumerate(items[:3]))
        more = f' … +{len(items) - 3}' if len(items) > 3 else ''
        self._queue_widget.update(f'⏳ {len(items)} queued: {preview}{more} '
                                  '· Ctrl+G 管理')
        self._queue_widget.display = True

    # ---------- 状态 ----------

    def _tick_border_status(self) -> None:
        if self._composer is None:
            return
        offset = int(self.view.scroll_offset.y) if self.view.is_attached else 0
        if (not self._busy and self._history_auto_armed
                and self._history_start > 0 and offset <= 1
                and self._history_last_offset > 1):
            self.load_older_history()
        self._history_last_offset = offset
        if not self._busy:
            if self._composer.border_title:
                self._composer.border_title = ''
            return
        elapsed = max(0.0, time.monotonic() - self._turn_started)
        spinner = SPINNERS[int(time.monotonic() * 8) % len(SPINNERS)]
        queued = len(self._pending) + len(self._steering)
        title = f'{spinner} Working {elapsed:4.1f}s'
        if queued:
            title += f' · {queued} queued'
        self._composer.border_title = title

    # ---------- 事件 ----------

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

    # ---------- 渲染 ----------

    def _render_block(self, block, width: int) -> list[Text]:
        kind = block.kind
        payload = block.payload
        if kind == 'user':
            return _render_lines(Text(f"❯ {payload.get('text', '')}",
                                      style='bold #e0e0e0'), width)
        if kind == 'assistant':
            text = str(payload.get('text', ''))
            if not text:
                return []
            streaming = bool(payload.get('streaming'))
            render_text, provisional_styles = (
                _streaming_markdown_source(text) if streaming else (text, False))
            if not streaming and not provisional_styles:
                cached = self._stream_render_cache
                if cached is not None:
                    block_id, cached_width, source, lines = cached
                    if block_id == block.id and cached_width == width and source == text:
                        self._stream_render_cache = None
                        return lines
            # Provisional code styles must never leak into the finalized frame.
            self._stream_render_cache = None
            lines = _render_markdown(render_text, width)
            if (streaming and not provisional_styles
                    and len(text) <= 100_000 and len(lines) <= 10_000):
                self._stream_render_cache = (block.id, width, text, lines)
            return lines
        if kind == 'tool':
            return _render_lines(tool_view(payload), width)
        if kind == 'diff':
            title = Text(f"diff {payload.get('path', '')}",
                         style='bold #d78757')
            body = Text(str(payload.get('text', '')))
            return _render_lines(Text('\n').join([title, body]), width)
        if kind == 'notice':
            style = '#ff5f5f' if payload.get('level') == 'error' else '#a7a7a7'
            return _render_lines(Text(str(payload.get('text', '')), style=style),
                                 width)
        return []

    # ---------- 交互 ----------

    def toggle_tool(self, block_id: str) -> None:
        block = self.model.get(block_id)
        if block is None or block.kind != 'tool':
            return
        self.model.set_expanded(block_id, not block.expanded)
        self._mark_dirty()

    def copy_last_tool(self) -> None:
        """`/copy`：复制最近一次工具输出的完整内容，便于粘贴。"""
        for block in reversed(self.model.snapshot()):
            if block.kind == 'tool':
                output = str(block.payload.get('output') or '')
                if output:
                    self._copy_text(output, destination='from the last tool output')
                else:
                    self._notice('The last tool block has no output yet.')
                return
        self._notice('No tool output to copy.', level='error')

    def toggle_last_tool(self) -> None:
        for block in reversed(self.model.snapshot()):
            if block.kind == 'tool':
                self.toggle_tool(block.id)
                return

    def on_transcript_click(self, line_index: int) -> None:
        if self._selected_text():
            return          # 拖动选择时不要顺带展开工具
        block_id = self.doc_layout.block_at(line_index)
        if block_id:
            block = self.model.get(block_id)
            if block is not None and block.kind == 'tool':
                self.toggle_tool_preview(block_id)
            elif block is not None:
                self.toggle_tool(block_id)

    def toggle_tool_preview(self, block_id: str) -> None:
        """点击工具块：10 行 ↔ 40 行；Ctrl+O 仍可展开全文。"""
        block = self.model.get(block_id)
        if block is None or block.kind != 'tool':
            return
        if block.expanded:
            # Ctrl+O 展开过全文，再点一次回到默认折叠。
            self.model.set_expanded(block_id, False)
            new_limit = TOOL_PREVIEW_LINES
        else:
            limit = int(block.payload.get('preview_lines') or TOOL_PREVIEW_LINES)
            new_limit = (TOOL_PREVIEW_LINES if limit > TOOL_PREVIEW_LINES
                         else TOOL_MORE_LINES)
        self.model.update(block_id, preview_lines=new_limit)
        self._mark_dirty()
        self._flush_layout(force=True)

    def _notice(self, text: str, level: str = 'info') -> None:
        self.model.append('notice', {'text': text, 'level': level})
        self._mark_dirty()

    def start_review(self) -> None:
        """`/review`：后台只读审阅，进度/结果写进同一块（不占对话回合）。"""
        if self._busy:
            self._notice('Cannot review while a request is running.')
            return
        block = self.model.append('notice', {'text': 'review: running…'})
        self._mark_dirty()
        threading.Thread(target=self._run_review,
                         args=(block.id,), daemon=True).start()

    def _run_review(self, block_id: str) -> None:
        from review import run_review

        def progress(text: str) -> None:
            try:
                self.call_from_thread(self._review_progress, block_id, text)
            except Exception:
                pass

        try:
            result = run_review(progress=progress)
        except Exception as exc:
            result = f'Review failed: {type(exc).__name__}: {exc}'
        try:
            self.call_from_thread(self._review_done, block_id, result)
        except Exception:
            pass

    def _review_progress(self, block_id: str, text: str) -> None:
        if self.model.get(block_id) is not None:
            self.model.update(block_id, text=str(text))
            self._mark_dirty()

    def _review_done(self, block_id: str, text: str) -> None:
        if self.model.get(block_id) is not None:
            self.model.update(block_id, text=str(text))
            self._mark_dirty()

    # ---------- 后台任务 ----------

    def start_watch(self, argument: str) -> None:
        """`/watch`：后台线程轮询，进度与最终输出写进同一块。"""
        block = self.model.append('notice', {'text': 'watch: 等待后台任务…'})
        self._mark_dirty()
        threading.Thread(target=self._run_watch,
                         args=(block.id, argument), daemon=True).start()

    def _run_watch(self, block_id: str, argument: str) -> None:
        from background_watch import watch_command
        lines = []

        def progress(text: str) -> None:
            lines.append(str(text))
            try:
                self.call_from_thread(self._watch_progress, block_id, list(lines))
            except Exception:
                pass

        try:
            result = watch_command(argument, emit=progress)
        except Exception as exc:
            result = f'Watch failed: {type(exc).__name__}: {exc}'
        try:
            self.call_from_thread(self._watch_done, block_id, result)
        except Exception:
            pass

    def _watch_progress(self, block_id: str, lines: list) -> None:
        if self.model.get(block_id) is not None:
            self.model.update(block_id,
                              text='watch:' + chr(10) + chr(10).join(lines[-8:]))
            self._mark_dirty()

    def _watch_done(self, block_id: str, result: str) -> None:
        if self.model.get(block_id) is not None:
            self.model.update(block_id, text=str(result))
            self._mark_dirty()

    def cancel_turn(self) -> None:
        """Esc：取消当前回合，并把 steering/follow-up 取回编辑器（同 Pi）。"""
        if not self._busy:
            return
        import turn_control
        turn_control.request_cancel()
        self.dequeue_all()
        if self._composer is not None:
            self._composer.border_title = 'cancelling…'

    def submit_shell(self, command: str) -> None:
        """`!cmd`：直接执行 shell，输出按工具块展示，不进入模型上下文。"""
        if self._busy:
            self._notice('当前任务尚未结束，请结束后再执行 shell 命令。')
            return
        self._busy = True
        self._operation = 'shell'
        self._turn_started = time.monotonic()
        block = self.model.append('tool', {
            'name': 'bash', 'args': {'command': command},
            'state': 'running', 'output': '', 'expanded': False})
        self._mark_dirty()
        threading.Thread(target=self._run_shell,
                         args=(block.id, command), daemon=True).start()

    def _run_shell(self, block_id: str, command: str) -> None:
        import harness
        import turn_control
        started = time.monotonic()
        output = ''
        try:
            harness.permission_gate.use_ui = True
            turn_control.begin_turn()
            output = harness.run_bash(command)
        except Exception as exc:
            output = f'Error: {exc}'
        finally:
            turn_control.end_turn()
            harness.permission_gate.use_ui = False
        duration = int((time.monotonic() - started) * 1000)
        try:
            self.call_from_thread(self._finish_shell, block_id, output, duration)
        except Exception:
            pass

    def _finish_shell(self, block_id: str, output: str, duration_ms: int) -> None:
        failed = str(output).lstrip().startswith(('Error', 'Permission denied'))
        if self.model.get(block_id) is not None:
            self.model.update(block_id,
                              state='failed' if failed else 'complete',
                              output=str(output), duration_ms=duration_ms)
        self._mark_dirty()
        self._finish_local_operation()

    def submit_composer(self, mode: str = 'followup') -> None:
        """Enter 的统一入口：候选优先采用，命令候选随后执行。

        忙时 Enter 传 followup（本回合结束后处理），Alt+Enter 传 steer
        （Agent 下一次消费时注入当前回合）；空闲时两者都直接发送。
        """
        if self._composer is None:
            return
        if self.has_suggestions():
            original = self._composer.text
            self.accept_suggestion()
            text = self._composer.text
            # 目录候选只回填，让用户继续补子目录或 --name，再显式提交。
            name, _, argument = original.lstrip('/').partition(' ')
            if name in ('cd', 'workspace') and (
                    name == 'cd' or argument.lstrip().startswith('add ')):
                return
            if text.lstrip().startswith('/'):
                self._programmatic_text = ''
                self._composer.text = ''
                self.submit_text(text, mode=mode)
            return
        text = self._composer.text
        self._programmatic_text = ''
        self._composer.text = ''
        self.submit_text(text, mode=mode)

    def submit_text(self, text: str, mode: str = 'followup') -> None:
        text = (text or '').strip()
        if not text or self._exiting:
            return
        self.close_suggestions()
        self._programmatic_text = ''    # Composer 提交后会清空文本
        if text.startswith('!') and text[1:].strip():
            self.submit_shell(text[1:].strip())
            return
        if self._handle_command(text):
            return
        self._send_user(text, mode=mode)

    def _send_user(self, text: str, mode: str = 'followup') -> None:
        if self._busy and self._operation != 'agent':
            mode = 'followup'
        if self._busy and mode != 'followup':
            # Alt+Enter 的 steer：先只进队列；agent_loop 注入时再由 user_message 落块。
            self._steering.put(text)
            self._refresh_queue()
            return
        self.model.append('user', {'text': text})
        self._mark_dirty()
        self._flush_layout(force=True)      # 先画出用户消息，不必等 50ms 定时器
        if self._busy:
            # Enter：进入下回合队列，不打断正在进行的回答。
            self._pending.put(text)
            self._refresh_queue()
            return
        self._pending.put(text)
        self._refresh_queue()
        self._start_worker()

    def submit_skill(self, name: str, argument: str = '') -> None:
        """`/skill:<name> args`：把技能正文展开成一条 user 消息（同 Pi）。"""
        import skill_system
        body = skill_system.load_skill(name)
        if body.startswith('Error:'):
            self._notice(f'Unknown skill: {name}', level='error')
            return
        content = f'<skill name="{name}">' + chr(10) + body + chr(10) + '</skill>'
        if argument.strip():
            content += chr(10) + chr(10) + argument.strip()
        self._send_user(content)

    def _start_worker(self) -> None:
        self._operation = 'agent'
        self._busy = True
        self._turn_started = time.monotonic()
        self._refresh_footer()
        self._tick_border_status()   # 立即显示 Working，不等 0.2s 定时器
        threading.Thread(target=self._run_agent, daemon=True).start()

    def _run_agent(self) -> None:
        import harness
        import turn_control
        try:
            while not self._exiting:
                text = self._pending.get()
                if text is None:
                    break
                self._messages.append({'role': 'user', 'content': text})
                self.call_from_thread(self._refresh_queue)
                answer = None
                turn_control.begin_turn()
                try:
                    with harness.agent_lock:
                        harness.permission_gate.use_ui = True
                        # agent_loop 仍会 print tokens/结果；Textual 已接管屏幕，
                        # 这些写入会盖住 UI 并拖慢重绘，全部收进内存。
                        with contextlib.redirect_stdout(io.StringIO()):
                            kwargs = {
                                'steer_provider': self._steering.drain,
                            }
                            if self._store is not None:
                                kwargs['session_store'] = self._store
                            answer = harness.agent_loop(self._messages, **kwargs)
                except KeyboardInterrupt:
                    pass
                except Exception as exc:
                    answer = f'Error: {type(exc).__name__}: {exc}'
                finally:
                    turn_control.end_turn()
                    harness.permission_gate.use_ui = False
                try:
                    self.call_from_thread(self._sync_final_answer, answer)
                except Exception:
                    pass
                self.call_from_thread(self._refresh_queue)
                self.call_from_thread(self._refresh_footer)
                if len(self._pending) == 0:
                    self._busy = False
                    break
        finally:
            self._busy = False

    def _sync_final_answer(self, answer) -> None:
        """兜底：事件流丢失 assistant_end/text 时，用 agent_loop 返回值补最后一答。"""
        self._drain_events()   # 先吃掉 queued 的 delta/end，避免把同一段文本追加两遍
        text = str(answer or '').strip()
        if not text or text == 'No output':
            for message in reversed(self._messages):
                if message.get('role') != 'assistant':
                    continue
                content = message.get('content')
                if isinstance(content, str) and content.strip():
                    text = content.strip()
                    break
        if not text:
            return
        assistants = [block for block in self.model.snapshot()
                      if block.kind == 'assistant']
        last = assistants[-1] if assistants else None
        if last is not None:
            current = str(last.payload.get('text', '')).strip()
            if not last.payload.get('streaming'):
                # 已有完整 assistant 块：事件流已渲染，不再追加，避免重复。
                if current:
                    return
            elif text.startswith(current):
                self.model.update(last.id, text=text, streaming=False)
                self._mark_dirty()
                self._flush_layout(force=True)
                return
        self.model.append('assistant', {'text': text, 'streaming': False})
        self._mark_dirty()
        self._flush_layout(force=True)


def run(messages: list | None = None, session_id: str | None = None,
        title: str = '', session_store=None) -> None:
    # Textual 自己渲染事件；ANSI 默认渲染器若继续订阅会把同样的工具/文本
    # 打印到屏幕，盖住 TUI。退出时恢复，保证随后回退 ANSI 仍能正常输出。
    from renderer import default_renderer

    bus.unsubscribe(default_renderer.on_event)
    try:
        HarnessApp(messages or [], session_id=session_id, title=title,
                   session_store=session_store).run()
    finally:
        bus.subscribe(default_renderer.on_event)
