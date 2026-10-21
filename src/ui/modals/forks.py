"""Fork from message：选择一条用户消息，从它之前分叉（对齐 Pi）。"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class ForkPickerModal(ModalScreen):
    """↑↓ 选择、Enter 分叉、Esc 取消；默认选中最新一条。"""

    BINDINGS = [('escape', 'dismiss', 'Cancel')]

    def __init__(self, rows: list[dict]):
        super().__init__()
        self.rows = list(rows)

    def compose(self) -> ComposeResult:
        with Vertical(id='fork-box'):
            yield Static('Fork from message', classes='modal-title')
            yield Static('从选中的用户消息之前分叉；消息会回填到输入框。',
                         classes='modal-hint')
            yield OptionList(id='fork-list')
            yield Static('↑↓ 选择 · Enter 分叉 · Esc 取消', classes='modal-hint')

    def on_mount(self) -> None:
        options = self.query_one('#fork-list', OptionList)
        total = len(self.rows)
        for index, row in enumerate(self.rows, start=1):
            text = ' '.join(str(row.get('text', '')).split())
            if len(text) > 72:
                text = text[:71] + '…'
            label = f'{index}/{total}  {text}'
            options.add_option(Option(label, id=str(row['seq'])))
        if options.option_count:
            options.highlighted = options.option_count - 1
        self.call_after_refresh(options.focus)

    def on_option_list_option_selected(self,
                                       event: OptionList.OptionSelected) -> None:
        self.dismiss(int(event.option.id))
