"""Fuzzy command palette backed by the shared command registry."""
from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ui.commands import CommandSpec
from ui.fuzzy import fuzzy_filter


class CommandPaletteInput(Input):
    BINDINGS = [
        Binding('down', 'focus_list', 'To list', show=False),
        Binding('up', 'focus_list', 'To list', show=False),
        Binding('enter', 'first', 'Select', show=False, priority=True),
    ]

    def action_focus_list(self) -> None:
        options = self.screen.query_one('#command-palette-list', OptionList)
        if options.option_count:
            options.focus()

    def action_first(self) -> None:
        options = self.screen.query_one('#command-palette-list', OptionList)
        if options.option_count > 1:
            options.highlighted = 1
            options.focus()
            options.action_select()


class CommandPaletteModal(ModalScreen):
    BINDINGS = [Binding('escape', 'dismiss', 'Close')]

    def __init__(self, commands: Sequence[CommandSpec]) -> None:
        super().__init__()
        self.commands = list(commands)
        self._by_name = {spec.name: spec for spec in self.commands}

    def compose(self) -> ComposeResult:
        with Vertical(id='command-palette-box'):
            yield Static('命令面板', classes='modal-title')
            yield CommandPaletteInput(
                placeholder='Search commands', id='command-palette-search')
            yield OptionList(id='command-palette-list')
            yield Static('', id='command-palette-empty', classes='modal-hint')
            yield Static('↑↓ select · Enter run or edit · Esc 关闭',
                         classes='modal-hint')

    def on_mount(self) -> None:
        self._filter('')
        self.call_after_refresh(
            self.query_one('#command-palette-search', CommandPaletteInput).focus)

    def _filter(self, query: str) -> None:
        matches = fuzzy_filter(
            self.commands, query,
            lambda spec: f'{spec.name} {spec.description}')
        options = self.query_one('#command-palette-list', OptionList)
        options.clear_options()
        current_group = None
        for spec in matches:
            group = 'Skills' if spec.name.startswith('skill:') else 'Commands'
            if group != current_group:
                current_group = group
                options.add_option(Option(
                    Text(group, style='bold #7f9d8f'),
                    id=f'group:{group.casefold()}', disabled=True))
            label = Text.assemble(
                (f'/{spec.name}', 'bold'),
                (f'  {spec.argument_hint}' if spec.argument_hint else '',
                 'dim'),
                (f'  {spec.description}', 'dim'))
            options.add_option(Option(label, id=spec.name))
        options.highlighted = 1 if matches else None
        self.query_one('#command-palette-empty', Static).update(
            '' if matches else 'No matching commands.')

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == 'command-palette-search':
            self._filter(event.value)

    def on_option_list_option_selected(self,
                                       event: OptionList.OptionSelected) -> None:
        value = str(event.option.id)
        if not value.startswith('group:'):
            self.dismiss(self._by_name[value])
