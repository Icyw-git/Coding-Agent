"""新 TUI 骨架（P2）：ConversationModel + VirtualTranscript。

仍是 feature flag 阶段；旧 `ui/app.py` 继续作为默认实现，迁移完成后再切换。
这里先打通「事件 → 块模型 → 虚拟视口」的主链路。
"""
from __future__ import annotations

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

from ui.transcript import ConversationModel, TranscriptLayout, VirtualTranscript
from ui_events import UIEvent, bus

TOOL_PREVIEW_LINES = 10
FOLLOW_END = True


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


def _tool_summary(name: str, args: dict) -> str:
    for key in ('command', 'path', 'pattern', 'url', 'query', 'name'):
        value = (args or {}).get(key)
        if value:
            text = ' '.join(str(value).split())
            return text if len(text) <= 72 else text[:69] + '...'
    return ''


class Composer(TextArea):
    """多行输入：Enter 发送、Ctrl-J 换行（P3 会加浮层补全）。"""

    BINDINGS = [
        Binding('enter', 'submit', 'Send', show=False, priority=True),
        Binding('ctrl+j', 'newline', 'Newline', show=False, priority=True),
    ]

    def action_submit(self) -> None:
        self.app.submit_text(self.text)
        self.text = ''

    def action_newline(self) -> None:
        self.insert(chr(10))


class Transcript(VirtualTranscript):
    """点击视口：把屏幕 y 转成内容行，再交给 App 做 block 命中。"""

    def on_click(self, event: events.Click) -> None:
        y = event.y + int(self.scroll_offset.y)
        self.app.on_transcript_click(y)


class HarnessApp(App):
    """行模型驱动的 TUI：聊天区只有一个自绘视口。"""

    CSS = """
    Screen { background: $background; }
    #footer { height: 1; padding: 0 2; color: $text-muted; background: $surface; }
    #dock { dock: bottom; height: auto; background: $surface; }
    #composer { height: auto; max-height: 8; padding: 0 1;
                border: round $accent; background: $surface; }
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
        self._input_q: queue.Queue = queue.Queue()
        self._tool_blocks: dict[str, str] = {}
        self._dirty = True
        self._busy = False
        self._exiting = False
        self._status: dict = {}
        self._footer = None
        self._composer = None

    # ---------- 布局 ----------

    def compose(self) -> ComposeResult:
        yield self.view
        with Vertical(id='dock'):
            yield Static('', id='footer')
            yield Composer(id='composer')

    def on_mount(self) -> None:
        bus.subscribe(self._on_event)
        self._composer = self.query_one('#composer', Composer)
        self._footer = self.query_one('#footer', Static)
        self._load_history(self._source_messages)
        self._refresh_footer()
        self._composer.focus()
        self.set_interval(0.03, self._drain_events)
        self.set_interval(0.05, self._flush_layout)

    def on_unmount(self) -> None:
        self._exiting = True
        bus.unsubscribe(self._on_event)
        self._input_q.put(None)

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
            parts.append(str(harness.get_workdir()))
        except Exception:
            pass
        if self._status.get('ctx_pct') is not None:
            parts.append(f"ctx {self._status['ctx_pct']:.0f}%")
        queued = self._input_q.qsize()
        if queued:
            parts.append(f'{queued} queued')
        if self._busy:
            parts.append('working')
        self._footer.update('  ·  '.join(p for p in parts if p))

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
            name = payload.get('name', 'tool')
            state = payload.get('state', 'running')
            summary = _tool_summary(name, payload.get('args') or {})
            duration = payload.get('duration_ms')
            dur = f' {duration / 1000:.1f}s' if duration else ''
            header = Text(f'▸ {name} {summary}{dur} [{state}]',
                          style='bold #d78757')
            output = str(payload.get('output', ''))
            lines = output.splitlines()
            if not payload.get('expanded') and len(lines) > TOOL_PREVIEW_LINES:
                hidden = len(lines) - TOOL_PREVIEW_LINES
                lines = lines[:TOOL_PREVIEW_LINES] + [f'… {hidden} more lines']
            body = Text('\n'.join(lines), style='#bcbcbc')
            return _render_lines(Text('\n').join([header, body]), width)
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

    def submit_text(self, text: str) -> None:
        text = (text or '').strip()
        if not text or self._exiting:
            return
        self.model.append('user', {'text': text})
        self._mark_dirty()
        self._input_q.put(text)
        if not self._busy:
            self._start_worker()

    def _start_worker(self) -> None:
        self._busy = True
        self._refresh_footer()
        threading.Thread(target=self._run_agent, daemon=True).start()

    def _run_agent(self) -> None:
        import harness
        import turn_control
        try:
            while not self._exiting:
                text = self._input_q.get()
                if text is None:
                    break
                self._messages.append({'role': 'user', 'content': text})
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
                self._busy = False
                self.call_from_thread(self._refresh_footer)
                if self._input_q.empty():
                    break
        finally:
            self._busy = False


def run(messages: list | None = None, session_id: str | None = None,
        title: str = '') -> None:
    HarnessApp(messages or [], session_id=session_id, title=title).run()
