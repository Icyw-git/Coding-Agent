"""Current transcript search overlay."""
from __future__ import annotations

from collections.abc import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static


class TranscriptSearchInput(Input):
    BINDINGS = [
        Binding('enter', 'next_match', 'Next match', show=False,
                priority=True),
        Binding('shift+enter', 'previous_match', 'Previous match',
                show=False, priority=True),
    ]

    def action_next_match(self) -> None:
        self.screen.move(1)

    def action_previous_match(self) -> None:
        self.screen.move(-1)


class TranscriptSearchModal(ModalScreen):
    BINDINGS = [Binding('escape', 'close_search', 'Close', priority=True)]

    def __init__(self, on_query: Callable[[str], None],
                 on_move: Callable[[int], None], query: str = '') -> None:
        super().__init__()
        self._on_query = on_query
        self._on_move = on_move
        self.initial_query = query
        self._query = query

    def compose(self) -> ComposeResult:
        with Vertical(id='transcript-search-box'):
            yield Static('Search this conversation', classes='modal-title')
            yield TranscriptSearchInput(
                value=self.initial_query,
                placeholder='Search message text',
                max_length=200,
                id='transcript-search-input')
            yield Static('Type to search', id='transcript-search-status',
                         classes='modal-hint')
            yield Static('', id='transcript-search-preview', markup=False)
            yield Static('Enter next · Shift+Enter previous · Esc close',
                         classes='modal-hint')

    def on_mount(self) -> None:
        editor = self.query_one('#transcript-search-input',
                                TranscriptSearchInput)
        self.call_after_refresh(editor.focus)
        self._on_query(editor.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == 'transcript-search-input':
            self._query = event.value
            self._on_query(event.value)

    def move(self, direction: int) -> None:
        self._on_move(direction)

    def set_results(self, count: int, index: int = -1,
                    snippet: str = '', message: str = '') -> None:
        if message:
            status = message
        elif count:
            status = f'{index + 1} / {count} messages'
        else:
            status = 'No matching messages' if self._query.strip() else 'Type to search'
        self.query_one('#transcript-search-status', Static).update(status)
        self.query_one('#transcript-search-preview', Static).update(snippet)

    def action_close_search(self) -> None:
        self.dismiss(None)
