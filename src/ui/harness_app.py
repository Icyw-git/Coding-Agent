"""新 TUI（P2/P3）：行模型 + 虚拟视口 + 浮层补全 + 队列条。

feature flag：`HARNESS_UI=textual-next`。旧 `ui/app.py` 仍是默认实现。
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static, TextArea

from file_mentions import workspace_files
from ui import commands
from ui.composer.completions import apply_suggestion, suggestions
from ui.modals.approval import PermissionModal
from ui.modals.sessions import SessionPickerModal
from ui.renderers.tools import tool_view
from ui.transcript import ConversationModel, TranscriptLayout, VirtualTranscript
from ui_events import UIEvent, bus

FOLLOW_END = True
SUGGESTION_LIMIT = 6
SPINNERS = ('⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')


def _render_lines(renderable, width: int) -> list[Text]:
    """把 Rich renderable 渲染成固定宽度的 Text 行（虚拟视口用）。"""
    console = Console(width=width, force_terminal=True, color_system='truecolor',
                      legacy_windows=False, safe_box=True)
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

    def snapshot(self) -> list[str]:
        with self._lock:
            return [item for item in self._items if item is not None]

    def __len__(self) -> int:
        return len(self.snapshot())


class Composer(TextArea):
    """多行输入：Enter 发送、Ctrl-J 换行、Tab 补全、Alt+↑ 取回队列。"""

    BINDINGS = [
        Binding('enter', 'submit', 'Send', show=False, priority=True),
        Binding('ctrl+j', 'newline', 'Newline', show=False, priority=True),
        Binding('tab', 'complete', 'Complete', show=False),
        Binding('alt+up', 'suggest_prev', 'Prev', show=False),
        Binding('alt+down', 'suggest_next', 'Next', show=False),
        Binding('ctrl+o', 'expand_tool', 'Expand tool', show=False,
                priority=True),
        Binding('escape', 'escape', 'Close', show=False, priority=True),
    ]

    def action_submit(self) -> None:
        self.app.submit_text(self.text)
        self.text = ''

    def action_newline(self) -> None:
        self.insert(chr(10))

    def action_complete(self) -> None:
        self.app.accept_suggestion()

    def action_suggest_prev(self) -> None:
        if self.app.has_suggestions():
            self.app.cycle_suggestion(-1)
        else:
            self.app.take_last_queued()

    def action_suggest_next(self) -> None:
        if self.app.has_suggestions():
            self.app.cycle_suggestion(1)

    def action_escape(self) -> None:
        if self.app.has_suggestions():
            self.app.close_suggestions()
        else:
            self.app.cancel_turn()

    def action_expand_tool(self) -> None:
        self.app.toggle_last_tool()


class CompletionPopup(Static):
    """绝对定位的补全浮层；不抢焦点，键盘仍由 Composer 分发。"""

    def show(self, items: list[tuple[str, str]], index: int) -> None:
        lines = []
        for position, (_value, label) in enumerate(items[:SUGGESTION_LIMIT]):
            marker = '▸' if position == index else ' '
            lines.append(f' {marker} {label}')
        if len(items) > SUGGESTION_LIMIT:
            lines.append(f'   … {len(items) - SUGGESTION_LIMIT} more')
        self.update(Text('\n'.join(lines)))
        self.display = True

    def hide_popup(self) -> None:
        self.display = False


class Transcript(VirtualTranscript):
    """点击视口：把屏幕 y 转成内容行，再交给 App 做 block 命中。"""

    def on_click(self, event: events.Click) -> None:
        y = event.y + int(self.scroll_offset.y)
        self.app.on_transcript_click(y)


class HarnessApp(App):
    """行模型驱动的 TUI：聊天区只有一个自绘视口。"""

    BINDINGS = [Binding('ctrl+q', 'quit_app', 'Quit', show=False,
                        priority=True)]

    CSS = """
    Screen { background: $background; }
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
    #perm-box { width: 76; padding: 2 3; border: round $warning;
                background: $boost; }
    #perm-box.risk-danger { border: round $error; }
    #session-box { width: 90; max-height: 28; padding: 1 2;
                   border: round $accent; background: $boost; }
    #session-list { height: auto; max-height: 18; }
    #session-search { margin: 0 0 1 0; }
    """

    def __init__(self, messages: list | None = None,
                 session_id: str | None = None, title: str = '') -> None:
        super().__init__()
        self._source_messages = list(messages or [])
        self._messages = list(messages or [])
        self._session_id = session_id or ''
        self._title = title
        self.model = ConversationModel()
        self.doc_layout = TranscriptLayout(self._render_block)
        self.view = Transcript(self.model, self.doc_layout,
                               follow_end=FOLLOW_END)
        self._events: queue.Queue = queue.Queue()
        self._pending = PendingQueue()
        self._tool_blocks: dict[str, str] = {}
        self._dirty = True
        self._busy = False
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

    def action_quit_app(self) -> None:
        self.exit()

    def on_unmount(self) -> None:
        self._exiting = True
        bus.unsubscribe(self._on_event)
        self._pending.put(None)

    def on_resize(self, event: events.Resize) -> None:
        self._place_popup()

    def _flush_layout(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        self.view.refresh_layout()

    def _refresh_footer(self) -> None:
        if self._footer is None:
            return
        parts = []
        if self._title:
            parts.append(self._title[:40])
        try:
            import harness
            parts.append(harness.os.getenv('LLM_MODEL_ID', '') or '(model unset)')
            workspace = harness.get_workdir()
            branch = harness.current_branch(workspace)
            label = f'{workspace.name} ({branch})' if branch else workspace.name
            parts.append(str(label))
        except Exception:
            pass
        if self._status.get('ctx_pct') is not None:
            parts.append(f"ctx {self._status['ctx_pct']:.0f}%")
        tokens = self._status.get('tokens')
        if tokens:
            parts.append(f'tok {tokens}')
        if self._status.get('cost') and self._status.get('cost_source') != 'unavailable':
            symbol = '¥' if self._status.get('currency', 'CNY') == 'CNY' else ''
            parts.append(f"~{symbol}{self._status['cost']}")
        queued = len(self._pending)
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

    def _refresh_queue(self) -> None:
        if self._queue_widget is None:
            return
        items = self._pending.snapshot()
        if not items:
            self._queue_widget.display = False
            self._queue_widget.update('')
            return
        preview = ' | '.join(item.replace(chr(10), ' ')[:24] for item in items[:3])
        more = f' … +{len(items) - 3}' if len(items) > 3 else ''
        self._queue_widget.update(f'⏳ {len(items)} queued: {preview}{more} '
                                  '· Alt+↑ 取回')
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
        queued = len(self._pending)
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
            self.push_screen(PermissionModal(
                str(payload.get('req_id', '')),
                str(payload.get('tool', '')),
                payload.get('args') or {},
                str(payload.get('title', 'Permission needed')),
                str(payload.get('risk', 'warning'))))
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
            if payload.get('streaming'):
                return _render_lines(Text(text), width)
            return _render_lines(RichMarkdown(text), width)
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

    def toggle_last_tool(self) -> None:
        for block in reversed(self.model.snapshot()):
            if block.kind == 'tool':
                self.toggle_tool(block.id)
                return

    def on_transcript_click(self, line_index: int) -> None:
        block_id = self.doc_layout.block_at(line_index)
        if block_id:
            self.toggle_tool(block_id)

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

        spec = commands.find(verb)
        if spec is None:
            hint = commands.did_you_mean(verb)
            suffix = f'；是否想输入 {hint}？' if hint else ''
            self._notice(f'Unknown command: /{verb}{suffix}', level='error')
            return True
        verb = spec.name
        if not spec.ported:
            self._notice(f'/{verb} 暂未迁移到 textual-next；请用 --ui ansi。')
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
        elif verb in ('status', 'model', 'pwd'):
            state = self._display_status()
            lines = [f"model: {harness.os.getenv('LLM_MODEL_ID', '') or 'unset'}",
                     f'workspace: {harness.get_workdir()}']
            if state.get('ctx_pct') is not None:
                lines.append(f"context: {state['ctx_pct']:.0f}%")
            if state.get('tokens'):
                lines.append(f"tokens: {state['tokens']}")
            self._notice(chr(10).join(lines))
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
        if argument.isdigit():
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
        try:
            store = harness.SessionEventStore.open(
                harness.resolve_session_path(row['id']))
            messages = store.rebuild_openai_history(recover_pending=True)
            summary = store.session_summary()
        except Exception as exc:
            self._notice(f'Unable to resume: {exc}', level='error')
            return
        if summary.get('workspace'):
            harness.set_workspace(summary['workspace'])
        self._messages = list(messages)
        self._session_id = store.session_id
        self.model.clear()
        self._load_history(messages)
        self._refresh_footer()
        self._notice(f"Resumed {summary['title']}.")

    def cancel_turn(self) -> None:
        """Esc：仅当回合进行中才发取消，避免空闲时误触。"""
        if not self._busy:
            return
        import turn_control
        turn_control.request_cancel()
        if self._composer is not None:
            self._composer.border_title = 'cancelling…'

    def submit_shell(self, command: str) -> None:
        """`!cmd`：直接执行 shell，输出按工具块展示，不进入模型上下文。"""
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
        self.model.update(block_id,
                          state='failed' if failed else 'complete',
                          output=str(output), duration_ms=duration_ms)
        self._mark_dirty()

    def submit_text(self, text: str) -> None:
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
        self.model.append('user', {'text': text})
        self._mark_dirty()
        self._pending.put(text)
        self._refresh_queue()
        if not self._busy:
            self._start_worker()

    def _start_worker(self) -> None:
        self._busy = True
        self._turn_started = time.monotonic()
        self._refresh_footer()
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
                turn_control.begin_turn()
                try:
                    with harness.agent_lock:
                        harness.permission_gate.use_ui = True
                        harness.agent_loop(self._messages)
                except KeyboardInterrupt:
                    pass
                finally:
                    turn_control.end_turn()
                    harness.permission_gate.use_ui = False
                self.call_from_thread(self._refresh_queue)
                self.call_from_thread(self._refresh_footer)
                if len(self._pending) == 0:
                    self._busy = False
                    break
        finally:
            self._busy = False


def run(messages: list | None = None, session_id: str | None = None,
        title: str = '') -> None:
    HarnessApp(messages or [], session_id=session_id, title=title).run()
