"""通用确认弹窗：用于 /rewind 等会改文件/会话的操作。"""
from __future__ import annotations

from rich.markup import escape as markup_escape
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static


class ConfirmModal(ModalScreen):
    """y/Enter 确认，n/Esc 取消；默认不执行任何副作用。"""

    BINDINGS = [
        ('y', 'confirm', 'Confirm'),
        ('enter', 'confirm', 'Confirm'),
        ('n', 'cancel', 'Cancel'),
        ('escape', 'cancel', 'Cancel'),
    ]

    def __init__(self, title: str, body: str = '',
                 confirm_label: str = 'Confirm'):
        super().__init__()
        self.title = title
        self.body = body
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id='confirm-box'):
            yield Static(f'[bold]{markup_escape(self.title)}[/]',
                         classes='modal-title')
            if self.body:
                with VerticalScroll(classes='confirmation-body'):
                    yield Static(markup_escape(self.body), classes='modal-hint')
            yield Static(
                f'[yellow]y[/]/[yellow]Enter[/] = {markup_escape(self.confirm_label)}    '
                '[yellow]n[/]/[yellow]Esc[/] = Cancel',
                classes='modal-hint')

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
