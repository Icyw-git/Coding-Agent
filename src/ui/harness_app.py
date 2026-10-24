"""新 TUI（P2/P3）：行模型 + 虚拟视口 + 浮层补全 + 队列条。

入口：交互模式默认由 `HARNESS_UI=textual` 启动（`textual-next` 为兼容别名）；
非 TTY 或 Textual 不可用时自动回退 ANSI。
"""
from __future__ import annotations

import contextlib
import io
import queue
import threading
import time

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static

from ui.background_actions import BackgroundActionsMixin
from ui.chrome.footer import FooterMixin
from ui.chrome.status import StatusMixin
from ui.command_actions import CommandActionsMixin
from ui.composer.suggestions import SuggestionsMixin
from ui.event_reduction import EventReductionMixin
from ui.queue_actions import QueueActionsMixin
from ui.renderers.tools import TOOL_MORE_LINES, TOOL_PREVIEW_LINES, tool_view
from ui.rendering import _render_lines, _render_markdown, _streaming_markdown_source
from ui.session_actions import SessionActionsMixin
from ui.session_admin_actions import SessionAdminActionsMixin
from ui.transcript import ConversationModel, TranscriptLayout
from ui.widgets import CompletionPopup, Composer, PendingQueue, Transcript
from ui_events import bus

FOLLOW_END = True
# 流式 Markdown 每次都要重新解析累计全文。根据上一帧耗时动态选择下一帧
# 间隔：短文本更快出字，长文本自动退让，避免固定 120ms 让小回复也显得迟滞。
STREAM_MIN_INTERVAL = 0.05
STREAM_MAX_INTERVAL = 0.30
STREAM_DEFAULT_INTERVAL = 0.12

class HarnessApp(SessionActionsMixin, SessionAdminActionsMixin,
                 CommandActionsMixin, EventReductionMixin, QueueActionsMixin,
                 SuggestionsMixin, BackgroundActionsMixin, FooterMixin,
                 StatusMixin, App):
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
