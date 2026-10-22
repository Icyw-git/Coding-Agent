"""新 TUI（P2/P3）：行模型 + 虚拟视口 + 浮层补全 + 队列条。

入口：`HARNESS_UI=textual`（`textual-next` 为兼容别名）。
旧实现保留为 `HARNESS_UI=textual-legacy`，仅用于对照。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import queue
import re
import threading
import time
from functools import partial
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from rich.theme import Theme
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static, TextArea

from file_mentions import workspace_files
from ui import commands
from ui.composer.completions import apply_suggestion, suggestions
from ui.modals.approval import PermissionModal
from ui.modals.choices import ChoiceModal
from ui.modals.cleanup import CleanupPickerModal, TypedConfirmModal
from ui.modals.confirm import ConfirmModal
from ui.modals.forks import ForkPickerModal
from ui.modals.models import ModelPickerModal
from ui.modals.queue import QueueModal
from ui.modals.sessions import SessionPickerModal
from ui.renderers.tools import TOOL_MORE_LINES, TOOL_PREVIEW_LINES, tool_view
from ui.transcript import ConversationModel, TranscriptLayout, VirtualTranscript
from ui_events import UIEvent, bus

_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')


def _strip_ansi(text: str) -> str:
    """harness 的 format_* 报告带 ANSI 色码；Textual 里统一剥掉。"""
    return _ANSI_RE.sub('', str(text))


def _short_count(value) -> str:
    """12345 -> 12.3k，避免状态栏在窄终端被挤掉。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    if number >= 1_000_000:
        return f'{number / 1_000_000:.1f}m'
    if number >= 10_000:
        return f'{number / 1000:.1f}k'
    return str(number)


FOLLOW_END = True
SUGGESTION_LIMIT = 6
SPINNERS = ('⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')

# TUI 的 Rich Markdown 主题：避免默认的 “cyan on black” 代码块在深色终端上
# 显得突兀，也让正文/标题/链接与 Textual dark 主题一致。
MARKDOWN_THEME = Theme({
    'markdown.paragraph': '#d7d7d7',
    'markdown.h1': 'bold #ffffff',
    'markdown.h2': 'bold #ffffff',
    'markdown.h3': 'bold #eeeeee',
    'markdown.h4': 'bold #eeeeee',
    'markdown.h5': 'bold #dddddd',
    'markdown.h6': 'bold #dddddd',
    'markdown.strong': 'bold #ffffff',
    'markdown.em': 'italic #d7d7d7',
    'markdown.code': 'bold #87d7ff',
    'markdown.code_block': '#d7d7d7 on #1c1c1c',
    'markdown.block_quote': 'italic #a7a7a7',
    'markdown.item.bullet': '#d7af5f',
    'markdown.item.number': '#d7af5f',
    'markdown.link': 'underline #5fafff',
    'markdown.link_url': 'dim #5fafff',
    'markdown.table.border': '#5f8787',
    'markdown.hr': '#5f8787',
})



def _render_markdown(text: str, width: int) -> list[Text]:
    """Markdown 渲染兜底：畸形链接/超长表格等不能让整帧崩溃。"""
    try:
        return _render_lines(RichMarkdown(text), width)
    except Exception:
        return _render_lines(Text(text), width)


_CONSOLES: dict[int, Console] = {}


def _console_for(width: int) -> Console:
    console = _CONSOLES.get(width)
    if console is None:
        console = Console(width=width, force_terminal=True,
                          color_system='truecolor', legacy_windows=False,
                          safe_box=True, theme=MARKDOWN_THEME)
        _CONSOLES[width] = console
    return console


def _render_lines(renderable, width: int) -> list[Text]:
    """把 Rich renderable 渲染成固定宽度的 Text 行（虚拟视口用）。"""
    console = _console_for(width)
    options = console.options.update(max_width=width, min_width=width)
    lines = console.render_lines(renderable, options, pad=False)
    result = []
    for segments in lines:
        result.append(Text.assemble(*[
            (segment.text, segment.style) for segment in segments
            if segment.text]))
    return result or [Text('')]


class PendingQueue:
    """待发送消息队列：worker 取头部，用户可取回最后一条编辑。"""

    def __init__(self) -> None:
        self._items: list[str | None] = []
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)

    def put(self, item: str | None) -> None:
        with self._cv:
            self._items.append(item)
            self._cv.notify()

    def get(self) -> str | None:
        with self._cv:
            while not self._items:
                self._cv.wait()
            return self._items.pop(0)

    def take_last(self) -> str | None:
        with self._lock:
            if not self._items:
                return None
            item = self._items[-1]
            if item is None:
                return None
            self._items.pop()
            return item

    def take_at(self, index: int, expected: str | None = None) -> str | None:
        """按位置取走一条；expected 不匹配说明队列已被消费，拒绝误取。"""
        with self._lock:
            if not 0 <= index < len(self._items):
                return None
            item = self._items[index]
            if item is None or (expected is not None and item != expected):
                return None
            return self._items.pop(index)

    def snapshot(self) -> list[str]:
        with self._lock:
            return [item for item in self._items if item is not None]

    def drain(self) -> list[str]:
        """取走全部消息（用于 steer 注入或 Esc 取回）。"""
        with self._lock:
            items = [item for item in self._items if item is not None]
            self._items.clear()
            return items

    def __len__(self) -> int:
        return len(self.snapshot())


class Composer(TextArea):
    """多行输入：Enter 排队/发送、Ctrl-J 换行、Alt+Enter 立即插队、Ctrl+G 管理队列。"""

    BINDINGS = [
        Binding('enter', 'submit', 'Send', show=False, priority=True),
        Binding('ctrl+j', 'newline', 'Newline', show=False, priority=True),
        Binding('tab', 'complete', 'Complete', show=False),
        Binding('alt+up', 'queued_prev', 'Manage queue', show=False),
        Binding('alt+down', 'queued_next', 'Next', show=False),
        Binding('alt+enter', 'submit_now', 'Send now', show=False,
                priority=True),
        Binding('ctrl+g', 'dequeue', 'Manage queue', show=False, priority=True),
        Binding('up', 'suggest_prev', 'Prev', show=False),
        Binding('down', 'suggest_next', 'Next', show=False),
        Binding('ctrl+o', 'expand_tool', 'Expand tool', show=False,
                priority=True),
        Binding('escape', 'escape', 'Close', show=False, priority=True),
    ]

    def action_submit(self) -> None:
        # 忙时 Enter 只排队到本回合结束；空闲时直接发送。
        self.app.submit_composer(mode='followup')

    def action_submit_now(self) -> None:
        # Alt+Enter：忙时进入 steering，Agent 下一次消费时注入当前回合。
        self.app.submit_composer(mode='steer')

    def action_newline(self) -> None:
        self.insert(chr(10))

    def action_complete(self) -> None:
        self.app.accept_suggestion()

    def action_suggest_prev(self) -> None:
        if self.app.has_suggestions():
            self.app.cycle_suggestion(-1)
            return
        self.move_cursor_relative(rows=-1)

    def action_suggest_next(self) -> None:
        if self.app.has_suggestions():
            self.app.cycle_suggestion(1)
            return
        self.move_cursor_relative(rows=1)

    def action_queued_prev(self) -> None:
        if self.app.has_suggestions():
            self.app.cycle_suggestion(-1)
        else:
            self.app.open_queue_picker()

    def action_queued_next(self) -> None:
        if self.app.has_suggestions():
            self.app.cycle_suggestion(1)

    def action_dequeue(self) -> None:
        self.app.open_queue_picker()

    def action_escape(self) -> None:
        if self.app.has_suggestions():
            self.app.close_suggestions()
        else:
            self.app.cancel_turn()

    def action_expand_tool(self) -> None:
        self.app.toggle_last_tool()


class CompletionPopup(Static):
    """绝对定位的补全浮层；不抢焦点，键盘由 Composer 分发，鼠标点击可选。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._items: list[tuple[str, str]] = []

    def show(self, items: list[tuple[str, str]], index: int) -> None:
        self._items = list(items)
        lines = []
        for position, (_value, label) in enumerate(items[:SUGGESTION_LIMIT]):
            marker = '▸' if position == index else ' '
            lines.append(f' {marker} {label}')
        if len(items) > SUGGESTION_LIMIT:
            lines.append(f'   … {len(items) - SUGGESTION_LIMIT} more')
        self.update(Text('\n'.join(lines)))
        self.display = True

    def on_click(self, event: events.Click) -> None:
        limit = min(len(self._items), SUGGESTION_LIMIT)
        if 0 <= event.y < limit:
            self.app.choose_suggestion(event.y)

    def hide_popup(self) -> None:
        self.display = False


class Transcript(VirtualTranscript):
    """点击命中工具块；拖选结束后自动复制显示文本。"""

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button == 1:
            # 等 Screen 应用最终选区后再取文字，避免复制到拖动的倒数一帧。
            self.call_after_refresh(self._copy_selection)

    def _copy_selection(self) -> None:
        if (self.is_attached and self.text_selection is not None
                and self.app._selected_text()):
            self.app.action_copy_selection()

    def on_click(self, event: events.Click) -> None:
        y = event.y + int(self.scroll_offset.y)
        self.app.on_transcript_click(y)


class HarnessApp(App):
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
        self._exiting = True
        bus.unsubscribe(self._on_event)
        self._pending.put(None)

    def on_resize(self, event: events.Resize) -> None:
        self._place_popup()

    def _flush_layout(self, force: bool = False) -> None:
        if not self._dirty:
            return
        if not force and self._busy:
            streaming = any(block.kind == 'assistant'
                            and block.payload.get('streaming')
                            for block in reversed(self.model.snapshot()[-3:]))
            if streaming and time.monotonic() - self._last_stream_render < 0.12:
                return      # 流式 Markdown 很贵；保持 dirty，下个 tick 再整体重排
        self._last_stream_render = time.monotonic()
        self._dirty = False
        self.view.refresh_layout()

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

    def _load_history(self, messages: list) -> None:
        for message in messages:
            role = message.get('role')
            content = message.get('content')
            if role == 'user':
                self.model.append('user', {'text': str(content or '')})
            elif role == 'assistant':
                self.model.append('assistant', {'text': str(content or '')})
            elif role == 'tool':
                self.model.append('tool', {
                    'name': 'tool', 'args': {}, 'state': 'complete',
                    'output': str(content or ''), 'expanded': False})
        self._mark_dirty()

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
            # 流式也用 Markdown 渲染，避免先看到 `**源码**` 再“变成”正文；
            # Rich 能处理未闭合的标记，最后一块只是复用同一渲染路径。
            return _render_markdown(text, width)
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

    def _handle_command(self, text: str) -> bool:
        """返回 True 表示已作为命令处理，不再发给模型。"""
        if not text.startswith('/'):
            return False
        raw = text[1:].strip()
        verb, _, argument = raw.partition(' ')
        verb = verb.lower()
        argument = argument.strip()

        if verb.startswith('skill:'):
            self.submit_skill(verb[len('skill:'):], argument)
            return True

        spec = commands.find(verb)
        if spec is None:
            hint = commands.did_you_mean(verb)
            suffix = f'；是否想输入 {hint}？' if hint else ''
            self._notice(f'Unknown command: /{verb}{suffix}', level='error')
            return True
        verb = spec.name
        if not spec.ported:
            self._notice(f'/{verb} 暂未迁移到新 TUI；请用 --ui ansi。')
            return True

        if self._busy and verb in ('new', 'resume', 'sessions', 'fork', 'clone', 'cd'):
            self._notice('当前任务尚未结束，请结束后再切换会话或工作区。')
            return True

        import harness
        if verb == 'quit':
            self.exit()
        elif verb == 'help':
            spec = commands.find(argument) if argument else None
            if spec is None:
                self._notice(commands.help_text())
            else:
                availability = ('' if spec.ported
                                else f'{chr(10)}仅行式 REPL 提供（--ui ansi）')
                self._notice(f'{spec.usage}{chr(10)}{spec.description}'
                             f'{availability}')
        elif verb == 'hotkeys':
            self._notice(commands.hotkeys_text())
        elif verb == 'clear':
            self.model.clear()
            self._mark_dirty()
        elif verb == 'more':
            self._notice('新 TUI 启动时已加载全部历史，无需 /more。')
        elif verb in ('status', 'pwd'):
            state = self._display_status()
            lines = [f"model: {harness.os.getenv('LLM_MODEL_ID', '') or 'unset'}",
                     f'workspace: {harness.get_workdir()}']
            if state.get('ctx_pct') is not None:
                lines.append(f"context: {state['ctx_pct']:.0f}%")
            if state.get('tokens'):
                lines.append(f"tokens: {state['tokens']}")
            prompt_tokens = state.get('prompt_tokens')
            cached_tokens = state.get('cached_tokens')
            if prompt_tokens and cached_tokens is not None:
                lines.append(
                    f"cache: {100 * min(1.0, cached_tokens / prompt_tokens):.0f}%")
            self._notice(chr(10).join(lines))
        elif verb == 'model':
            if argument:
                self._set_model(argument.strip())
            else:
                self.open_model_picker()
        elif verb == 'diff':
            try:
                self._notice(harness.format_workspace_diff(color=False))
            except Exception as exc:
                self._notice(f'Diff failed: {exc}', level='error')
        elif verb == 'cache':
            try:
                self._notice(harness.tool_cache.handle_cache_command(
                    harness.TOOL_RESULTS_DIR, harness.SESSIONS_DIR, argument))
            except Exception as exc:
                self._notice(f'Cache failed: {exc}', level='error')
        elif verb == 'sandbox':
            try:
                if argument:
                    message = harness.apply_sandbox_tier(argument)
                    self._status['sandbox'] = argument.split()[0]
                    self._refresh_footer()
                else:
                    message = harness.format_sandbox_tier()
                self._notice(message)
            except Exception as exc:
                self._notice(f'Sandbox failed: {exc}', level='error')
        elif verb == 'review':
            self.start_review()
        elif verb == 'watch':
            self.start_watch(argument)
        elif verb == 'copy':
            self.copy_last_tool()
        elif verb == 'dequeue':
            self.open_queue_picker()
        elif verb == 'expand':
            self.toggle_last_tool()
            self._notice('Toggled the most recent tool block.')
        elif verb == 'approvals':
            error = harness.command_error('/approvals', argument)
            if error:
                self._notice(error, level='error')
            elif argument:
                index, choice = argument.split()
                pending = harness.hooks.GATE.pending()
                if not 1 <= int(index) <= len(pending):
                    self._notice('没有该序号的待审批操作；先运行 /approvals 查看列表。')
                else:
                    req = pending[int(index) - 1]
                    record = harness.hooks.GATE.resolve(req.req_id, choice == 'y', by='cli')
                    self._notice(f'已决议：{record.status} · {req.tool}')
            else:
                self._notice(harness.format_pending_approvals())
        elif verb == 'revoke':
            import hooks
            count = hooks.GATE.revoke_session(
                self._session_id or hooks.current_session_id() or None)
            self._notice(f'已撤销 {count} 条当前会话预授权。')
        elif verb == 'workspaces':
            self.show_workspaces()
        elif verb == 'workspace':
            self.handle_workspace(argument)
        elif verb == 'cd':
            self.change_workspace(argument)
        elif verb == 'find':
            self.start_find(argument)
        elif verb in ('archive', 'delete'):
            self.start_cleanup(verb, argument)
        elif verb == 'new':
            self.start_new_session(argument)
        elif verb == 'rename':
            self.rename_session(argument)
        elif verb == 'fork':
            if argument:
                self.fork_session(argument)
            else:
                self.open_fork_picker()
        elif verb == 'clone':
            self.fork_session('')
        elif verb == 'checkpoint':
            self.create_checkpoint(argument)
        elif verb == 'checkpoints':
            self.show_checkpoints()
        elif verb == 'checkpoint-gc':
            if self._busy:
                self._notice('当前任务尚未结束，请结束后再清理快照。')
            elif not self._store:
                self._notice('No session context.', level='error')
            else:
                self.start_checkpoint_gc(argument)
        elif verb == 'rewind':
            self.start_rewind(argument)
        elif verb == 'undo':
            if argument:
                self._notice('用法：/undo（不带参数）', level='error')
            else:
                self.start_rewind('last --yes')
        elif verb == 'compact':
            self.start_compact(argument)
        elif verb == 'sessions':
            self.open_session_picker()
        elif verb == 'resume':
            if argument:
                asyncio.create_task(self._resume_by_argument(argument))
            else:
                self.open_session_picker()
        elif verb == 'export':
            try:
                path = harness.export_session(self._messages)
                self._notice(f'Exported transcript: {path}')
            except Exception as exc:
                self._notice(f'Export failed: {exc}', level='error')
        elif verb in ('allow', 'deny', 'ask'):
            if not argument:
                self._notice(f'Use /{verb} <tool>.')
            else:
                harness.permission_gate.set_policy(argument, verb)
                self._notice(f'Permission policy for {argument}: {verb}')
        return True

    def _display_status(self) -> dict:
        return dict(self._status)

    def open_model_picker(self) -> None:
        import harness
        current = harness.os.getenv('LLM_MODEL_ID', '')
        models = sorted(set(harness.MODEL_WINDOWS) | ({current} if current else set()))
        self.push_screen(ModelPickerModal(models, current), self._on_model_picked)

    def _on_model_picked(self, model) -> None:
        if model:
            self._set_model(str(model))

    def _set_model(self, model: str) -> None:
        import harness
        harness.os.environ['LLM_MODEL_ID'] = model
        self._notice(f'model: {model}')
        self._refresh_footer()

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

    # ---------- 工作区 ----------

    def show_workspaces(self) -> None:
        import harness
        self._notice(harness.format_workspace_list())

    def handle_workspace(self, argument: str) -> None:
        """`/workspace [list|add <路径> [--name 名称]|remove [选择器]]`。"""
        import harness
        parts = (argument or '').split(maxsplit=1)
        action = parts[0].lower() if parts else 'list'
        if action in ('', 'list'):
            self.show_workspaces()
            return
        if action == 'add':
            try:
                entry = harness.add_workspace_from_argument(
                    parts[1] if len(parts) > 1 else '')
            except (OSError, ValueError) as exc:
                self._notice(f'[workspace] {exc}', level='error')
                return
            self._notice(f"已登记工作区：{entry['name']}  {entry['path']}")
            return
        if action == 'remove':
            selector = parts[1].strip() if len(parts) > 1 else ''
            if not selector:
                self.open_workspace_remover()
                return
            try:
                removed = harness.remove_workspace_selector(selector)
            except (KeyError, OSError, ValueError) as exc:
                self._notice(f'[workspace] {exc}', level='error')
                return
            self._notice(f"已移除登记：{removed['name']}  {removed['path']}")
            return
        self._notice('用法：/workspaces；/workspace add <路径> [--name 名称]；'
                     '/workspace remove [序号|名称|路径]', level='error')

    def open_workspace_remover(self) -> None:
        import harness
        entries = harness.registered_workspaces()
        if not entries:
            self._notice('尚未登记工作区。')
            return
        rows = [{'value': entry['id'],
                 'label': f"{index}. {entry['name']}  {entry['path']}",
                 'search': f"{entry['name']} {entry['path']} {entry['id']}"}
                for index, entry in enumerate(entries, 1)]
        self.push_screen(
            ChoiceModal('Remove workspace', rows,
                        hint='Enter 移除登记 · Esc 取消'),
            self._on_workspace_remove_picked)

    def _on_workspace_remove_picked(self, workspace_id) -> None:
        if not workspace_id:
            return
        import harness
        try:
            removed = harness.workspaces.remove_workspace(
                str(workspace_id), file=harness.workspace_registry_path())
        except (KeyError, OSError, ValueError) as exc:
            self._notice(f'[workspace] {exc}', level='error')
            return
        self._notice(f"已移除登记：{removed['name']}  {removed['path']}")

    def change_workspace(self, argument: str) -> None:
        """`/cd [路径]`：只切 Agent 工作区，不改进程 cwd（同旧 REPL）。"""
        import harness
        argument = (argument or '').strip()
        if not argument:
            self._notice(f'工作区：{harness.get_workdir()}')
            return
        path = argument
        if len(path) >= 2 and path[0] == path[-1] and path[0] in ('"', "'"):
            path = path[1:-1]
        try:
            workspace = harness.set_workspace(path)
            if self._store is not None:
                self._store.record_workspace(workspace)
        except (OSError, ValueError) as exc:
            self._notice(f'[workspace] {exc}', level='error')
            return
        self._mention_root = None      # @ 文件索引跟随工作区重扫
        self._refresh_footer()
        self._notice(f'工作区：{workspace}')

    # ---------- 跨会话搜索 ----------

    def start_find(self, argument: str) -> None:
        import harness
        argument = (argument or '').strip()
        error = harness.command_error('/find', argument)
        if error:
            self._notice(error, level='error')
            return
        block = self.model.append('notice',
                                  {'text': f'find: 搜索「{argument}」…'})
        self._mark_dirty()
        threading.Thread(target=self._run_find,
                         args=(block.id, argument), daemon=True).start()

    def _run_find(self, block_id: str, query: str) -> None:
        import harness
        try:
            rows = harness.SessionEventStore.search_all(
                harness.SESSIONS_DIR, query, limit=8)
            error = ''
        except Exception as exc:
            rows = []
            error = f'{type(exc).__name__}: {exc}'
        try:
            self.call_from_thread(self._find_done, block_id, query, rows, error)
        except Exception:
            pass

    def _find_done(self, block_id: str, query: str, rows: list,
                   error: str = '') -> None:
        if self.model.get(block_id) is None:
            return
        from turn_progress import relative_time
        if self.model.get(block_id) is not None:
            if error:
                text = f'find: {error}'
            elif rows:
                text = f'find: {len(rows)} 个会话命中「{query}」。'
            else:
                text = f'没有包含「{query}」的会话。'
            self.model.update(block_id, text=text,
                              level='error' if error else 'info')
            self._mark_dirty()
        if error or not rows:
            return
        self._find_rows = {row['id']: row for row in rows}
        choices = [{
            'value': row['id'],
            'label': (f"{row['title']}  ·  {relative_time(row.get('updated'))}"
                      f"  ·  {row['id'][:8]}"),
            'search': ' '.join(str(row.get(key) or '') for key in
                               ('title', 'workspace', 'id', 'matched')),
        } for row in rows]
        self.push_screen(
            ChoiceModal('Find sessions', choices,
                        hint='Enter 恢复会话 · Esc 关闭'),
            self._on_find_picked)

    def _on_find_picked(self, session_id) -> None:
        if not session_id:
            return
        row = getattr(self, '_find_rows', {}).get(str(session_id))
        if row is not None:
            asyncio.create_task(self._resume_session_row(row))

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

    # ---------- 会话归档 / 删除 ----------

    def start_cleanup(self, command: str, argument: str) -> None:
        """`/archive`、`/delete`：后台扫描候选，再打开多选与确认。"""
        import harness
        if self._busy:
            self._notice('Cannot clean up sessions while a request is running.')
            return
        if not self._store:
            self._notice('No session context.', level='error')
            return
        error = harness.command_error('/' + command, argument)
        if error:
            self._notice(error, level='error')
            return
        mode = ('delete' if command == 'delete'
                else 'restore' if argument == 'restore' else 'archive')
        block = self.model.append('notice',
                                  {'text': f'cleanup: 扫描{mode}候选…'})
        self._mark_dirty()
        threading.Thread(target=self._scan_cleanup,
                         args=(block.id, command, argument), daemon=True).start()

    def _scan_cleanup(self, block_id: str, command: str, argument: str) -> None:
        import harness
        try:
            harness.wait_for_housekeeping()
            if harness.housekeeping_busy():
                raise RuntimeError('会话后台整理尚未结束，请稍后再清理。')
            archive, rows = harness.session_cleanup_plan(
                command, argument, self._session_id, emit=lambda text: None)
        except Exception as exc:
            try:
                self.call_from_thread(
                    self._cleanup_failed, block_id,
                    f'{type(exc).__name__}: {exc}')
            except Exception:
                pass
            return
        try:
            self.call_from_thread(self._cleanup_scan_done, block_id,
                                  archive, rows, command, argument)
        except Exception:
            pass

    def _cleanup_failed(self, block_id: str, message: str) -> None:
        if self.model.get(block_id) is not None:
            self.model.update(block_id, text=f'[cleanup] {message}',
                              level='error')
            self._mark_dirty()
        if self._cleanup_block_id == block_id:
            self._cleanup_block_id = None
            self._finish_local_operation()

    def _cleanup_scan_done(self, block_id: str, archive, rows: list,
                           command: str, argument: str) -> None:
        if self.model.get(block_id) is None:
            return
        mode = ('delete' if command == 'delete'
                else 'restore' if argument == 'restore' else 'archive')
        if self._busy:
            self._cleanup_failed(block_id, '当前任务尚未结束，请稍后重新清理。')
            return
        if self.model.get(block_id) is not None:
            if rows:
                text = f'[cleanup] {len(rows)} 个会话可{self._cleanup_action(mode)}。'
            elif mode in ('delete', 'restore'):
                text = '[cleanup] 没有已归档的会话。'
            else:
                text = '[cleanup] 没有符合条件的会话。'
            self.model.update(block_id, text=text)
            self._mark_dirty()
        if not rows:
            return
        self.push_screen(
            CleanupPickerModal(rows, mode=mode),
            partial(self._on_cleanup_selected,
                    archive=archive, rows=rows, mode=mode))

    @staticmethod
    def _cleanup_action(mode: str) -> str:
        return {'delete': '永久删除', 'restore': '恢复'}.get(mode, '归档')

    def _on_cleanup_selected(self, selected, archive, rows: list,
                             mode: str) -> None:
        if not selected:
            self._notice('已取消，未执行改动。')
            return
        chosen_ids = set(selected)
        chosen = [row for row in rows if row['id'] in chosen_ids]
        action = self._cleanup_action(mode)
        detail = chr(10).join(
            f"  {row.get('title') or '未命名任务'} · "
            f"{row.get('workspace') or '目录未记录'}"
            for row in chosen)
        body = (f'将{action}以下 {len(chosen)} 个会话（含关联快照）：'
                + chr(10) + detail)
        if mode == 'delete':
            body += chr(10) + '记录及关联快照不可恢复；项目文件不受影响。'
            self.push_screen(
                TypedConfirmModal('Delete archived sessions?', body,
                                  phrase='删除'),
                partial(self._execute_cleanup,
                        archive=archive, rows=chosen, mode=mode))
            return
        label = 'Restore' if mode == 'restore' else 'Archive'
        self.push_screen(
            ConfirmModal(f'{action}会话?', body, label),
            partial(self._execute_cleanup,
                    archive=archive, rows=chosen, mode=mode))

    def _execute_cleanup(self, confirmed, archive, rows: list,
                         mode: str) -> None:
        import harness
        if not confirmed:
            self._notice('已取消，未执行改动。')
            return
        if self._busy or harness.housekeeping_busy():
            self._notice('当前任务或后台整理尚未结束，请稍后重新清理。')
            return
        self._busy = True
        self._operation = 'cleanup'
        self._turn_started = time.monotonic()
        block = self.model.append(
            'notice', {'text': f'[cleanup] {self._cleanup_action(mode)}中…'})
        self._cleanup_block_id = block.id
        self._mark_dirty()
        threading.Thread(target=self._run_cleanup,
                         args=(block.id, archive, rows, mode),
                         daemon=True).start()

    def _run_cleanup(self, block_id: str, archive, rows: list,
                     mode: str) -> None:
        import harness
        try:
            completed, failures = harness.apply_session_cleanup(
                archive, rows, mode, self._session_id)
        except Exception as exc:
            try:
                self.call_from_thread(
                    self._cleanup_failed, block_id,
                    f'{type(exc).__name__}: {exc}')
            except Exception:
                pass
            return
        try:
            self.call_from_thread(self._cleanup_done, block_id,
                                  completed, len(rows), failures, mode)
        except Exception:
            pass

    def _cleanup_done(self, block_id: str, completed: int, total: int,
                      failures: list, mode: str) -> None:
        action = self._cleanup_action(mode)
        lines = [f'[cleanup] 已{action} {completed}/{total} 个会话。']
        if mode == 'delete':
            lines.append('记录及关联快照不可恢复，项目文件未改动。')
        elif mode == 'restore':
            lines.append('可用 /resume 继续会话。')
        else:
            lines.append('用 /archive restore 可恢复；项目文件未改动。')
        for row, error in failures:
            lines.append(f"未处理 {row.get('title') or row['id']}：{error}")
        if self.model.get(block_id) is not None:
            self.model.update(block_id, text=chr(10).join(lines),
                              level='error' if failures else 'info')
            self._mark_dirty()
            self._refresh_footer()
        if self._cleanup_block_id == block_id:
            self._cleanup_block_id = None
            self._finish_local_operation()

    def _finish_local_operation(self) -> None:
        self._busy = False
        self._operation = 'agent'
        self._refresh_footer()
        if self._pending.snapshot():
            self._start_worker()

    def _adopt_session(self, store, messages: list, title: str = '') -> None:
        self._store = store
        self._session_id = store.session_id
        self._messages = list(messages)
        self._title = title
        self._status.clear()
        self._tool_blocks.clear()
        self._mention_root = None
        self.model.clear()
        self._load_history(messages)
        self._refresh_footer()
        self._mark_dirty()

    def start_new_session(self, argument: str = '') -> None:
        """`/new [--ws <序号|名称|路径>|会话ID]`（对齐旧 REPL 与 Pi `/new`）。"""
        import harness
        requested = (argument or '').strip()
        target = harness.get_workdir()
        session_id = None
        lowered = requested.lower()
        if lowered.startswith('--'):
            if lowered != '--ws' and not lowered.startswith('--ws '):
                self._notice('用法：/new [--ws <序号|名称|路径>|会话ID]',
                             level='error')
                return
            selector = requested[4:].strip()
            if len(selector) >= 2 and selector[0] == selector[-1] and selector[0] in ('"', "'"):
                selector = selector[1:-1]
            if not selector:
                self._notice('用法：/new --ws <序号|名称|路径>', level='error')
                return
            try:
                target = harness.find_workspace(
                    harness.registered_workspaces(), selector)['path']
            except ValueError as exc:
                # 未登记的目录也允许直接使用，和旧 REPL 保持一致。
                candidate = Path(selector).expanduser()
                if not candidate.is_absolute():
                    candidate = harness.get_workdir() / candidate
                if not candidate.is_dir():
                    self._notice(f'[workspace] {exc}', level='error')
                    return
                target = candidate.resolve()
        elif requested:
            if not requested.replace('-', '').replace('_', '').isalnum():
                self._notice('用法：/new [--ws <序号|名称|路径>|会话ID]',
                             level='error')
                return
            session_id = requested
        try:
            store = harness.start_new_session(workspace=target,
                                              session_id=session_id)
            summary = store.session_summary()
        except Exception as exc:
            self._notice(f'New session failed: {exc}', level='error')
            return
        self._adopt_session(store, [], summary.get('title', ''))
        self._notice(f"New session: {store.session_id}")

    def rename_session(self, argument: str) -> None:
        if not self._store:
            self._notice('No session context; cannot rename.', level='error')
            return
        if not argument:
            self._notice('用法：/rename <标题>', level='error')
            return
        title = self._store.append_session_title(argument[:80], source='user')
        self._title = title['title']
        self._refresh_footer()
        self._notice(f"Session title: {self._title}")

    def fork_session(self, argument: str = '') -> None:
        """`/fork [事件序号]`；`/clone` 无参复制到当前叶。"""
        if not self._store:
            self._notice('No session context; cannot fork.', level='error')
            return
        upto = None
        if argument:
            if not argument.isdigit():
                self._notice('用法：/fork [事件序号]', level='error')
                return
            upto = int(argument)
        if self._fork_to(upto):
            self._notice(f'Forked to {self._session_id} (original kept).')

    def _fork_to(self, upto: int | None) -> bool:
        try:
            forked = self._store.__class__.fork(self._store, upto_seq=upto)
            messages = forked.rebuild_openai_history(recover_pending=True)
            summary = forked.session_summary()
        except Exception as exc:
            self._notice(f'Fork failed: {exc}', level='error')
            return False
        self._adopt_session(forked, messages, summary.get('title', ''))
        return True

    def open_fork_picker(self) -> None:
        """Pi 风格 `/fork`：列出用户消息，默认选最新一条。"""
        if not self._store:
            self._notice('No session context; cannot fork.', level='error')
            return
        rows = self._forkable_messages()
        if not rows:
            self._notice('No messages to fork from.')
            return
        self._fork_rows = {row['seq']: row['text'] for row in rows}
        self.push_screen(ForkPickerModal(rows), self._on_fork_picked)

    def _forkable_messages(self) -> list[dict]:
        rows = []
        try:
            events = self._store.read_events()
        except Exception:
            return rows
        for event in events:
            if event.get('type') != 'message':
                continue
            message = event.get('message') or {}
            if message.get('role') != 'user':
                continue
            content = message.get('content')
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = ''.join(str(block.get('text', ''))
                               for block in content
                               if isinstance(block, dict)
                               and block.get('type') == 'text')
            else:
                text = ''
            text = text.strip()
            if not text or text.startswith('[Compacted]'):
                continue
            rows.append({'seq': int(event.get('seq', 0)), 'text': text})
        return rows

    def _on_fork_picked(self, seq) -> None:
        if seq is None:
            return
        text = getattr(self, '_fork_rows', {}).get(int(seq), '')
        if not self._fork_to(max(0, int(seq) - 1)):
            return
        if text and self._composer is not None:
            self._programmatic_text = text
            self._composer.text = text
            self._composer.cursor_location = (0, len(text))
            self._composer.focus()
        self._notice(f'Forked to {self._session_id}; message restored to editor.')

    def show_checkpoints(self) -> None:
        import harness
        if not self._store:
            self._notice('No session context.', level='error')
            return
        try:
            manager = harness.get_checkpoint_manager(self._store)
            self._notice(harness.format_checkpoint_list(manager))
        except Exception as exc:
            self._notice(f'Checkpoints failed: {exc}', level='error')

    def create_checkpoint(self, label: str = '') -> None:
        """`/checkpoint [label]`：手动快照当前会话（文件前像 + 历史）。"""
        import harness
        if self._busy:
            self._notice('Cannot create a checkpoint while a request is running.')
            return
        if not self._store:
            self._notice('No session context.', level='error')
            return
        try:
            manager = harness.get_checkpoint_manager(self._store)
            cp_id = manager.begin_turn(self._messages, self._store,
                                       label=label.strip(), trigger='manual')
            manager.seal()
            self._notice(f'Created checkpoint {cp_id}. Use /rewind head to restore.')
        except Exception as exc:
            self._notice(f'Checkpoint failed: {exc}', level='error')

    def start_checkpoint_gc(self, argument: str) -> None:
        """`/checkpoint-gc [--all] [--keep N]`：当前会话直接回收，全局先确认。"""
        import harness
        try:
            manager = harness.get_checkpoint_manager(self._store)
            preview = harness.checkpoint_gc_preview(manager, argument)
        except ValueError as exc:
            self._notice(str(exc), level='error')
            return
        except Exception as exc:
            self._notice(f'Checkpoint cleanup failed: {exc}', level='error')
            return
        summary = harness.format_checkpoint_gc(preview)
        if not preview['removed']:
            self._notice(summary)
            return
        if preview['mode'] != 'all':
            self._finish_checkpoint_gc(argument)
            return
        self.push_screen(
            ConfirmModal('Clean old checkpoints?', summary, 'Clean'),
            partial(self._confirm_checkpoint_gc, argument=argument))

    def _confirm_checkpoint_gc(self, confirmed, argument: str) -> None:
        if not confirmed:
            self._notice('已取消，未删除任何快照。')
            return
        if self._busy:
            self._notice('当前任务尚未结束，请结束后再清理快照。')
            return
        self._finish_checkpoint_gc(argument)

    def _finish_checkpoint_gc(self, argument: str) -> None:
        import harness
        try:
            result = harness.checkpoint_gc_execute(
                harness.get_checkpoint_manager(self._store), argument)
            self._notice(harness.format_checkpoint_gc(result, done=True))
        except Exception as exc:
            self._notice(f'Checkpoint cleanup failed: {exc}', level='error')

    def start_rewind(self, argument: str) -> None:
        """`/rewind`：先预览，再确认；`--yes` 跳过确认，`/undo` 即 last --yes。"""
        import harness
        if self._busy:
            self._notice('Cannot rewind while a request is running.')
            return
        if not self._store:
            self._notice('No session context.', level='error')
            return
        argument = (argument or '').strip()
        error = harness.command_error('/rewind', argument)
        if error:
            self._notice(error, level='error')
            return
        parts = argument.split()
        ref = next((part for part in parts if not part.startswith('--')), 'last')
        force = '--force' in parts
        scope = 'session' if '--session' in parts else 'all'
        try:
            manager = harness.get_checkpoint_manager(self._store)
            cp_id = manager.resolve(ref)
            if not manager.snapshot_path(cp_id).exists():
                self._notice(f'[rewind] snapshot missing for {cp_id}', level='error')
                return
            preview = manager.plan_restore(cp_id, scope=scope)
            preview['scope'] = scope
        except Exception as exc:
            self._notice(f'[rewind] {exc}', level='error')
            return
        report = _strip_ansi(harness.format_restore_report(preview))
        if preview['conflicts'] and not force:
            self._notice(report + chr(10) + '未执行任何改动；确认可覆盖后重试 '
                         f'/rewind {cp_id} --force')
            return
        if not preview.get('ok'):
            self._notice(report)
            return
        if '--yes' in parts:
            self._execute_rewind(True, cp_id, force, scope)
            return
        self.push_screen(
            ConfirmModal('Rewind checkpoint?', report, 'Rewind'),
            # callback 先收到 True/False，再带上用于执行的参数
            partial(self._execute_rewind, cp_id=cp_id,
                    force=force, scope=scope),
        )

    def _execute_rewind(self, confirmed: bool, cp_id: str,
                        force: bool, scope: str) -> None:
        if not confirmed or self._busy or not self._store:
            return
        import harness
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                ok = harness.rewind_to(
                    self._messages, self._store,
                    harness.get_checkpoint_manager(self._store),
                    cp_id, force=force, scope=scope, report=False)
        except Exception as exc:
            self._notice(f'[rewind] {exc}', level='error')
            return
        detail = _strip_ansi(output.getvalue()).strip()
        if not ok:
            self._notice(detail or f'[rewind] could not restore {cp_id}',
                         level='error')
            return
        self._reload_transcript()
        self._notice(f'Rewound to {cp_id}.' + (chr(10) + detail if detail else ''))

    def _reload_transcript(self) -> None:
        """回溯会替换 canonical history，按它重建块模型。"""
        self.model.clear()
        self._load_history(self._messages)
        self._mark_dirty()
        self._flush_layout(force=True)
        self._refresh_footer()

    def start_compact(self, instructions: str = '') -> None:
        """`/compact`：后台压缩当前上下文（Pi `/compact [instructions]`）。"""
        if self._busy:
            self._notice('Cannot compact while a request is running.')
            return
        if not self._store:
            self._notice('No session context.', level='error')
            return
        if not self._messages:
            self._notice('当前会话没有可压缩的内容。')
            return
        self._busy = True
        self._operation = 'compact'
        self._turn_started = time.monotonic()
        block = self.model.append('notice', {'text': 'compact: running…'})
        self._mark_dirty()
        threading.Thread(target=self._run_compact,
                         args=(block.id, list(self._messages)),
                         daemon=True).start()

    def _run_compact(self, block_id: str, messages: list) -> None:
        import harness
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                compacted = harness.compact_history(list(messages))
                harness._record_compaction(self._store, compacted, 'manual')
        except Exception as exc:
            self.call_from_thread(self._compact_done, block_id, None,
                                  f'Compact failed: {exc}')
            return
        self.call_from_thread(self._compact_done, block_id, compacted, '')

    def _compact_done(self, block_id: str, compacted, error: str = '') -> None:
        self._busy = False
        if compacted is not None:
            self._messages[:] = compacted
            text = (f'已压缩会话上下文：{len(compacted)} 条消息。'
                    '完整记录已保存。')
        else:
            text = error
        if self.model.get(block_id) is not None:
            self.model.update(block_id, text=text)
        self._mark_dirty()
        self._refresh_footer()
        self._operation = 'agent'
        if self._pending.snapshot() and not self._busy:
            self._start_worker()

    def open_session_picker(self) -> None:
        import harness
        rows = harness.session_rows(limit=None, include_empty=False)
        if not rows:
            self._notice('No saved sessions.')
            return
        self.push_screen(SessionPickerModal(rows), self._on_session_picked)

    def _on_session_picked(self, row) -> None:
        if row:
            asyncio.create_task(self._resume_session_row(row))

    async def _resume_by_argument(self, argument: str) -> None:
        import harness
        rows = harness.session_rows(limit=None, include_empty=False)
        if not rows:
            self._notice('No saved sessions.')
            return
        selected = None
        if argument.casefold() == 'last':
            selected = rows[0]
        elif argument.isdigit():
            index = int(argument)
            if 1 <= index <= len(rows):
                selected = rows[index - 1]
        else:
            matches = [row for row in rows
                       if row['id'].startswith(argument)
                       or argument.casefold() in (row['title'] or '').casefold()]
            if len(matches) == 1:
                selected = matches[0]
            elif len(matches) > 1:
                self._notice(f'{len(matches)} sessions match; use /sessions.')
                return
        if selected is None:
            self._notice(f'No session matches {argument!r}.')
            return
        await self._resume_session_row(selected)

    async def _resume_session_row(self, row: dict) -> None:
        import harness
        if self._busy:
            self._notice('当前任务尚未结束，请结束后再恢复会话。')
            return
        try:
            store = harness.SessionEventStore.open(
                harness.resolve_session_path(row['id']))
            messages = store.rebuild_openai_history(recover_pending=True)
            summary = store.session_summary()
        except Exception as exc:
            self._notice(f'Unable to resume: {exc}', level='error')
            return
        try:
            if summary.get('workspace'):
                harness.set_workspace(summary['workspace'])
        except (OSError, ValueError) as exc:
            self._notice(f'Unable to resume workspace: {exc}', level='error')
            return
        self._mention_root = None
        self._adopt_session(store, messages, summary.get('title', ''))
        self._notice(f"Resumed {summary['title']}.")

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
    HarnessApp(messages or [], session_id=session_id, title=title,
               session_store=session_store).run()
