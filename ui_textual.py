"""Textual 渲染后端（UI 改进方案 P2）：Claude Code 风格全屏 TUI。

布局：Header(模型) + 对话流（assistant Markdown / 折叠工具块 / debug / diff）
     + 权限 Modal 覆盖层 + 底部 CC 风格状态行（thinking 提示 / 模型 / 工具数 / ctx）。

配色对齐 cc_ui.py 的 Claude Code Dark 色板：
  claude 橙 #d77757（工具标题） bash 粉边 #ecb7be（输出框） 权限蓝紫 #b1b9f9
  codeBg #3c3c42 主文本 #ffffff subtle #a7a3a2 inactive #6e6a6a

线程模型：Textual App 跑主线程，agent_loop 在后台线程（run_worker thread=True）；
bus 事件进线程安全队列，由 UI 事件循环里的消费者 worker 顺序处理（mount 是
异步操作必须 await；消费用 get_nowait + sleep 轮询，绝不同步阻塞事件循环）。
权限请求在 agent 线程挂起（PermissionGate.use_ui=True），Modal 在 UI 线程应答后唤醒。

启用：HARNESS_UI=textual 且 stdout 为 TTY（否则自动回退 ANSI）。
"""
import asyncio
import json
import queue
import time

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Collapsible, Input, Markdown, Static

from rich.markup import escape as markup_escape
from rich.text import Text
from ui_events import bus, UIEvent

PALETTE = {
    'background': '#191919',
    'surface': '#101010',
    'raised': '#232323',
    'text': '#e8e6e3',
    'muted': '#a7a3a2',
    'inactive': '#6e6a6a',
    'accent': '#d77757',
    'info': '#6ab0ff',
    'success': '#4eba65',
    'warning': '#ffc107',
    'danger': '#ff6b80',
    'permission': '#b1b9f9',
    'code': '#3c3c42',
}

CSS = """
Screen { background: $background; }
#content { height: 1fr; }
#chat { width: 1fr; }
#side-panel { width: 30; padding: 1 1; background: $surface; border-left: thick $inactive; }
#session-summary { color: $muted; }
#recent-tools { margin-top: 1; color: $muted; }

/* ---- 顶部状态条（CC：极简深色，无 Textual 默认 Header） ---- */
#header-line { height: 1; background: $surface; padding: 0 2; }

/* ---- 用户消息（CC：左侧橙竖条 + 深底，与 assistant 区分） ---- */
.user-msg { margin: 1 2; padding: 0 1;
            border-left: thick $accent; background: $raised; color: $text; }

/* ---- assistant 回复（流式 Static 增量；CC：正文色，无边框纯文本流） ---- */
.assistant-msg { margin: 1 2; color: $text; }
Markdown { margin: 1 2; background: $background; }
Markdown > .markdown-h1, .markdown-h2, .markdown-h3 { color: $text; }
Markdown > .markdown-code { background: $code; color: $warning; }
Markdown > .markdown-link { color: $info; }
Markdown > .markdown-blockquote { color: $muted; }

/* ---- 工具调用块（CC：✳ 橙标题，折叠成一行；输出粉色边框框） ---- */
Collapsible { margin: 1 2 0 2; background: $background; }
Collapsible > .collapsible--title { color: $accent; }
.tool-output { margin: 0 2 1 2; padding: 1 2;
               border-left: thick $inactive; background: $raised; color: $text; }
.tool-failed { border-left: thick $danger; color: $danger; }
Collapsible:focus { border-left: thick $info; }

/* ---- 过程信息 / diff 标题 ---- */
.debug-line { margin: 0 2; color: $inactive; }
.error-line { margin: 1 2; padding: 0 1; border-left: thick $danger; color: $danger; }
.diff-title { margin: 1 2 0 2; color: $muted; }

/* ---- 底部输入框 + 状态栏（dock 固定：输入在上，状态在下） ---- */
#bottom-bar { dock: bottom; height: 2; background: $surface; }
#input-line { height: 1; padding: 0 2; background: $surface; color: $text; border: none; }
#input-line:focus { border: none; }
#status-line { height: 1; background: $surface; color: $muted; padding: 0 2; }

/* ---- 权限 Modal（CC：蓝紫边框权限确认） ---- */
#perm-box { width: 76; padding: 2 3; border: round $permission; background: $raised; }
#perm-box.risk-warning { border: round $warning; }
#perm-box.risk-danger { border: round $danger; }
#perm-title { color: $warning; }

/* ---- 命令 / 搜索弹窗 ---- */
#command-box, #search-box { width: 66; padding: 1 2; border: round $info; background: $raised; }
.modal-title { color: $text; }
.modal-hint { color: $muted; }
#command-input, #search-input { margin-top: 1; border: none; background: $surface; color: $text; }
.search-match { background: $raised; border-left: thick $info; }
"""

for _name, _color in PALETTE.items():
    CSS = CSS.replace(f'${_name}', _color)


class PermissionModal(ModalScreen):
    """权限确认覆盖层：y 允许 / n|Esc 拒绝。应答后 dismiss（gate 自动唤醒 agent 线程）。

    200ms 防误触：弹窗后 0.2s 内按键无效（对应 CC 的 anti-misclick 设计）。"""

    BINDINGS = [
        ("y", "approve", "Allow"),
        ("n", "reject", "Deny"),
        ("escape", "reject", "Deny"),
    ]

    ARM_DELAY = 0.2

    def __init__(self, req_id: str, tool: str, args: dict, title: str = 'Permission needed',
                 risk: str = 'warning'):
        super().__init__()
        self.req_id = req_id
        self.tool = tool
        self.args = args
        self.title = title
        self.risk = risk if risk in ('warning', 'danger') else 'warning'
        self._armed = False

    def on_mount(self) -> None:
        self.set_timer(self.ARM_DELAY, self._arm)

    def _arm(self) -> None:
        self._armed = True

    def compose(self) -> ComposeResult:
        with Static(id="perm-box", classes=f"risk-{self.risk}"):
            label = 'HIGH RISK' if self.risk == 'danger' else 'CONFIRM ACTION'
            title_color = 'red' if self.risk == 'danger' else 'yellow'
            yield Static(f"[{title_color}][bold]{label}[/]  {markup_escape(self.title)}[/]",
                         id="perm-title")
            yield Static(Text("Tool: ", style="dim") + Text(self.tool))
            yield Static(Text("Target: ", style="dim") + Text(self._target_text()))
            yield Static("")
            yield Static("[yellow]y[/] = Allow    [yellow]n[/]/[yellow]Esc[/] = Deny    "
                         f"([dim]{int(self.ARM_DELAY * 1000)}ms 防误触[/])")

    def _target_text(self) -> str:
        """Prefer the actionable command/path over a noisy raw argument dict."""
        for key in ('command', 'path', 'file_path', 'pattern'):
            if self.args.get(key):
                return str(self.args[key])
        return str(self.args)

    def action_approve(self):
        if not self._armed:
            return
        bus.emit('permission_resolve', req_id=self.req_id, approved=True)
        self.dismiss()

    def action_reject(self):
        if not self._armed:
            return
        bus.emit('permission_resolve', req_id=self.req_id, approved=False)
        self.dismiss()


class CommandModal(ModalScreen):
    """轻量命令面板：输入命令并交给主应用执行。"""

    BINDINGS = [("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id='command-box'):
            yield Static('Command', classes='modal-title')
            yield Static('clear  export  sessions  resume <n>  search <text>  failures  all  help', classes='modal-hint')
            yield Input(placeholder='Type a command', id='command-input')

    def on_mount(self) -> None:
        self.query_one('#command-input', Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        command = (event.value or '').strip()
        if command:
            self.app.execute_command(command)
        self.dismiss()

    def action_dismiss(self) -> None:
        self.dismiss()


class SearchModal(ModalScreen):
    """搜索当前可见会话与工具输出，并定位第一条结果。"""

    BINDINGS = [("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id='search-box'):
            yield Static('Search conversation', classes='modal-title')
            yield Input(placeholder='Search visible messages and tool output', id='search-input')

    def on_mount(self) -> None:
        self.query_one('#search-input', Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = (event.value or '').strip()
        if query:
            self.app.execute_command(f'search {query}')
        self.dismiss()

    def action_dismiss(self) -> None:
        self.dismiss()


class CCApp(App):
    TITLE = "harness"
    CSS = CSS

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+d", "quit", "Quit"),
        ("ctrl+p", "commands", "Commands"),
        ("ctrl+f", "search", "Search"),
        ("ctrl+shift+f", "failures", "Failed tools"),
        ("ctrl+l", "clear", "Clear"),
        ("ctrl+e", "export", "Export"),
    ]

    def __init__(self, msgs: list):
        super().__init__()
        self._msgs = msgs
        self._chat = None
        self._user_input_q: queue.Queue = queue.Queue()   # 用户输入 → agent 线程（多轮）
        self._exiting = False
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Static('', id="header-line")
        with Horizontal(id='content'):
            yield VerticalScroll(id="chat")
            with Vertical(id='side-panel'):
                yield Static('', id='session-summary')
                yield Static('', id='recent-tools')
        with Vertical(id="bottom-bar"):
            yield Input(placeholder="输入消息，Enter 发送 · Ctrl+C 退出", id="input-line")
            yield Static('', id="status-line")

    def on_mount(self) -> None:
        import os
        import harness as h
        from ui_events import clear_cancel
        clear_cancel()
        bus.subscribe(self._on_event)
        header = self.query_one('#header-line', Static)
        header.update(f"[{PALETTE['accent']}]harness[/] [{PALETTE['muted']}]{os.getenv('LLM_MODEL_ID', '')}[/]"
                      f" [dim]{markup_escape(str(h.WORKDIR))}[/]")
        self._chat = self.query_one('#chat', VerticalScroll)
        self._side_panel = self.query_one('#side-panel', Vertical)
        self._session_summary = self.query_one('#session-summary', Static)
        self._recent_tools_widget = self.query_one('#recent-tools', Static)
        self._input = self.query_one('#input-line', Input)
        self._event_queue: queue.Queue = queue.Queue()
        self._active_tools: list = []   # 执行中的工具块（FIFO 匹配 tool_end）
        self._last_status: dict = {}    # 最后已知状态指标（thinking/idle 时状态栏常驻显示）
        self._active_widgets: dict = {}   # mid -> 流式中的 assistant Static（增量追加）
        self._assistant_buffers: dict = {}   # mid -> 已渲染文本前缀（Static 不方便读回，自维护）
        self._flushed: dict = {}          # mid -> 已推送到 widget 的文本（帧刷新去重）
        self._streaming = False           # 正在流式输出（状态栏显示 spinner + 耗时）
        self._stream_start = 0.0
        self._stream_chars = 0
        self._activity_state = 'ready'
        self._activity_detail = ''
        self._recent_tools = []
        self._round_metrics = []
        self._background_tasks = {}
        self._tool_filter = 'all'
        self._status_refresh_pending = False
        self._search_matches = []
        self._side_panel.display = self.size.width >= 110
        self._refresh_sidebar()
        # 帧刷新：网络 chunk 到达频率与 UI 渲染频率解耦 → 无论 chunk 多大都平滑打字机
        self.set_interval(0.05, self._flush_stream_buffers)
        self.set_interval(0.1, self._stream_tick)
        self.set_interval(0.1, self._flush_status)
        # 初始用户输入先入队（在工具/回复之前渲染，保持对话顺序）
        for m in self._msgs:
            if isinstance(m, dict):
                self._event_queue.put(UIEvent('history_message', {'message': m}))
        # 单一消费者 worker：严格 FIFO 处理事件（mount 是异步操作，必须 await）
        self.run_worker(self._consume_events, thread=False)
        self.run_worker(self._run_agent, thread=True)
        self._input.focus()

    def on_unmount(self) -> None:
        """窗口被外部关闭时同样回收事件订阅与待等待的业务线程。"""
        self._exiting = True
        bus.unsubscribe(self._on_event)
        try:
            import harness
            harness.permission_gate.cancel_pending()
        except Exception:
            pass
        self._user_input_q.put(None)

    def on_resize(self, event) -> None:
        """窄终端专注会话，宽终端补充执行上下文。"""
        if not hasattr(self, '_side_panel'):
            return
        self._side_panel.display = event.size.width >= 110
        self._refresh_status_line()
        self._refresh_sidebar()

    # ---------- 底部输入框：多轮对话 ----------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter 提交：文本进队列交给 agent 线程，同时渲染为 user 消息。"""
        if self._busy or self._exiting:
            return
        text = (event.value or '').strip()
        self._input.value = ''
        if not text:
            return
        self._set_input_busy(True)
        self._event_queue.put(UIEvent('user_message', {'text': text[:2000]}))
        self._user_input_q.put(text)

    def action_quit(self) -> None:
        """Ctrl+C / Ctrl+D：拒绝待审批并唤醒 agent 线程退出。"""
        self._exiting = True
        from ui_events import request_cancel
        request_cancel()
        try:
            import harness
            harness.permission_gate.cancel_pending()
        except Exception:
            pass
        try:
            self._user_input_q.put(None)
        except Exception:
            pass

    def action_commands(self) -> None:
        if not self._exiting:
            self.push_screen(CommandModal())

    def action_search(self) -> None:
        if not self._exiting:
            self.push_screen(SearchModal())

    def action_clear(self) -> None:
        self.execute_command('clear')

    def action_export(self) -> None:
        self.execute_command('export')

    def action_failures(self) -> None:
        self.execute_command('failures')

    def execute_command(self, command: str) -> None:
        """从快捷键或命令面板进入同一执行路径。"""
        asyncio.create_task(self._execute_command(command))

    async def _execute_command(self, command: str) -> None:
        raw = command.strip()
        verb, _, argument = raw.partition(' ')
        verb = verb.lower()
        argument = argument.strip()
        if verb in ('help', '?'):
            await self._show_notice('Commands: clear | export | sessions | resume <n> | allow <tool> | deny <tool> | ask <tool> | search <text> | failures | all | help')
        elif verb == 'clear':
            await self._clear_conversation()
        elif verb == 'export':
            await self._export_conversation()
        elif verb in ('sessions', 'transcripts'):
            await self._list_sessions()
        elif verb == 'resume':
            await self._resume_session(argument)
        elif verb in ('allow', 'deny', 'ask'):
            await self._set_permission_policy(verb, argument)
        elif verb == 'search':
            await self._search_conversation(argument)
        elif verb in ('failures', 'failed'):
            await self._filter_tools('failed')
        elif verb in ('all', 'show-all'):
            await self._filter_tools('all')
        elif raw:
            await self._show_notice(f'Unknown command: {raw}')

    async def _clear_conversation(self) -> None:
        """仅在空闲时清除本轮会话历史，避免破坏正在执行的请求。"""
        if self._busy:
            await self._show_notice('Cannot clear while a request is running.')
            return
        self._msgs.clear()
        self._active_tools.clear()
        self._active_widgets.clear()
        self._assistant_buffers.clear()
        self._flushed.clear()
        self._recent_tools.clear()
        self._tool_filter = 'all'
        self._clear_search_matches()
        await self._chat.remove_children()
        self._refresh_sidebar()
        await self._show_notice('Conversation cleared.')

    async def _export_conversation(self) -> None:
        """导出当前 messages 的 Markdown 副本，不影响会话或 agent 流程。"""
        import harness
        try:
            path = harness.export_session(self._msgs)
        except Exception as exc:
            await self._show_notice(f'Export failed: {type(exc).__name__}: {exc}')
            return
        await self._show_notice(f'Exported Markdown: {path}')

    async def _list_sessions(self) -> None:
        import harness
        sessions = harness.list_transcripts()
        if not sessions:
            await self._show_notice('No saved sessions.')
            return
        lines = ['Saved sessions:']
        for index, session in enumerate(sessions, 1):
            lines.append(f"{index}. {session['name']}")
        await self._show_notice('\n'.join(lines))

    async def _set_permission_policy(self, policy: str, tool: str) -> None:
        if not tool:
            await self._show_notice(f'Use {policy} <tool>.')
            return
        import harness
        harness.permission_gate.set_policy(tool, policy)
        await self._show_notice(f'Permission policy for {tool}: {policy}.')

    async def _resume_session(self, argument: str) -> None:
        if self._busy:
            await self._show_notice('Cannot resume while a request is running.')
            return
        import harness
        sessions = harness.list_transcripts()
        if not argument:
            await self._show_notice('Use resume <number> from sessions.')
            return
        try:
            index = int(argument)
            path = sessions[index - 1]['path']
            messages = harness.load_transcript(path)
        except (ValueError, IndexError, json.JSONDecodeError) as exc:
            await self._show_notice(f'Unable to resume session: {exc}')
            return
        self._msgs[:] = messages
        await self._clear_rendered_conversation()
        for message in messages:
            await self._render_history_message(message)
        await self._show_notice(f'Resumed {sessions[index - 1]["name"]}.')

    async def _clear_rendered_conversation(self) -> None:
        self._active_tools.clear()
        self._active_widgets.clear()
        self._assistant_buffers.clear()
        self._flushed.clear()
        self._recent_tools.clear()
        self._clear_search_matches()
        await self._chat.remove_children()
        self._refresh_sidebar()

    async def _search_conversation(self, query: str) -> None:
        """搜索当前渲染的会话内容，并定位第一条匹配消息。"""
        self._clear_search_matches()
        if not query:
            await self._show_notice('Enter text after search.')
            return
        needle = query.casefold()
        for widget in self._chat.children:
            if needle in self._widget_text(widget).casefold():
                widget.add_class('search-match')
                self._search_matches.append(widget)
        if self._search_matches:
            self._chat.scroll_to(self._search_matches[0], animate=False)
            await self._show_notice(f'Found {len(self._search_matches)} match(es) for: {query}')
        else:
            await self._show_notice(f'No visible matches for: {query}')

    async def _filter_tools(self, mode: str) -> None:
        """Keep failure investigation in the UI layer without discarding event history."""
        self._tool_filter = mode
        shown = 0
        for widget in self._chat.query('Collapsible'):
            if not widget.has_class('tool-call'):
                continue
            visible = mode == 'all' or widget.has_class('tool-failed-call')
            widget.display = visible
            shown += int(visible)
        label = 'Showing failed tools only.' if mode == 'failed' else 'Showing all tool output.'
        self._refresh_sidebar()
        await self._show_notice(f'{label} ({shown} visible)')

    def _clear_search_matches(self) -> None:
        for widget in self._search_matches:
            widget.remove_class('search-match')
        self._search_matches = []

    @staticmethod
    def _widget_text(widget) -> str:
        """递归收集 widget 的已渲染内容，供会话与工具输出搜索。"""
        parts = [str(widget.render())]
        for child in widget.children:
            parts.append(CCApp._widget_text(child))
        return '\n'.join(parts)

    async def _show_notice(self, message: str) -> None:
        await self._chat.mount(Static(Text(message), classes='debug-line'))
        self._chat.scroll_end(animate=False)

    async def _show_session_summary(self, summary: dict) -> None:
        outcomes = summary.get('tool_outcomes', {})
        failed = outcomes.get('failed', 0) + outcomes.get('denied', 0)
        source = summary.get('cost_source', 'estimated')
        prefix = '' if source in ('confirmed', 'provider_reported') else '~'
        try:
            cost = float(summary.get('cost', 0))
        except (TypeError, ValueError):
            cost = 0.0
        text = (f"Session complete  ·  {summary.get('rounds', 0)} rounds  ·  "
                f"{summary.get('tools', 0)} tools ({failed} failed/denied)  ·  "
                f"{summary.get('tokens', 0)} tokens  ·  {prefix}¥{cost:.4f}")
        await self._chat.mount(Static(Text(text), classes='session-summary-line'))

    def _set_input_busy(self, busy: bool) -> None:
        """只允许一条在途请求，保证聊天记录与实际执行顺序一致。"""
        self._busy = busy
        if not hasattr(self, '_input'):
            return
        self._input.disabled = busy
        self._input.placeholder = ('正在处理当前消息...' if busy
                                   else '输入消息，Enter 发送 · Ctrl+C 退出')
        if not busy and not self._exiting:
            self._input.focus()

    async def _consume_events(self) -> None:
        while True:
            try:
                e = self._event_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.02)   # 让出事件循环：绝不同步阻塞（get() 会冻结 UI）
                continue
            if e is None:            # agent 结束 sentinel：退出消费者，避免挂起
                self._event_queue.task_done()
                break
            try:
                await self._apply(e)
            finally:
                self._event_queue.task_done()

    # ---------- agent 线程 ----------

    def _run_agent(self) -> None:
        import time
        import harness
        try:
            # 多轮对话：空闲时不占 agent_lock，让 cron 队列能主动执行。
            while not self._exiting:
                text = self._user_input_q.get()   # 阻塞等用户输入（None = 退出 sentinel）
                if text is None:
                    break
                self._msgs.append({'role': 'user', 'content': text})
                try:
                    with harness.agent_lock:
                        harness.permission_gate.use_ui = True
                        harness.agent_loop(self._msgs)
                except Exception as exc:
                    bus.emit('activity', state='failed', detail='Request failed')
                    bus.emit('error', msg=f'{type(exc).__name__}: {exc}')
                finally:
                    if not self._exiting:
                        self.call_from_thread(self._set_input_busy, False)
        finally:
            harness.permission_gate.cancel_pending()
            harness.permission_gate.use_ui = False
            self._event_queue.put(None)   # 通知消费者退出
            # 等剩余事件渲染完再退出（带超时兜底，避免消费者异常时永久挂起）
            deadline = time.monotonic() + 10
            while self._event_queue.unfinished_tasks > 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.call_from_thread(self.exit)

    # ---------- 事件流：bus 回调（任意线程）→ 队列 → UI 线程顺序应用 ----------

    def _on_event(self, e):
        self._event_queue.put(e)   # 线程安全；由 _consume_events worker 顺序处理

    async def _apply(self, e):
        if e.type == 'user_message':
            # 用户输入可能含方括号 → 用 Text 对象（不做 markup 解析，避免 MarkupError）
            await self._chat.mount(Static(Text(f"\u276f {e.payload.get('text', '')}"),
                                          classes='user-msg'))
        elif e.type == 'history_message':
            await self._render_history_message(e.payload.get('message', {}))
        elif e.type == 'assistant_text':
            # 流式打字机：先挂空 Markdown，再由独立 task 分块增量渲染（CC 流式渲染轻量版）
            md = Markdown('')
            await self._chat.mount(md)
            asyncio.create_task(self._stream_typing(md, e.payload.get('text', '')))
        elif e.type == 'assistant_start':
            # 真流式：新消息挂空 Static，delta 由帧刷新逐帧填充（打字机效果）
            mid = e.payload['mid']
            w = Static(Text(''), classes='assistant-msg')
            self._active_widgets[mid] = w
            self._assistant_buffers[mid] = ''
            self._flushed[mid] = ''
            self._streaming = True
            self._stream_start = time.monotonic()
            self._stream_chars = 0
            await self._chat.mount(w)
        elif e.type == 'assistant_delta':
            # 只累加缓冲 + 计数；实际渲染交给 _flush_stream_buffers（50ms 帧）
            mid = e.payload['mid']
            delta = e.payload.get('delta', '')
            self._assistant_buffers[mid] = self._assistant_buffers.get(mid, '') + delta
            self._stream_chars += len(delta)
            # Generating is meaningful only after visible model output starts.
            self._activity_state = 'generating'
            self._activity_detail = 'Streaming response'
            self._refresh_status_line()
        elif e.type == 'assistant_end':
            mid = e.payload['mid']
            w = self._active_widgets.pop(mid, None)
            buf = self._assistant_buffers.pop(mid, None) or ''
            self._flushed.pop(mid, None)
            if w is not None and w.is_mounted and buf.strip():
                # 流式收尾：纯文本 Static 换成 Markdown —— 代码块/加粗/列表最终排版精致化
                md = Markdown(buf)
                await self._chat.mount(md, before=w)
                await w.remove()
            self._streaming = False
            self._update_status(self._last_status)   # 恢复常驻状态行（pills）
        elif e.type == 'tool_start':
            # 工具实时可见：执行中即展开显示（✳ 橙标题），完成后折叠
            name = e.payload.get('name', 'tool')
            args = e.payload.get('args', {})
            col = Collapsible(title=self._tool_title('running', name, args=args), collapsed=False)
            col.add_class('tool-call')
            await self._chat.mount(col)
            self._active_tools.append(col)   # 工具串行执行，FIFO 匹配 tool_end
            self._recent_tools.insert(0, {'name': name, 'state': 'running', 'duration_ms': None})
            self._recent_tools = self._recent_tools[:5]
            self._refresh_sidebar()
        elif e.type == 'tool_end':
            out = str(e.payload.get('output_preview', ''))[:2000]
            if self._active_tools:
                col = self._active_tools.pop(0)
                name = e.payload.get('name', 'tool')
                ok = bool(e.payload.get('ok', True))
                background = bool(e.payload.get('background', False))
                outcome = e.payload.get('outcome')
                state = outcome or ('queued' if background else 'complete' if ok else 'failed')
                if state == 'completed':
                    state = 'complete'
                col.title = self._tool_title(
                    state, name, duration_ms=e.payload.get('duration_ms', 0))
                output_len = int(e.payload.get('output_len', len(out)))
                if output_len > len(out):
                    out += f'\n\n... output truncated ({output_len:,} characters total)'
                classes = 'tool-output' if ok else 'tool-output tool-failed'
                await col.mount(Static(Text(out or '[No output]'), classes=classes))
                if state in ('failed', 'denied'):
                    col.add_class('tool-failed-call')
                col.display = self._tool_filter == 'all' or state in ('failed', 'denied')
                # 失败结果保留展开状态，成功结果收起以便扫描长会话。
                col.collapsed = ok
                for tool in self._recent_tools:
                    if tool['name'] == name and tool['state'] == 'running':
                        tool.update(state=state, duration_ms=e.payload.get('duration_ms', 0))
                        break
                self._activity_state = 'tool_complete' if ok else 'failed'
                self._activity_detail = 'Tool result ready'
                self._refresh_status_line()
                self._refresh_sidebar()
        elif e.type == 'debug':
            await self._chat.mount(Static(Text(self._strip_ansi(e.payload.get('msg', ''))),
                                          classes='debug-line'))
        elif e.type == 'error':
            await self._chat.mount(Static(Text(f"Error: {e.payload.get('msg', '')}"),
                                          classes='error-line'))
        elif e.type == 'activity':
            state = e.payload.get('state', 'ready')
            detail = e.payload.get('detail', '')
            # Internal bookkeeping should not overwrite a visible tool result
            # or claim that generation started before the first delta arrives.
            if state == 'thinking' and detail == 'Reviewing tool result':
                return
            if state == 'thinking' and detail == 'Requesting model response':
                state = 'waiting'
                detail = 'Waiting for response'
            self._activity_state = state
            self._activity_detail = detail
            self._refresh_status_line()
            self._refresh_sidebar()
        elif e.type == 'diff':
            path = e.payload.get('path', '')
            if path:
                await self._chat.mount(Static(Text(path), classes='diff-title'))
            await self._chat.mount(Static(self._render_diff(e.payload.get('diff_text', '')),
                                          classes='tool-output'))
        elif e.type == 'permission_request':
            risk = self._permission_risk(e.payload.get('tool', ''), e.payload.get('args', {}),
                                         e.payload.get('title', ''))
            self.push_screen(PermissionModal(
                e.payload['req_id'], e.payload['tool'], e.payload['args'],
                e.payload.get('title', 'Permission needed'), risk=risk))
        elif e.type == 'permission_resolve':
            self._input.focus()   # 弹窗应答后焦点回到输入框
        elif e.type == 'status':
            self._update_status(e.payload)
        elif e.type == 'round_end':
            metric = e.payload
            self._round_metrics.append(dict(metric))
            self._round_metrics = self._round_metrics[-10:]
            source = metric.get('source', 'estimated')
            marker = '' if source in ('confirmed', 'provider_reported') else '~'
            compressed = ' · compressed' if metric.get('compressed') else ''
            await self._show_notice(
                f"Round {metric.get('round', 0)} · {metric.get('duration_ms', 0)}ms · "
                f"{metric.get('prompt_tokens', 0)} in / {metric.get('completion_tokens', 0)} out · "
                f"{marker}¥{float(metric.get('cost', 0)):.4f}{compressed}")
        elif e.type == 'session_end':
            await self._show_session_summary(e.payload.get('summary', {}))
        elif e.type == 'background_start':
            task_id = e.payload.get('task_id', '')
            self._background_tasks[task_id] = 'running'
            await self._show_notice(f'Background task started: {task_id}')
        elif e.type == 'background_end':
            task_id = e.payload.get('task_id', '')
            state = 'completed' if e.payload.get('ok') else 'failed'
            self._background_tasks[task_id] = state
            await self._show_notice(f'Background task {state}: {task_id}')
        self._chat.scroll_end(animate=False)   # scroll_end 是同步方法（返回 None）

    async def _render_history_message(self, message: dict) -> None:
        role = message.get('role')
        content = message.get('content') or ''
        if role == 'user':
            await self._chat.mount(Static(Text(f"❯ {content}"), classes='user-msg'))
        elif role == 'assistant':
            await self._chat.mount(Markdown(str(content)))
        elif role == 'tool':
            await self._chat.mount(Static(Text(str(content)), classes='tool-output'))

    async def _stream_typing(self, md: Markdown, text: str) -> None:
        """打字机效果：分块增量渲染 Markdown（约 80 步完成，模拟流式）。"""
        if not text:
            return
        step = max(1, len(text) // 80)
        for i in range(0, len(text), step):
            await md.update(text[:i + step])   # Markdown.update 是异步的（AwaitComplete）
            await asyncio.sleep(0.02)

    @staticmethod
    def _short_args(args: dict, limit: int = 72) -> str:
        """工具参数压成单行摘要，避免工具标题挤占会话宽度。"""
        compact = ' '.join(str(args).split())
        return compact if len(compact) <= limit else compact[:limit - 3] + '...'

    @staticmethod
    def _permission_risk(tool: str, args: dict, title: str = '') -> str:
        """Classify only the presentation; the actual permission decision stays in hooks."""
        text = (str(tool) + ' ' + str(title) + ' ' + str(args)).lower()
        if any(word in text for word in ('outside workspace', 'delete', 'remove', 'sudo', 'chmod')):
            return 'danger'
        if tool.lower() in ('bash', 'run_command', 'shell', 'write_file', 'edit_file'):
            return 'danger' if tool.lower() in ('bash', 'run_command', 'shell') else 'warning'
        return 'warning'

    @staticmethod
    def _tool_title(state: str, name: str, args=None, duration_ms=None) -> Text:
        """用统一的状态语义渲染工具标题，结果默认可折叠扫描。"""
        icon, label, color = {
            'running': ('*', 'Running', PALETTE['accent']),
            'complete': ('+', 'Done', PALETTE['success']),
            'queued': ('~', 'Queued', PALETTE['info']),
            'failed': ('!', 'Failed', PALETTE['danger']),
            'denied': ('x', 'Denied', PALETTE['danger']),
            'warning': ('!', 'Warning', PALETTE['warning']),
        }.get(state, ('*', state.title(), PALETTE['muted']))
        title = Text()
        title.append(f'{icon} {label} ', style=f'bold {color}')
        title.append(name, style='bold')
        if args:
            title.append(f'  {CCApp._short_args(args)}', style=PALETTE['muted'])
        if duration_ms is not None:
            title.append(f'  {duration_ms / 1000:.1f}s', style=PALETTE['muted'])
        return title

    @staticmethod
    def _strip_ansi(text: str) -> str:
        """剥掉 ANSI 转义（hooks 的 print 收敛为 debug 事件后仍带颜色码）。"""
        import re
        return re.sub(r'\x1b\[[0-9;]*m', '', str(text))

    @staticmethod
    def _render_diff(diff_text: str) -> Text:
        """diff 文本转 rich Text：+ 绿 / - 红 / @@ 灰（对应 CC diff 配色）。

        Text 对象不做 markup 解析（diff 行可能含方括号，字符串 markup 会抛 MarkupError）。"""
        text = Text()
        for ln in str(diff_text).splitlines():
            if ln.startswith('+'):
                text.append(ln + '\n', style=PALETTE['success'])
            elif ln.startswith('-'):
                text.append(ln + '\n', style=PALETTE['danger'])
            elif ln.startswith('@@'):
                text.append(ln + '\n', style=PALETTE['muted'])
            else:
                text.append(ln + '\n')
        return text

    # ---------- 帧刷新：流式渲染平滑化 ----------

    def _flush_stream_buffers(self) -> None:
        """50ms 帧：把流式缓冲批量推送到 widget（只在文本变化时更新）。

        网络 chunk 可能一次来几百字符；按固定帧渲染让显示节奏平滑，
        不会"一块一块"跳变。"""
        if not self._active_widgets:
            return
        for mid, w in list(self._active_widgets.items()):
            buf = self._assistant_buffers.get(mid, '')
            if self._flushed.get(mid) != buf:
                w.update(Text(buf))   # Text 对象不做 markup 解析（含方括号也不崩）
                self._flushed[mid] = buf

    _SPINNERS = ('\u28a7', '\u28bd', '\u28bb', '\u28bf', '\u28b7', '\u28af', '\u28ab', '\u28a3')

    def _status_line(self):
        """#status-line 的防御性查询：应用关闭窗口期 widget 可能已销毁。"""
        try:
            return self.query_one('#status-line', Static)
        except Exception:
            return None

    def _stream_tick(self) -> None:
        """100ms tick：流式输出时状态栏显示 spinner + 已耗时 + 已生成字符数。"""
        if not self._streaming:
            return
        elapsed = time.monotonic() - self._stream_start
        spin = self._SPINNERS[int(time.monotonic() * 6) % len(self._SPINNERS)]
        base = self._pills(self._last_status)
        line = self._status_line()
        if line is None:
            return
        line.update(f'{spin} [bold {PALETTE["accent"]}]Generating[/] {elapsed:4.1f}s'
                    f' [dim]{self._stream_chars} chars[/]'
                    f'{("  " + base) if base else ""}')

    def _update_status(self, payload: dict) -> None:
        """更新指标后重绘状态栏；活动态由 activity 事件统一驱动。"""
        if any(key in payload for key in (
                'model', 'tools', 'ctx_pct', 'ctx_tokens', 'tokens', 'cost',
                'cost_source', 'ctx_source', 'recent_calls')):
            self._last_status.update(payload)
        self._status_refresh_pending = True

    def _flush_status(self) -> None:
        if not self._status_refresh_pending:
            return
        self._status_refresh_pending = False
        self._refresh_status_line()
        self._refresh_sidebar()

    def _refresh_sidebar(self) -> None:
        """宽屏侧栏只呈现当前会话的高价值摘要，不重复完整日志。"""
        if not hasattr(self, '_session_summary'):
            return
        labels = {
            'ready': 'Ready', 'preparing': 'Preparing', 'thinking': 'Thinking',
            'generating': 'Generating', 'waiting': 'Waiting for response',
            'tool': 'Running tool', 'tool_complete': 'Tool complete',
            'awaiting_permission': 'Awaiting approval', 'saving': 'Saving',
            'complete': 'Complete', 'failed': 'Failed', 'idle': 'Idle',
            'cancelled': 'Cancelled',
        }
        colors = {
            'ready': PALETTE['muted'], 'preparing': PALETTE['info'],
            'thinking': PALETTE['warning'], 'waiting': PALETTE['info'],
            'generating': PALETTE['accent'],
            'tool': PALETTE['accent'], 'awaiting_permission': PALETTE['permission'],
            'tool_complete': PALETTE['success'],
            'saving': PALETTE['info'], 'complete': PALETTE['success'],
            'failed': PALETTE['danger'], 'idle': PALETTE['muted'],
            'cancelled': PALETTE['warning'],
        }
        state = self._activity_state
        summary = Text()
        summary.append('SESSION\n', style=f'bold {PALETTE["accent"]}')
        summary.append(labels.get(state, state.title()) + '\n', style=f'bold {colors.get(state, PALETTE["muted"])}')
        if self._activity_detail:
            summary.append(str(self._activity_detail)[:48] + '\n', style=PALETTE['muted'])
        if self._last_status.get('ctx_pct') is not None:
            ctx_source = self._last_status.get('ctx_source', 'estimated')
            marker = '' if ctx_source == 'confirmed' else '~'
            ctx_tokens = self._last_status.get('ctx_tokens')
            ctx_window = self._last_status.get('ctx_window')
            summary.append(f"Context  {marker}{int(self._last_status['ctx_pct'])}%\n")
            if ctx_tokens is not None and ctx_window:
                summary.append(f"         {ctx_tokens} / {ctx_window}\n", style=PALETTE['muted'])
        summary.append(f"Tools    {int(self._last_status.get('tools', 0))}\n")
        failed_tools = sum(1 for tool in self._recent_tools if tool['state'] == 'failed')
        if failed_tools:
            summary.append(f"Failed   {failed_tools}\n", style=PALETTE['danger'])
        if self._tool_filter == 'failed':
            summary.append('Filter   failures only\n', style=PALETTE['warning'])
        if self._last_status.get('tokens') is not None:
            tokens = self._last_status.get('tokens', 0)
            prompt = self._last_status.get('prompt_tokens', 0)
            completion = self._last_status.get('completion_tokens', 0)
            cached = self._last_status.get('cached_tokens', 0)
            cache_creation = self._last_status.get('cache_creation_tokens', 0)
            reasoning = self._last_status.get('reasoning_tokens', 0)
            summary.append(f"Tokens   {tokens}\n")
            summary.append(f"Input    {prompt}\n")
            summary.append(f"Output   {completion}\n")
            if cached or cache_creation or reasoning:
                summary.append(f"Cache    {cached}\n")
                if cache_creation:
                    summary.append(f"Cache new {cache_creation}\n")
                summary.append(f"Reason   {reasoning}\n")
        if self._last_status.get('cost') is not None:
            source = self._last_status.get('cost_source', 'estimated')
            summary.append(f"Cost     {self._format_cost(self._last_status['cost'], source)}\n")
            summary.append(f"Billing  {self._source_label(source)}\n")
        if self._last_status.get('calls'):
            summary.append(f"Calls    {int(self._last_status['calls'])}\n")
        recent_calls = self._last_status.get('recent_calls', [])
        if recent_calls:
            summary.append('RECENT CALLS\n', style=f'bold {PALETTE["accent"]}')
            for call in recent_calls:
                marker = '' if call.get('source') in ('confirmed', 'provider_reported') else '~'
                try:
                    cost = float(call.get('cost', 0))
                except (TypeError, ValueError):
                    cost = 0.0
                summary.append(f"{str(call.get('model', ''))[:18]}  {marker}¥{cost:.4f}\n",
                               style=PALETTE['muted'])
        if self._round_metrics:
            summary.append('RECENT ROUNDS\n', style=f'bold {PALETTE["accent"]}')
            for metric in self._round_metrics[-5:]:
                compressed = ' *' if metric.get('compressed') else ''
                summary.append(
                    f"#{metric.get('round', 0)} {metric.get('duration_ms', 0)}ms "
                    f"{metric.get('prompt_tokens', 0)}/{metric.get('completion_tokens', 0)}"
                    f"{compressed}\n", style=PALETTE['muted'])
        if self._background_tasks:
            summary.append('BACKGROUND\n', style=f'bold {PALETTE["accent"]}')
            for task_id, state in list(self._background_tasks.items())[-5:]:
                summary.append(f'{task_id}  {state}\n', style=PALETTE['muted'])
        self._session_summary.update(summary)

        recent = Text()
        recent.append('RECENT TOOLS\n', style=f'bold {PALETTE["accent"]}')
        if not self._recent_tools:
            recent.append('No tool activity yet.', style=PALETTE['inactive'])
        for tool in self._recent_tools:
            state = tool['state']
            marker, color = {
                'running': ('*', PALETTE['accent']),
                'complete': ('+', PALETTE['success']),
                'queued': ('~', PALETTE['info']),
                'failed': ('!', PALETTE['danger']),
            }.get(state, ('*', PALETTE['muted']))
            recent.append(f'{marker} ', style=color)
            recent.append(tool['name'])
            if tool['duration_ms'] is not None:
                recent.append(f"  {tool['duration_ms'] / 1000:.1f}s", style=PALETTE['muted'])
            recent.append('\n')
        self._recent_tools_widget.update(recent)

    def _refresh_status_line(self) -> None:
        line = self._status_line()
        if line is None:
            return
        if self._streaming:
            return
        label, color = {
            'ready': ('Ready', PALETTE['muted']),
            'preparing': ('Preparing', PALETTE['info']),
            'thinking': ('Thinking', PALETTE['warning']),
            'generating': ('Generating', PALETTE['accent']),
            'waiting': ('Waiting for response', PALETTE['info']),
            'tool': ('Running tool', PALETTE['accent']),
            'tool_complete': ('Tool complete', PALETTE['success']),
            'awaiting_permission': ('Awaiting approval', PALETTE['permission']),
            'saving': ('Saving', PALETTE['info']),
            'complete': ('Complete', PALETTE['success']),
            'failed': ('Failed', PALETTE['danger']),
            'cancelled': ('Cancelled', PALETTE['warning']),
            'idle': ('Idle', PALETTE['muted']),
        }.get(self._activity_state, (self._activity_state.title(), PALETTE['muted']))
        detail = markup_escape(str(self._activity_detail))
        if self.size.width < 70:
            detail = ''
        elif len(detail) > 36:
            detail = detail[:33] + '...'
        base = self._pills(self._last_status)
        line.update(f'[bold {color}]{label}[/]'
                    f'{(" [dim]" + detail + "[/]") if detail else ""}'
                    f'{("  " + base) if base else ""}')

    def _pills(self, payload: dict) -> str:
        """按终端宽度保留从模型到成本的递减优先级指标。"""
        width = self.size.width
        parts = []
        if payload.get('model') and width >= 58:
            parts.append(f"[{PALETTE['accent']} on {PALETTE['raised']}] {markup_escape(str(payload['model']))} [/]")
        ctx = payload.get('ctx_pct')
        if ctx is not None and width >= 44:
            marker = '' if payload.get('ctx_source') == 'confirmed' else '~'
            parts.append(marker + CCApp._ctx_bar(int(ctx)))
        if payload.get('tools') is not None and width >= 68:
            parts.append(f"[dim]{int(payload['tools'])} tools[/]")
        if payload.get('tokens') and width >= 78:
            parts.append(f"[dim]{payload['tokens']} tok[/]")
        cost = payload.get('cost')
        if cost and width >= 96:
            parts.append(f"[dim]{self._format_cost(cost, payload.get('cost_source', 'estimated'))}[/]")
        return '  '.join(parts)

    @staticmethod
    def _format_cost(value, source: str = 'estimated') -> str:
        """Format Decimal/string costs without losing the confirmed/estimated distinction."""
        try:
            amount = float(value)
        except (TypeError, ValueError):
            amount = 0.0
        prefix = '' if source in ('confirmed', 'provider_reported') else '~'
        return f"{prefix}\u00a5{amount:.4f}"

    @staticmethod
    def _source_label(source: str) -> str:
        return {
            'confirmed': 'Confirmed',
            'provider_reported': 'Provider reported',
            'mixed': 'Mixed',
            'estimated': 'Estimated',
        }.get(source, 'Estimated')

    @staticmethod
    def _ctx_bar(pct: int) -> str:
        """上下文占用进度条：10 格，实心=已用 / 空心=剩余，附百分比。

        >75% 红 / >50% 黄 / 否则绿。占用随会话推进由 status 事件实时刷新。"""
        pct = max(0, min(100, pct))
        filled = round(pct / 10)
        bar = '\u2588' * filled + '\u2591' * (10 - filled)   # █ 已用 / ░ 剩余
        color = PALETTE['danger'] if pct > 75 else PALETTE['warning'] if pct > 50 else PALETTE['success']
        return f'[{color}]{bar}[/] [dim]{pct}%[/]'


def run(msgs: list) -> None:
    """以 Textual 模式启动会话（阻塞直到 agent_loop 结束）。

    调用方（harness __main__）已保证 stdout 为 TTY；此处只负责切换渲染后端。"""
    from renderer import default_renderer
    bus.unsubscribe(default_renderer.on_event)   # 避免 ANSI 后端双份渲染
    try:
        CCApp(msgs).run()
    finally:
        bus.subscribe(default_renderer.on_event)
