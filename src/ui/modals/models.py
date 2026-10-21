"""模型选择器：列出已登记窗口的模型，选择后写回 LLM_MODEL_ID。"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class ModelPickerModal(ModalScreen):
    """↑↓ 选择、Enter 采用、Esc 取消。"""

    BINDINGS = [('escape', 'dismiss', 'Close')]

    def __init__(self, models: list[str], current: str = ''):
        super().__init__()
        self.models = list(models)
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id='model-box'):
            yield Static('Select model', classes='modal-title')
            yield OptionList(id='model-list')
            yield Static('↑↓ 选择 · Enter 采用 · Esc 取消', classes='modal-hint')

    def on_mount(self) -> None:
        options = self.query_one('#model-list', OptionList)
        for model in self.models:
            label = model + ('  (current)' if model == self.current else '')
            options.add_option(Option(label, id=model))
        if options.option_count:
            options.highlighted = 0
        self.call_after_refresh(options.focus)

    def on_option_list_option_selected(self,
                                       event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))
