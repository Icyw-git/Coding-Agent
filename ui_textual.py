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
import queue
import time

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Collapsible, Input, Markdown, Static

from rich.markup import escape as markup_escape
from rich.text import Text
from ui_events import bus, UIEvent

CSS = """
Screen { background: #191919; }
#chat { height: 1fr; }

/* ---- 顶部状态条（CC：极简深色，无 Textual 默认 Header） ---- */
#header-line { height: 1; background: #101010; padding: 0 2; }

/* ---- 用户消息（CC：左侧橙竖条 + 深底，与 assistant 区分） ---- */
.user-msg { margin: 1 2; padding: 0 1;
            border-left: thick #d77757; background: #1f1f1f; color: #e8e6e3; }

/* ---- assistant 回复（流式 Static 增量；CC：正文色，无边框纯文本流） ---- */
.assistant-msg { margin: 1 2; color: #e8e6e3; }
Markdown { margin: 1 2; background: #191919; }
Markdown > .markdown-h1, .markdown-h2, .markdown-h3 { color: #ffffff; }
Markdown > .markdown-code { background: #3c3c42; color: #ffc882; }
Markdown > .markdown-link { color: #6ab0ff; }
Markdown > .markdown-blockquote { color: #a0a0aa; }

/* ---- 工具调用块（CC：✳ 橙标题，折叠成一行；输出粉色边框框） ---- */
Collapsible { margin: 1 2 0 2; background: #191919; }
Collapsible > .collapsible--title { color: #d77757; }
.tool-output { margin: 0 2 1 2; padding: 1 2;
               border: round #ecb7be; background: #232323; color: #d4d4d4; }

/* ---- 过程信息 / diff 标题 ---- */
.debug-line { margin: 0 2; color: #6e6a6a; }
.error-line { margin: 1 2; padding: 0 1; border-left: thick #ff6b80; color: #ffb4c0; }
.diff-title { margin: 1 2 0 2; color: #96a0b4; }

/* ---- 底部输入框 + 状态栏（dock 固定：输入在上，状态在下） ---- */
#bottom-bar { dock: bottom; height: 2; background: #101010; }
#input-line { height: 1; padding: 0 2; background: #101010; color: #e8e6e3; border: none; }
#input-line:focus { border: none; }
#status-line { height: 1; background: #101010; color: #a7a3a2; padding: 0 2; }

/* ---- 权限 Modal（CC：蓝紫边框权限确认） ---- */
#perm-box { width: 76; padding: 2 3; border: round #b1b9f9; background: #262626; }
#perm-title { color: #ffc107; }
"""


class PermissionModal(ModalScreen):
    """权限确认覆盖层：y 允许 / n|Esc 拒绝。应答后 dismiss（gate 自动唤醒 agent 线程）。

    200ms 防误触：弹窗后 0.2s 内按键无效（对应 CC 的 anti-misclick 设计）。"""

    BINDINGS = [
        ("y", "approve", "Allow"),
        ("n", "reject", "Deny"),
        ("escape", "reject", "Deny"),
    ]

    ARM_DELAY = 0.2

    def __init__(self, req_id: str, tool: str, args: dict, title: str = 'Permission needed'):
        super().__init__()
        self.req_id = req_id
        self.tool = tool
        self.args = args
        self.title = title
        self._armed = False

    def on_mount(self) -> None:
        self.set_timer(self.ARM_DELAY, self._arm)

    def _arm(self) -> None:
        self._armed = True

    def compose(self) -> ComposeResult:
        with Static(id="perm-box"):
            yield Static(f"[bold]{self.title}[/]", id="perm-title")
            yield Static(Text("Tool: ", style="dim") + Text(self.tool))
            yield Static(Text("Args: ", style="dim") + Text(str(self.args)))
            yield Static("")
            yield Static("[yellow]y[/] = Allow    [yellow]n[/]/[yellow]Esc[/] = Deny    "
                         f"([dim]{int(self.ARM_DELAY * 1000)}ms 防误触[/])")

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


class CCApp(App):
    TITLE = "harness"
    CSS = CSS

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+d", "quit", "Quit"),
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
        yield VerticalScroll(id="chat")
        with Vertical(id="bottom-bar"):
            yield Input(placeholder="输入消息，Enter 发送 · Ctrl+C 退出", id="input-line")
            yield Static('', id="status-line")

    def on_mount(self) -> None:
        import os
        import harness as h
        bus.subscribe(self._on_event)
        header = self.query_one('#header-line', Static)
        header.update(f"[#d77757]harness[/] [#a7a3a2]{os.getenv('LLM_MODEL_ID', '')}[/]"
                      f" [dim]{markup_escape(str(h.WORKDIR))}[/]")
        self._chat = self.query_one('#chat', VerticalScroll)
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
        # 帧刷新：网络 chunk 到达频率与 UI 渲染频率解耦 → 无论 chunk 多大都平滑打字机
        self.set_interval(0.05, self._flush_stream_buffers)
        self.set_interval(0.1, self._stream_tick)
        # 初始用户输入先入队（在工具/回复之前渲染，保持对话顺序）
        for m in self._msgs:
            if isinstance(m, dict) and m.get('role') == 'user':
                text = str(m.get('content', ''))
                if text:
                    self._event_queue.put(UIEvent('user_message', {'text': text[:2000]}))
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
        try:
            import harness
            harness.permission_gate.cancel_pending()
        except Exception:
            pass
        try:
            self._user_input_q.put(None)
        except Exception:
            pass

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
            col = Collapsible(title=Text(f"\u2737 {name} {args}"), collapsed=False)
            await self._chat.mount(col)
            self._active_tools.append(col)   # 工具串行执行，FIFO 匹配 tool_end
        elif e.type == 'tool_end':
            out = str(e.payload.get('output_preview', ''))[:2000]
            if self._active_tools:
                col = self._active_tools.pop(0)
                await col.mount(Static(Text(out or '[No output]'), classes='tool-output'))
                col.collapsed = True
        elif e.type == 'debug':
            await self._chat.mount(Static(Text(self._strip_ansi(e.payload.get('msg', ''))),
                                          classes='debug-line'))
        elif e.type == 'error':
            await self._chat.mount(Static(Text(f"Error: {e.payload.get('msg', '')}"),
                                          classes='error-line'))
        elif e.type == 'diff':
            path = e.payload.get('path', '')
            if path:
                await self._chat.mount(Static(Text(path), classes='diff-title'))
            await self._chat.mount(Static(self._render_diff(e.payload.get('diff_text', '')),
                                          classes='tool-output'))
        elif e.type == 'permission_request':
            self.push_screen(PermissionModal(
                e.payload['req_id'], e.payload['tool'], e.payload['args'],
                e.payload.get('title', 'Permission needed')))
        elif e.type == 'permission_resolve':
            self._input.focus()   # 弹窗应答后焦点回到输入框
        elif e.type == 'status':
            self._update_status(e.payload)
        # session_end 无需渲染
        self._chat.scroll_end(animate=False)   # scroll_end 是同步方法（返回 None）

    async def _stream_typing(self, md: Markdown, text: str) -> None:
        """打字机效果：分块增量渲染 Markdown（约 80 步完成，模拟流式）。"""
        if not text:
            return
        step = max(1, len(text) // 80)
        for i in range(0, len(text), step):
            await md.update(text[:i + step])   # Markdown.update 是异步的（AwaitComplete）
            await asyncio.sleep(0.02)

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
                text.append(ln + '\n', style='#4eba65')
            elif ln.startswith('-'):
                text.append(ln + '\n', style='#ff6b80')
            elif ln.startswith('@@'):
                text.append(ln + '\n', style='#96a0b4')
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
        line.update(f'{spin} [bold #d77757]generating[/] {elapsed:4.1f}s'
                    f' [dim]{self._stream_chars} chars[/]'
                    f'{("  " + base) if base else ""}')

    def _update_status(self, payload: dict) -> None:
        """CC 状态栏（常驻）：mode pill + model pill + ctx 进度条 + tokens/cost。

        thinking 时叠加「思考中」提示；idle 不清空，恢复显示最后已知指标。"""
        line = self._status_line()
        if line is None:
            return
        phase = payload.get('phase')
        if phase == 'thinking':
            base = self._pills(self._last_status)
            line.update(f'\u23f3 [bold #ffc107]\u601d\u8003\u4e2d...[/]'
                        f'{("  " + base) if base else ""}')
            return
        if phase == 'idle':
            line.update(self._pills(self._last_status))   # 常驻：idle 不空白
            return
        self._last_status = payload
        line.update(self._pills(payload))

    @staticmethod
    def _pills(payload: dict) -> str:
        """组装 CC 状态行：normal · model · ctx 进度条 · tokens · 花费。"""
        parts = ['[#a7a3a2 on #2a2a2a] normal [/]']   # mode pill
        if payload.get('model'):
            parts.append(f"[#d77757 on #2a2a2a] {payload['model']} [/]")
        ctx = payload.get('ctx_pct')
        if ctx is not None:
            parts.append(CCApp._ctx_bar(int(ctx)))
        if payload.get('tokens'):
            parts.append(f"[dim]{payload['tokens']} tok[/]")
        cost = payload.get('cost')
        if cost:
            parts.append(f"[dim]\u00a5{float(cost):.3f}[/]")
        return '  '.join(parts)

    @staticmethod
    def _ctx_bar(pct: int) -> str:
        """上下文占用进度条：10 格，实心=已用 / 空心=剩余，附百分比。

        >75% 红 / >50% 黄 / 否则绿。占用随会话推进由 status 事件实时刷新。"""
        pct = max(0, min(100, pct))
        filled = round(pct / 10)
        bar = '\u2588' * filled + '\u2591' * (10 - filled)   # █ 已用 / ░ 剩余
        color = '#ff6b80' if pct > 75 else '#ffc107' if pct > 50 else '#4eba65'
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
