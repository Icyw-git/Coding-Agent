"""会话清理弹窗：多选归档/恢复/删除，以及不可逆操作的短语确认。"""

from __future__ import annotations

from rich.markup import escape as markup_escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, SelectionList, Static
from textual.widgets.selection_list import Selection

from turn_progress import relative_time

MODES = {
    'archive': ('Archive sessions', '归档'),
    'restore': ('Restore archived sessions', '恢复'),
    'delete': ('Delete archived sessions', '永久删除'),
}


class CleanupSearchInput(Input):
    """筛选框：↓ 进入多选列表；Enter 由 Modal 统一确认。"""

    BINDINGS = [Binding('down', 'focus_list', 'To list', show=False)]

    def action_focus_list(self) -> None:
        try:
            options = self.screen.query_one('#cleanup-list', SelectionList)
            if options.option_count:
                options.focus()
        except Exception:
            pass


class CleanupPickerModal(ModalScreen):
    """空格勾选、Ctrl+A 全选可见、Enter 确认、Esc 取消。

    Enter 只做确认，不复用 SelectionList 的「切换勾选」——否则按回车会
    以为确认了、实际又勾了一条（旧 CleanupPicker 注释里踩过的坑）。
    """

    BINDINGS = [
        Binding('enter', 'confirm', 'Confirm', show=False, priority=True),
        Binding('ctrl+a', 'select_all', 'Select all', show=False, priority=True),
        Binding('escape', 'cancel', 'Cancel'),
    ]

    def __init__(self, rows: list[dict], mode: str = 'archive'):
        super().__init__()
        self.rows = list(rows)
        self.mode = mode
        self.title_text, self.action = MODES.get(mode, MODES['archive'])
        self.selected: set[str] = set()
        self._visible: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id='cleanup-box'):
            yield Static('', id='cleanup-title', classes='modal-title')
            yield CleanupSearchInput(placeholder='输入关键词筛选', id='cleanup-search')
            yield SelectionList(compact=True, id='cleanup-list')
            yield Static('', id='cleanup-notice', classes='modal-hint')

    def on_mount(self) -> None:
        self._render_rows('')
        search = self.query_one('#cleanup-search', CleanupSearchInput)
        self.call_after_refresh(search.focus)

    def _count_text(self, message: str = '') -> str:
        marked = sum(row['id'] in self.selected for row in self.rows)
        text = (
            f'已选 {marked}/{len(self.rows)} · 输入筛选 / ↓ 进列表 · 空格勾选 · Ctrl+A 全选 · Enter 确认 · Esc 取消'
        )
        return f'{message} · {text}' if message else text

    def _visible_rows(self, query: str) -> list[dict]:
        needle = (query or '').strip().casefold()
        if not needle:
            return list(self.rows)
        return [
            row
            for row in self.rows
            if needle in ' '.join(str(row.get(key) or '') for key in ('title', 'preview', 'workspace', 'id')).casefold()
        ]

    def _render_rows(self, query: str, message: str = '') -> None:
        self._visible = self._visible_rows(query)
        options = self.query_one('#cleanup-list', SelectionList)
        options.clear_options()
        for row in self._visible:
            workspace = row.get('workspace') or '工作目录未记录'
            label = f'{row.get("title") or "未命名任务"}  ·  {workspace}  ·  {relative_time(row.get("updated"))}'
            options.add_option(Selection(Text(label), row['id'], initial_state=row['id'] in self.selected))
        if self._visible:
            options.highlighted = 0
        self.query_one('#cleanup-title', Static).update(f'{self.title_text} · {len(self._visible)} 个候选')
        self.query_one('#cleanup-notice', Static).update(self._count_text(message))

    def on_input_changed(self, event: Input.Changed) -> None:
        if getattr(event.input, 'id', '') == 'cleanup-search':
            self._render_rows(event.value)

    def on_selection_list_selection_toggled(self, event: SelectionList.SelectionToggled) -> None:
        value = str(event.selection.value)
        if value in self.selected:
            self.selected.discard(value)
        else:
            self.selected.add(value)
        self.query_one('#cleanup-notice', Static).update(self._count_text())

    def action_select_all(self) -> None:
        visible = {row['id'] for row in self._visible}
        if not visible:
            self.query_one('#cleanup-notice', Static).update(self._count_text('没有匹配的会话'))
            return
        if visible <= self.selected:
            self.selected -= visible
            message = f'已取消勾选 {len(visible)} 个会话'
        else:
            self.selected |= visible
            message = f'已勾选 {len(visible)} 个会话'
        query = self.query_one('#cleanup-search', Input).value
        self._render_rows(query, message)

    def action_confirm(self) -> None:
        if not self.selected:
            self.query_one('#cleanup-notice', Static).update(self._count_text('还没有勾选会话：空格勾选，Ctrl+A 全选'))
            return
        chosen = [row['id'] for row in self.rows if row['id'] in self.selected]
        self.dismiss(chosen)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TypedConfirmModal(ModalScreen):
    """必须原样输入短语（默认“删除”）才放行；回车不会误触发。"""

    BINDINGS = [('escape', 'cancel', 'Cancel')]

    def __init__(self, title: str, body: str = '', phrase: str = '删除'):
        super().__init__()
        self.title = title
        self.body = body
        self.phrase = phrase

    def compose(self) -> ComposeResult:
        with Vertical(id='typed-box'):
            yield Static(f'[bold]{markup_escape(self.title)}[/]', classes='modal-title')
            if self.body:
                with VerticalScroll(classes='confirmation-body'):
                    yield Static(markup_escape(self.body), classes='modal-hint')
            yield Input(placeholder=f'输入“{self.phrase}”确认', id='typed-input')
            yield Static('', id='typed-error', classes='modal-hint')
            yield Static(f'输入“{markup_escape(self.phrase)}”后 Enter 确认 · Esc 取消', classes='modal-hint')

    def on_mount(self) -> None:
        self.call_after_refresh(self.query_one('#typed-input', Input).focus)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if (event.value or '').strip() == self.phrase:
            self.dismiss(True)
            return
        self.query_one('#typed-error', Static).update(f'未执行改动：请输入“{self.phrase}”。')

    def action_cancel(self) -> None:
        self.dismiss(False)
