"""通用单选弹窗：搜索过滤 + ↑↓ 选择 + Enter 确认（Pi `ctx.ui.select` 等价物）。

`/find` 的命中会话、`/workspace remove` 的登记项都只需要「列出候选、选一条」，
共用同一个 Modal，避免每加一个命令就复制一套搜索/焦点逻辑。
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option


class ChoiceSearchInput(Input):
    """搜索框里按 ↑↓ 进入列表，Enter 直接选第一条。"""

    BINDINGS = [
        Binding('down', 'focus_list', 'To list', show=False),
        Binding('up', 'focus_list', 'To list', show=False),
        Binding('enter', 'first', 'Select', show=False, priority=True),
    ]

    def action_focus_list(self) -> None:
        try:
            options = self.screen.query_one('#choice-list', OptionList)
            if options.option_count:
                options.focus()
        except Exception:
            pass

    def action_first(self) -> None:
        try:
            options = self.screen.query_one('#choice-list', OptionList)
            if options.option_count:
                if options.highlighted is None:
                    options.highlighted = 0
                options.focus()
                options.action_select()
        except Exception:
            pass


class ChoiceModal(ModalScreen):
    """rows: [{value, label, search?}]；dismiss 返回选中的 value 或 None。"""

    BINDINGS = [('escape', 'dismiss', 'Close')]

    def __init__(
        self,
        title: str,
        rows: list[dict],
        *,
        hint: str = '↑↓ 选择 · Enter 确认 · Esc 取消',
        placeholder: str = '输入关键词筛选',
        empty_hint: str = '没有匹配项。',
    ):
        super().__init__()
        self.title = title
        self.rows = list(rows)
        self.hint = hint
        self.placeholder = placeholder
        self.empty_hint = empty_hint

    def compose(self) -> ComposeResult:
        with Vertical(id='choice-box'):
            yield Static(self.title, classes='modal-title')
            yield ChoiceSearchInput(placeholder=self.placeholder, id='choice-search')
            yield OptionList(id='choice-list')
            yield Static('', id='choice-empty', classes='modal-hint')
            yield Static(self.hint, classes='modal-hint')

    def on_mount(self) -> None:
        self._filter('')
        search = self.query_one('#choice-search', ChoiceSearchInput)
        self.call_after_refresh(search.focus)

    def _filter(self, query: str) -> None:
        needle = (query or '').strip().casefold()
        options = self.query_one('#choice-list', OptionList)
        options.clear_options()
        visible = 0
        for row in self.rows:
            haystack = str(row.get('search') or row.get('label') or '')
            if needle and needle not in haystack.casefold():
                continue
            options.add_option(Option(Text(str(row['label'])), id=str(row['value'])))
            visible += 1
        self.query_one('#choice-empty', Static).update(self.empty_hint if not visible else '')
        if visible:
            options.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        if getattr(event.input, 'id', '') == 'choice-search':
            self._filter(event.value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))
