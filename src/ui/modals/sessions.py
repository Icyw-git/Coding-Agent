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


class RenameSessionModal(ModalScreen):
    """输入新的会话标题；取消时不写入会话日志。"""

    BINDINGS = [Binding('escape', 'cancel', 'Cancel')]

    def __init__(self, title: str):
        super().__init__()
        self.title_text = title

    def compose(self) -> ComposeResult:
        with Vertical(id='rename-session-box'):
            yield Static('重命名会话', classes='modal-title')
            yield Input(value=self.title_text, id='rename-session-input',
                        max_length=80)
            yield Static('Enter 保存 · Esc 取消 · 最多 80 字',
                         classes='modal-hint')
            yield Static('', id='rename-session-error', classes='modal-hint')

    def on_mount(self) -> None:
        editor = self.query_one('#rename-session-input', Input)
        self.call_after_refresh(editor.focus)
        self.call_after_refresh(editor.select_all)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        title = event.value.strip()[:80]
        if not title:
            self.query_one('#rename-session-error', Static).update(
                'Title cannot be empty.')
            return
        self.dismiss(title)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionPickerModal(ModalScreen):
    """以搜索、排序、工作区范围和预览选择会话并恢复。"""

    BINDINGS = [
        Binding('escape', 'dismiss', 'Close'),
        Binding('alt+s', 'cycle_sort', 'Sort', show=False, priority=True),
        Binding('alt+w', 'toggle_workspace_scope', 'Workspace scope',
                show=False, priority=True),
        Binding('alt+v', 'toggle_archive_view', 'Active / archived',
                show=False, priority=True),
        Binding('alt+r', 'rename_session', 'Rename', show=False,
                priority=True),
        Binding('alt+a', 'archive_session', 'Archive', show=False,
                priority=True),
        Binding('alt+d', 'delete_session', 'Delete archived', show=False,
                priority=True),
    ]

    def __init__(self, rows: list[dict], current_workspace=None):
        super().__init__()
        self.rows = list(rows)
        self._by_id = {row['id']: row for row in self.rows}
        self.current_workspace = _workspace_key(current_workspace)
        self._sort = 'updated'
        self._workspace_scope = 'all'
        self._view = 'active'
        self._archive_rows: list[dict] | None = None
        self._archive_error = ''
        self._archive_loading = False
        self._visible: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id='session-box'):
            yield Static('进行中的会话', id='session-title',
                         classes='modal-title')
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

    def _visible_rows(self, query: str) -> list[dict]:
        needle = (query or '').strip().casefold()
        current_key = self.current_workspace
        source = (self.rows if self._view == 'active'
                  else self._archive_rows or [])
        result = []
        for row in sorted(source, key=self._sort_key,
                          reverse=self._sort != 'title'):
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

    def _sort_key(self, row: dict):
        return {
            'updated': (row.get('updated') or 0, str(row.get('id') or '')),
            'created': (row.get('created') or 0, str(row.get('id') or '')),
            'title': ((row.get('title') or '').casefold(),
                      str(row.get('id') or '')),
        }[self._sort]

    def set_archived_rows(self, rows: list[dict], error: str = '') -> None:
        self._archive_rows = None if error else list(rows)
        self._archive_error = error
        self._archive_loading = False
        if self.is_mounted and self._view == 'archived':
            search = self.query_one('#session-search', Input)
            self._filter(search.value)

    def _filter(self, query: str) -> None:
        options = self.query_one('#session-list', OptionList)
        options.clear_options()
        loading = self._view == 'archived' and self._archive_rows is None
        self.query_one('#session-title', Static).update(
            'Archived sessions' if self._view == 'archived' else '进行中的会话')
        if loading:
            self._visible = []
            self._by_id = {}
            self._show_preview(None)
            self._update_controls()
            return
        rows = self._visible_rows(query)
        self._visible = rows
        self._by_id = {str(row['id']): row for row in rows}
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
            if self._view == 'archived' and self._archive_error:
                message = self._archive_error
            elif self._view == 'archived' and self._archive_rows is None:
                message = 'Loading archived sessions…'
            else:
                message = 'No matching sessions.'
            preview.update(message)
            return
        title = str(row.get('title') or 'Untitled session')
        summary = ' '.join(str(row.get('preview') or 'No preview available.').split())
        created = relative_time(row.get('created'))
        preview.update(Text(f'{title}\n{summary}\nCreated {created}'))

    def _update_controls(self) -> None:
        count = self.query_one('#session-list', OptionList).option_count
        scope = 'current workspace' if self._workspace_scope == 'current' else 'all workspaces'
        sort = _SORT_LABELS[self._sort]
        if self._view == 'active':
            actions = 'Enter resume · Alt+R rename · Alt+A archive'
        elif self._archive_rows is None:
            actions = ('Alt+V retry archive list' if self._archive_error
                       else 'Loading archived sessions…')
        else:
            actions = 'Enter restore · Alt+D delete'
        self.query_one('#session-controls', Static).update(
            f'{count} sessions · {sort} · {scope} · Alt+S sort · Alt+W workspace'
            f' · Alt+V active/archived · {actions} · Esc 关闭')

    def action_toggle_archive_view(self) -> None:
        self._view = 'archived' if self._view == 'active' else 'active'
        query = self.query_one('#session-search', Input).value
        self._filter(query)
        if self._view == 'archived' and self._archive_rows is None:
            if self._archive_loading:
                return
            self._archive_loading = True
            self.app.load_archived_session_rows(self)

    def _selected_row(self) -> dict | None:
        options = self.query_one('#session-list', OptionList)
        if options.highlighted is None:
            return None
        option = options.get_option_at_index(options.highlighted)
        return self._by_id.get(str(option.id))

    def _dispatch_action(self, action: str) -> None:
        row = self._selected_row()
        if row is not None:
            self.dismiss({'_session_picker_action': action, 'row': row})

    def action_rename_session(self) -> None:
        if self._view == 'active':
            self._dispatch_action('rename')

    def action_archive_session(self) -> None:
        if self._view == 'active':
            self._dispatch_action('archive')

    def action_delete_session(self) -> None:
        if self._view == 'archived':
            self._dispatch_action('delete')

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
            if self._view == 'archived':
                self.dismiss({'_session_picker_action': 'restore', 'row': row})
            else:
                self.dismiss(row)
