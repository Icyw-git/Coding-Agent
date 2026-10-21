"""会话选择器：搜索过滤 + ↑↓ 选择 + Enter 恢复。"""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from turn_progress import relative_time


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
    """会话选择器：搜索过滤 + ↑↓ 选择 + Enter 恢复（对齐 Codex /resume）。"""

    BINDINGS = [('escape', 'dismiss', 'Close'),
                ('s', 'cycle_sort', 'Sort')]

    def __init__(self, rows: list[dict]):
        super().__init__()
        self.rows = list(rows)
        self._by_id = {row['id']: row for row in self.rows}
        self._sort = 'updated'    # updated | title

    def compose(self) -> ComposeResult:
        with Vertical(id='session-box'):
            yield Static('Resume session', classes='modal-title')
            yield SessionSearchInput(placeholder='搜索标题 / 工作区 / 会话 ID',
                                     id='session-search')
            yield OptionList(id='session-list')
            yield Static('↑↓ 选择 · s 切换排序 · Enter 恢复 · Esc 取消',
                         classes='modal-hint')

    def on_mount(self) -> None:
        self._filter('')
        search = self.query_one('#session-search', SessionSearchInput)
        # 等 screen 真正激活后再抢焦点，避免打开瞬间按键仍落在 Composer。
        self.call_after_refresh(search.focus)

    def _filter(self, query: str) -> None:

        needle = (query or '').strip().casefold()
        options = self.query_one('#session-list', OptionList)
        options.clear_options()
        rows = sorted(self.rows,
                      key=(lambda row: (row.get('title') or '').casefold())
                      if self._sort == 'title'
                      else (lambda row: row.get('updated') or 0),
                      reverse=self._sort != 'title')
        for row in rows:
            haystack = ' '.join(str(row.get(key, '')) for key in
                                ('title', 'workspace', 'id')).casefold()
            if needle and needle not in haystack:
                continue
            workspace = row.get('workspace') or 'workspace unrecorded'
            label = (f"{row['title']}  ·  {workspace}"
                     f"  ·  {relative_time(row.get('updated'))}"
                     f"  ·  {row['id'][:8]}")
            options.add_option(Option(label, id=row['id']))
        if options.option_count:
            options.highlighted = 0   # 首项高亮：聚焦后 Enter 即可恢复

    def action_cycle_sort(self) -> None:
        self._sort = 'title' if self._sort == 'updated' else 'updated'
        search = self.query_one('#session-search', Input)
        self._filter(search.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if getattr(event.input, 'id', '') == 'session-search':
            self._filter(event.value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        row = self._by_id.get(str(event.option.id))
        if row is not None:
            self.dismiss(row)
