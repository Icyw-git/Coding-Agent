"""TUI 基础控件：待发送队列、输入框、补全浮层与可选文本的视口。

控件只负责键位/鼠标到 App 动作的转发，不持有会话或渲染状态；这样
`harness_app.HarnessApp` 可以只关注布局、事件归约与回合控制。
"""
from __future__ import annotations

import threading

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.widgets import Static, TextArea

from ui.transcript import VirtualTranscript

SUGGESTION_LIMIT = 6


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
        Binding('ctrl+f', 'open_transcript_search', 'Search conversation',
                show=False, priority=True),
        Binding('ctrl+shift+p', 'open_command_palette', 'Command palette',
                show=False, priority=True),
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
