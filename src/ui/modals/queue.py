"""排队消息选择器：↑↓ 选择单条取回，或删除选中项。

队列分 steering（当前回合可注入）和 follow-up（本回合结束后处理），
两段都可能有内容。这里只负责选择与动作意图，真正从 PendingQueue 取走
和确认删除由 App 执行，避免弹窗持有可变队列造成索引错位。
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option


class QueueSearchInput(Input):
    """搜索框：↓ 进入列表，Enter 取回第一条可见消息。"""

    BINDINGS = [
        Binding('down', 'focus_list', 'To list', show=False),
        Binding('up', 'focus_list', 'To list', show=False),
        Binding('enter', 'first', 'Restore', show=False, priority=True),
    ]

    def action_focus_list(self) -> None:
        try:
            options = self.screen.query_one('#queue-list', OptionList)
            if options.option_count:
                options.focus()
        except Exception:
            pass

    def action_first(self) -> None:
        try:
            options = self.screen.query_one('#queue-list', OptionList)
            if options.option_count:
                if options.highlighted is None:
                    options.highlighted = 0
                options.action_select()
        except Exception:
            pass


class QueueModal(ModalScreen):
    """dismiss 返回 (action, value)：restore/delete 单条，all 取回全部。"""

    BINDINGS = [
        Binding('escape', 'cancel', 'Close'),
        Binding('ctrl+r', 'restore_all', 'Restore all', show=False, priority=True),
        # 不用裸字母：搜索框里还要能输入 d。列表获得焦点后按键会冒泡到这里。
        Binding('delete', 'delete', 'Delete', show=False),
        Binding('ctrl+d', 'delete', 'Delete', show=False),
    ]

    def __init__(self, rows: list[dict], *, hint: str = ''):
        super().__init__()
        self.rows = list(rows)
        self.hint = hint or (
            '↑↓ 选择 · Enter 取回 · Delete/Ctrl+D 删除 · Ctrl+R 取回全部 · Esc 关闭')
        self._visible: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id='queue-box'):
            yield Static('', id='queue-title', classes='modal-title')
            yield QueueSearchInput(placeholder='输入关键词筛选', id='queue-search')
            yield OptionList(id='queue-list')
            yield Static('', id='queue-empty', classes='modal-hint')
            yield Static(self.hint, classes='modal-hint')

    def on_mount(self) -> None:
        self._filter('')
        search = self.query_one('#queue-search', QueueSearchInput)
        self.call_after_refresh(search.focus)

    def _filter(self, query: str) -> None:
        needle = (query or '').strip().casefold()
        options = self.query_one('#queue-list', OptionList)
        options.clear_options()
        self._visible = []
        for row in self.rows:
            haystack = str(row.get('search') or row.get('text') or '')
            if needle and needle not in haystack.casefold():
                continue
            options.add_option(Option(Text(str(row['label'])), id=str(row['value'])))
            self._visible.append(row)
        visible = len(self._visible)
        self.query_one('#queue-title', Static).update(
            f'Queued messages · {visible}/{len(self.rows)}')
        self.query_one('#queue-empty', Static).update(
            '没有匹配的排队消息。' if not visible else '')
        if visible:
            options.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        if getattr(event.input, 'id', '') == 'queue-search':
            self._filter(event.value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(('restore', str(event.option.id)))

    def _highlighted_row(self) -> dict | None:
        options = self.query_one('#queue-list', OptionList)
        index = options.highlighted
        if index is None or not 0 <= index < len(self._visible):
            return None
        return self._visible[index]

    def action_delete(self) -> None:
        row = self._highlighted_row()
        if row is not None:
            self.dismiss(('delete', str(row['value'])))

    def action_restore_all(self) -> None:
        self.dismiss(('all', ''))

    def action_cancel(self) -> None:
        self.dismiss(None)
