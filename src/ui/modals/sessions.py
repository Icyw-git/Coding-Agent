"""会话选择器：跨工作区搜索、排序、预览并恢复历史会话。"""
from __future__ import annotations

import os
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from turn_progress import relative_time

_SORTS = ('updated', 'created', 'title')
_SORT_LABELS = {
    'updated': '最近更新',
    'created': '创建时间',
    'title': '名称',
}


def _workspace_key(value) -> str:
    if not value:
        return ''
    return os.path.normcase(os.path.normpath(str(Path(value).expanduser())))


class SessionSearchInput(Input):
    """搜索框里按下方向键进入列表，Enter 选第一条。"""

    BINDINGS = [
        Binding('down', 'focus_list', 'To list', show=False),
        Binding('up', 'focus_list', 'To list', show=False),
        Binding('enter', 'first', 'Select', show=False, priority=True),
    ]

    def action_focus_list(self) -> None:
        try:
            options = self.screen.query_one('#session-list', OptionList)
            if options.option_count:
                options.focus()
        except Exception:
            pass

    def action_first(self) -> None:
        try:
            options = self.screen.query_one('#session-list', OptionList)
            if options.option_count:
                if options.highlighted is None:
                    options.highlighted = 0
                options.focus()
                options.action_select()
        except Exception:
            pass


class SessionPickerModal(ModalScreen):
    """以搜索、排序、工作区范围和预览选择会话并恢复。"""

    BINDINGS = [
        Binding('escape', 'dismiss', 'Close'),
        Binding('alt+s', 'cycle_sort', 'Sort', show=False, priority=True),
        Binding('alt+w', 'toggle_workspace_scope', 'Workspace scope',
                show=False, priority=True),
    ]

    def __init__(self, rows: list[dict], current_workspace=None):
        super().__init__()
        self.rows = list(rows)
        self._by_id = {row['id']: row for row in self.rows}
        self.current_workspace = _workspace_key(current_workspace)
        self._sort = 'updated'
        self._workspace_scope = 'all'

    def compose(self) -> ComposeResult:
        with Vertical(id='session-box'):
            yield Static('Resume session', classes='modal-title')
            yield SessionSearchInput(
                placeholder='搜索标题 / 工作区 / 会话 ID / 摘要',
                id='session-search')
            yield OptionList(id='session-list')
            yield Static('', id='session-preview', markup=False)
            yield Static('', id='session-controls', classes='modal-hint')

    def on_mount(self) -> None:
        self._filter('')
        search = self.query_one('#session-search', SessionSearchInput)
        # 等 screen 激活后再抢焦点，避免打开瞬间按键仍落在 Composer。
        self.call_after_refresh(search.focus)

    def _ordered_rows(self) -> list[dict]:
        key_by_sort = {
            'updated': lambda row: (row.get('updated') or 0,
                                   str(row.get('id') or '')),
            'created': lambda row: (row.get('created') or 0,
                                    str(row.get('id') or '')),
            'title': lambda row: ((row.get('title') or '').casefold(),
                                  str(row.get('id') or '')),
        }
        return sorted(self.rows, key=key_by_sort[self._sort],
                      reverse=self._sort != 'title')

    def _visible_rows(self, query: str) -> list[dict]:
        needle = (query or '').strip().casefold()
        current_key = self.current_workspace
        result = []
        for row in self._ordered_rows():
            row_workspace = _workspace_key(row.get('workspace'))
            if self._workspace_scope == 'current':
                if not current_key or not row_workspace or row_workspace != current_key:
                    continue
            haystack = ' '.join(str(row.get(key, '')) for key in
                                ('title', 'workspace', 'id', 'preview')).casefold()
            if needle and needle not in haystack:
                continue
            result.append(row)
        return result

    def _filter(self, query: str) -> None:
        options = self.query_one('#session-list', OptionList)
        rows = self._visible_rows(query)
        options.clear_options()
        for row in rows:
            workspace = row.get('workspace') or 'workspace unrecorded'
            label = (f"{row.get('title') or 'Untitled'}  ·  {workspace}"
                     f"  ·  {relative_time(row.get('updated'))}"
                     f"  ·  {str(row['id'])[:8]}")
            options.add_option(Option(label, id=row['id']))
        if options.option_count:
            options.highlighted = 0
            self._show_preview(rows[0])
        else:
            self._show_preview(None)
        self._update_controls()

    def _show_preview(self, row: dict | None) -> None:
        preview = self.query_one('#session-preview', Static)
        if row is None:
            preview.update('No matching sessions.')
            return
        title = str(row.get('title') or 'Untitled session')
        summary = ' '.join(str(row.get('preview') or 'No preview available.').split())
        created = relative_time(row.get('created'))
        preview.update(Text(f'{title}\n{summary}\nCreated {created}'))

    def _update_controls(self) -> None:
        scope = 'current workspace' if self._workspace_scope == 'current' else 'all workspaces'
        sort = _SORT_LABELS[self._sort]
        self.query_one('#session-controls', Static).update(
            f'{self.query_one("#session-list", OptionList).option_count} sessions'
            f' · sort: {sort} · scope: {scope}'
            ' · Alt+S sort · Alt+W scope · Enter resume · Esc close')

    def action_cycle_sort(self) -> None:
        index = _SORTS.index(self._sort)
        self._sort = _SORTS[(index + 1) % len(_SORTS)]
        search = self.query_one('#session-search', Input)
        self._filter(search.value)

    def action_toggle_workspace_scope(self) -> None:
        self._workspace_scope = (
            'current' if self._workspace_scope == 'all' else 'all')
        search = self.query_one('#session-search', Input)
        self._filter(search.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if getattr(event.input, 'id', '') == 'session-search':
            self._filter(event.value)

    def on_option_list_option_highlighted(
            self, event: OptionList.OptionHighlighted) -> None:
        self._show_preview(self._by_id.get(str(event.option.id)))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        row = self._by_id.get(str(event.option.id))
        if row is not None:
            self.dismiss(row)
