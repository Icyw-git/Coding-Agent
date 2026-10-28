"""Searchable picker for the configured main-role model."""
from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ui.fuzzy import fuzzy_filter


@dataclass(frozen=True)
class ModelChoice:
    ref: str
    label: str
    search: str
    context_window: int | None = None


class ModelSearchInput(Input):
    BINDINGS = [
        Binding('down', 'focus_list', 'To list', show=False),
        Binding('up', 'focus_list', 'To list', show=False),
        Binding('enter', 'first', 'Select', show=False, priority=True),
    ]

    def action_focus_list(self) -> None:
        options = self.screen.query_one('#model-list', OptionList)
        if options.option_count:
            options.focus()

    def action_first(self) -> None:
        options = self.screen.query_one('#model-list', OptionList)
        if options.option_count:
            options.focus()
            options.action_select()


class ModelPickerModal(ModalScreen):
    """Fuzzy filter models; Enter applies the highlighted model."""

    BINDINGS = [Binding('escape', 'dismiss', 'Close')]

    def __init__(self, models: list[ModelChoice], current: str = ''):
        super().__init__()
        self.models = list(models)
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id='model-box'):
            yield Static('选择模型', classes='modal-title')
            yield ModelSearchInput(
                placeholder='Search model or provider', id='model-search')
            yield OptionList(id='model-list')
            yield Static('', id='model-empty', classes='modal-hint')
            yield Static('↑↓ select · Enter 应用 · Esc 关闭',
                         classes='modal-hint')

    def on_mount(self) -> None:
        self._filter('')
        self.call_after_refresh(
            self.query_one('#model-search', ModelSearchInput).focus)

    def _filter(self, query: str) -> None:
        matches = fuzzy_filter(self.models, query, lambda item: item.search)
        options = self.query_one('#model-list', OptionList)
        options.clear_options()
        for model in matches:
            label = Text.assemble(
                (model.label, 'bold'),
                ('  current', 'bold #5f9d76') if model.ref == self.current else ('', ''),
                (f'  ·  {model.context_window:,} tokens'
                 if model.context_window else '', 'dim'))
            options.add_option(Option(label, id=model.ref))
        current_index = next(
            (index for index, model in enumerate(matches)
             if model.ref == self.current), None)
        options.highlighted = (current_index if current_index is not None
                               else (0 if matches else None))
        self.query_one('#model-empty', Static).update(
            '' if matches else 'No matching models.')

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == 'model-search':
            self._filter(event.value)

    def on_option_list_option_selected(self,
                                       event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))
