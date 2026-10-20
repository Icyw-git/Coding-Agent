"""Interactive input adapter; command execution stays in the REPL."""

import os
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion

from cli_commands import COMMANDS, parse_command
from session_views import display_text, group_sessions, relative_time, session_details, session_status


def session_label(row):
    workspace = row['workspace'] or '工作目录未记录'
    title = row['title'] or '未命名任务'
    preview = row.get('preview') or ''
    recent = '' if preview == title else preview
    lines = [f'  {title}', f'    工作区：{workspace}']
    if recent:
        lines.append(f'    最近：{recent}')
    lines.append(f"    最近活跃：{relative_time(row.get('updated'))}")
    return '\n'.join(lines)


def filter_session_rows(rows, workspace=None, include_empty=False, query=''):
    query = query.strip().casefold()
    filtered = []
    for row in rows:
        if not include_empty and row.get('preview') == '尚无任务':
            continue
        haystack = ' '.join(str(row.get(key, '')) for key in
                            ('title', 'preview', 'workspace')).casefold()
        if query and query not in haystack and not row['id'].startswith(query):
            continue
        filtered.append(row)
    if workspace:
        return [row for row in filtered if row.get('workspace') == str(workspace)]
    return filtered


def choose_session(rows, current_id=None, workspace=None):
    """Select by recognizable context, not an opaque identifier."""
    if not rows:
        print('  (no saved sessions)')
        return None
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('\n\n'.join(session_label(row) for row in rows))
        print('  用 /resume <主题、目录或会话 ID 前缀> 恢复会话。')
        return None
    picker = SessionPicker(rows, current_id, workspace)
    try:
        return picker.application.run()
    except (EOFError, KeyboardInterrupt):
        return None


class SessionPicker:
    """Search stays focused while arrows move the selection and Enter resumes."""

    def __init__(self, rows, current_id=None, workspace=None, **application_kwargs):
        from prompt_toolkit.application import Application
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import TextArea

        self.rows = rows
        self.workspace = workspace
        self.current_id = current_id
        self.show_all = True
        self.filter_index = 0
        self.filters = ('全部', '当前', '历史', '空会话')
        self.index = 0
        self.search = TextArea(height=1, prompt='搜索：', multiline=False)
        self.search.buffer.on_text_changed += self._reset_selection
        self.content = FormattedTextControl(self._render)
        self.list_window = Window(self.content, wrap_lines=False, width=Dimension(weight=3))
        bindings = KeyBindings()

        @bindings.add('up')
        def previous(event):
            self.index = max(0, self.index - 1)

        @bindings.add('down')
        def following(event):
            self.index = min(max(0, len(self.visible()) - 1), self.index + 1)

        @bindings.add('tab')
        @bindings.add('s-tab')
        def change_filter(event):
            step = -1 if event.key_sequence[0].key == 's-tab' else 1
            self.filter_index = (self.filter_index + step) % len(self.filters)
            self.index = 0

        @bindings.add('pageup')
        @bindings.add('pagedown')
        def move_page(event):
            step = -8 if event.key_sequence[0].key == 'pageup' else 8
            self.index = min(max(0, len(self.visible()) - 1), max(0, self.index + step))

        @bindings.add('enter')
        def accept(event):
            rows = self.visible()
            if rows:
                event.app.exit(result=rows[self.index]['id'])

        @bindings.add('escape')
        @bindings.add('c-c')
        @bindings.add('c-d')
        def cancel(event):
            event.app.exit(result=None)

        @bindings.add('c-a')
        def toggle_scope(event):
            if self.workspace is not None:
                self.show_all = not self.show_all
            self.index = 0

        wide = Condition(lambda: self.application.output.get_size().columns >= 100)
        details = Window(FormattedTextControl(self._render_details), wrap_lines=True,
                         width=Dimension(weight=2))
        root = HSplit([
            Window(FormattedTextControl('会话管理   按工作区分组'), height=1, style='class:heading'),
            Window(FormattedTextControl(self._render_filters), height=1),
            self.search,
            Window(height=1, char='─'),
            VSplit([self.list_window,
                    ConditionalContainer(Window(width=1, char='│'), wide),
                    ConditionalContainer(details, wide)]),
            Window(height=1, char='─'),
            Window(FormattedTextControl('↑↓ 选择 · Enter 切换 · Tab 筛选 · Ctrl+A 当前/全部目录 · Esc 返回'), height=1),
        ])
        application_kwargs.setdefault('style', Style.from_dict({
            'heading': 'bold', 'group': 'ansibrightblack', 'selected': 'bg:ansiblue ansiwhite bold',
            'muted': 'ansibrightblack', 'tab': 'reverse bold',
        }))
        self.application = Application(layout=Layout(root, focused_element=self.search),
                                       key_bindings=bindings, full_screen=True,
                                       **application_kwargs)

    def _reset_selection(self, buffer):
        self.index = 0

    def visible(self):
        rows = filter_session_rows(self.rows, include_empty=True, query=self.search.text)
        if not self.show_all:
            rows = [row for row in rows if row.get('workspace') == str(self.workspace)]
        if self.filter_index:
            status = self.filters[self.filter_index]
            rows = [row for row in rows if session_status(row, self.current_id) == status]
        return [row for _, group in group_sessions(rows) for row in group]

    def _render_filters(self):
        rows = filter_session_rows(self.rows, include_empty=True, query=self.search.text,
                                   workspace=None if self.show_all else self.workspace)
        output = []
        for index, label in enumerate(self.filters):
            count = len(rows) if index == 0 else sum(session_status(row, self.current_id) == label for row in rows)
            output.append(('class:tab' if index == self.filter_index else '', f' {label} {count} '))
        return output

    def _render(self):
        rows = self.visible()
        scope = '全部工作区' if self.show_all else str(self.workspace)
        output = [('class:muted', f'范围：{display_text(scope)} · {len(rows)} 个会话\n')]
        if not rows:
            return output + [('', '没有匹配的会话；可清空搜索或按 Ctrl+A 查看全部工作区。')]
        self.index = min(self.index, len(rows) - 1)
        height = self.list_window.render_info.window_height if self.list_window.render_info else 16
        # Each visible row may need a group heading; this fits even a short terminal.
        count = max(1, (height - 3) // 2)
        start = max(0, min(self.index - count // 2, len(rows) - count))
        width = self.list_window.render_info.window_width if self.list_window.render_info else 70
        output.append(('class:muted', fit_cells('  会话', max(8, width - 18), pad=True) + '状态    最近活跃\n'))
        previous_group = None
        for index in range(start, min(len(rows), start + count)):
            row = rows[index]
            group = row.get('workspace') or '工作目录未记录'
            if group != previous_group:
                total = sum(item.get('workspace') == row.get('workspace') for item in rows)
                output.append(('class:group', fit_cells(f'{group}  ({total})', width) + '\n'))
                previous_group = group
            marker = '› ' if index == self.index else '  '
            status = session_status(row, self.current_id)
            suffix = f' {fit_cells(status, 6, pad=True)} {fit_cells(relative_time(row.get("updated")), 16, pad=True)}'
            title_width = max(1, width - text_width(suffix) - 2)
            title = fit_cells(row.get('title') or '新会话', title_width, pad=True)
            output.append(('class:selected' if index == self.index else '', marker + title + suffix + '\n'))
        return output

    def _render_details(self):
        rows = self.visible()
        if not rows:
            return [('', '  会话详情\n\n  选择会话查看最近任务与回复。')]
        row = rows[min(self.index, len(rows) - 1)]
        output = [('class:heading', '  会话详情\n\n')]
        for label, value in session_details(row, self.current_id):
            output.extend([('class:muted', f'{label}\n'), ('', display_text(value) + '\n\n')])
        return output


def text_width(text):
    from prompt_toolkit.utils import get_cwidth
    return get_cwidth(text)


def fit_cells(text, width, pad=False):
    text = ' '.join(display_text(text).split())
    if text_width(text) > width:
        clipped = ''
        for char in text:
            if text_width(clipped + char) > max(0, width - 1):
                break
            clipped += char
        text = clipped + '…'
    return text + ' ' * max(0, width - text_width(text)) if pad else text


def directory_completions(argument, workspace):
    """目录补全：`/cd` 与「浏览其他工作区」共用同一份解析与引号处理。"""
    quote = argument[:1] if argument.startswith(('"', "'")) else ''
    raw = argument[1:] if quote else argument
    if quote and raw.endswith(quote):
        raw = raw[:-1]
    expanded = Path(raw).expanduser()
    ends_with_separator = raw.endswith(('/', '\\'))
    parent = expanded if ends_with_separator or not raw else expanded.parent
    prefix = '' if ends_with_separator or not raw else expanded.name
    directory = parent if parent.is_absolute() else Path(workspace()) / parent
    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return
    for child in children:
        if not child.is_dir() or not child.name.casefold().startswith(prefix.casefold()):
            continue
        value = str(parent / child.name) + os.sep
        if quote or any(char.isspace() for char in value):
            marker = quote or '"'
            value = marker + value + marker
        yield Completion(value, start_position=-len(argument),
                         display=child.name + os.sep, display_meta='目录')


class SlashCompleter(Completer):
    def __init__(self, workspace, sessions):
        self.workspace = workspace
        self.sessions = sessions

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith('/'):
            return
        if not any(char.isspace() for char in text):
            for command in COMMANDS:
                names = (command.name, *command.aliases)
                if any(name.startswith(text.lower()) for name in names):
                    yield Completion(command.name, start_position=-len(text),
                                     display_meta=command.description)
            return
        command, argument = parse_command(text)
        if command == '/archive':
            for value, description in (('30', '30 天未活跃'), ('90', '90 天未活跃'),
                                       ('all', '全部非当前会话'), ('empty', '仅空会话'), ('restore', '恢复已归档会话')):
                if value.startswith(argument):
                    yield Completion(value, start_position=-len(argument), display_meta=description)
        elif command == '/help':
            for item in COMMANDS:
                name = item.name.lstrip('/')
                if name.startswith(argument.lstrip('/').lower()):
                    yield Completion(name, start_position=-len(argument),
                                     display_meta=item.description)
        elif command == '/resume':
            for session_id, title in self.sessions():
                if (session_id.startswith(argument)
                        or argument.casefold() in title.casefold()):
                    yield Completion(session_id, start_position=-len(argument),
                                     display=display_text(title), display_meta='恢复会话')
        elif command == '/rewind':
            prefix = argument.split()[-1] if argument and not argument.endswith(' ') else ''
            used = argument.split()
            options = ('--force', '--session', '--yes')
            if len(used) <= 1:
                options = ('last', 'head', *options)
            for option in options:
                if option.startswith(prefix) and option not in used:
                    yield Completion(option, start_position=-len(prefix))
        elif command == '/cd':
            yield from self._directories(argument)

    def _directories(self, argument):
        yield from directory_completions(argument, self.workspace)


def create_reader(workspace, sessions):
    """Use plain input for pipes and test runners, never force a terminal UI."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input
    session = PromptSession(completer=SlashCompleter(workspace, sessions),
                            complete_while_typing=True)
    return session.prompt


class DirectoryBrowser:
    """逐级选择电脑中的文件夹：↑↓ 移动，Enter 进入/确认，Backspace 返回上级。"""

    def __init__(self, start, **application_kwargs):
        from prompt_toolkit.application import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.styles import Style

        from workspace_choices import browse_entries, system_drives

        self._entries = browse_entries
        self._drives = system_drives
        self.path = Path(start).expanduser()
        self.index = 0          # 置顶的「进入此文件夹」——第一次 Enter 就是确认当前目录
        self.content = FormattedTextControl(self._render)
        self.list_window = Window(self.content, wrap_lines=False,
                                  height=Dimension(weight=1))
        bindings = KeyBindings()

        @bindings.add('up')
        def previous(event):
            self.index = max(0, self.index - 1)

        @bindings.add('down')
        def following(event):
            self.index = min(max(0, len(self.entries()) - 1), self.index + 1)

        @bindings.add('home')
        def to_first(event):
            self.index = 0

        @bindings.add('end')
        def to_last(event):
            self.index = max(0, len(self.entries()) - 1)

        @bindings.add('enter')
        def enter(event):
            rows = self.entries()
            row = rows[min(self.index, len(rows) - 1)]
            if row['kind'] == 'here':
                event.app.exit(result=row['path'])
            else:
                self._move_to(Path(row['path']))

        @bindings.add('backspace')
        @bindings.add('left')
        def go_parent(event):
            parent = self.path.parent
            if parent != self.path:
                self._move_to(parent)

        @bindings.add('escape')
        @bindings.add('c-c')
        @bindings.add('c-d')
        def cancel(event):
            event.app.exit(result=None)

        root = HSplit([
            Window(FormattedTextControl('新建会话 · 选择文件夹'), height=1, style='class:heading'),
            Window(FormattedTextControl(self._render_path), height=1, style='class:path'),
            Window(height=1, char='─'),
            self.list_window,
            Window(height=1, char='─'),
            Window(FormattedTextControl(
                '↑↓ 移动 · Enter 进入 · Backspace 上级 · Esc 取消'), height=1),
        ])
        application_kwargs.setdefault('style', Style.from_dict({
            'heading': 'bold', 'path': 'ansicyan', 'muted': 'ansibrightblack',
            'selected': 'bg:ansiblue ansiwhite bold',
        }))
        self.application = Application(layout=Layout(root), key_bindings=bindings,
                                       full_screen=True, **application_kwargs)

    def entries(self):
        rows = self._entries(self.path, self._drives())
        here = {'kind': 'here', 'name': '进入此文件夹', 'path': str(self.path)}
        rows = [here, *rows]
        self.index = min(self.index, len(rows) - 1)
        return rows

    def _move_to(self, path):
        self.path = Path(path).expanduser()
        # 进入子目录后回到置顶行：再按 Enter 就是「进入这个目录」并开始会话，
        # 想继续往下走就按 ↓（子目录紧跟在置顶行后面）。
        self.index = 0

    def _render_path(self):
        return [('class:path', f'  {display_text(str(self.path))}')]

    def _render(self):
        rows = self.entries()
        output = []
        height = self.list_window.render_info.window_height if self.list_window.render_info else 20
        count = max(3, height - 2)
        start = max(0, min(self.index - count // 2, len(rows) - count))
        prefix = {'here': '> ', 'parent': '.. ', 'dir': '[+] ', 'drive': '[盘] '}
        for position in range(start, min(len(rows), start + count)):
            row = rows[position]
            marker = '› ' if position == self.index else '  '
            label = prefix[row['kind']] + row['name']
            if row['kind'] == 'dir':
                label += os.sep
            output.append(('class:selected' if position == self.index else '',
                           f'{marker}{label}\n'))
        return output


def browse_for_workspace(start):
    """打开文件夹浏览器；非 TTY 或取消返回 None。"""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    try:
        return DirectoryBrowser(start).application.run()
    except (EOFError, KeyboardInterrupt):
        return None


class WorkspacePicker:
    """选择新会话的工作区：列表来自最近会话，默认选中当前目录。"""

    def __init__(self, rows, **application_kwargs):
        from prompt_toolkit.application import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import TextArea

        self.rows = [*rows, {'path': '', 'name': '浏览电脑中的文件夹…', 'sessions': 0,
                             'last_active': None, 'current': False, 'browse': True}]
        self.index = 0
        self.search = TextArea(height=1, prompt='筛选：', multiline=False)
        self.search.buffer.on_text_changed += self._reset_selection
        self.content = FormattedTextControl(self._render)
        self.list_window = Window(self.content, wrap_lines=False,
                                  height=Dimension(weight=1))
        bindings = KeyBindings()

        @bindings.add('up')
        def previous(event):
            self.index = max(0, self.index - 1)

        @bindings.add('down')
        def following(event):
            self.index = min(max(0, len(self.visible()) - 1), self.index + 1)

        @bindings.add('enter')
        def accept(event):
            rows = self.visible()
            if rows:
                event.app.exit(result=rows[self.index])

        @bindings.add('escape')
        @bindings.add('c-c')
        @bindings.add('c-d')
        def cancel(event):
            event.app.exit(result=None)

        root = HSplit([
            Window(FormattedTextControl('新建会话 · 选择工作区'), height=1, style='class:heading'),
            self.search,
            Window(height=1, char='─'),
            self.list_window,
            Window(height=1, char='─'),
            Window(FormattedTextControl(
                '↑↓ 选择 · Enter 确认 · Esc 取消；选「浏览」可逐级挑任意文件夹'), height=1),
        ])
        application_kwargs.setdefault('style', Style.from_dict({
            'heading': 'bold', 'muted': 'ansibrightblack',
            'selected': 'bg:ansiblue ansiwhite bold',
        }))
        self.application = Application(layout=Layout(root, focused_element=self.search),
                                       key_bindings=bindings, full_screen=True,
                                       **application_kwargs)

    def _reset_selection(self, buffer):
        self.index = 0

    def visible(self):
        query = self.search.text.strip().casefold()
        rows = []
        for row in self.rows:
            haystack = '浏览其他目录' if row.get('browse') else f"{row['name']} {row['path']}"
            if query and query not in haystack.casefold():
                continue
            rows.append(row)
        self.index = min(self.index, max(0, len(rows) - 1))
        return rows

    def _render(self):
        rows = self.visible()
        output = [('class:muted', '  ↑↓ 选择，Enter 确认；默认是当前目录。\n\n')]
        for index, row in enumerate(rows):
            marker = '› ' if index == self.index else '  '
            if row.get('browse'):
                line = '  ── 浏览其他目录…（输入路径）'
            else:
                mark = '● ' if row.get('current') else '○ '
                meta = f"{row['sessions']} 个会话"
                if row.get('last_active'):
                    meta += f" · {relative_time(row['last_active'])}"
                line = f"  {mark}{row['name']}  {row['path']}  （{meta}）"
            output.append(('class:selected' if index == self.index else '', marker + line + '\n'))
        return output


def choose_workspace(rows):
    """交互选择工作区；非 TTY 或取消返回 None（调用方回退到当前目录）。"""
    if not rows or not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    try:
        return WorkspacePicker(rows).application.run()
    except (EOFError, KeyboardInterrupt):
        return None


def choose_cleanup_sessions(rows, *, restore=False, delete=False):
    if not rows:
        print('  没有符合条件的会话。')
        return []
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('  请在交互终端中选择会话；本次未执行任何改动。')
        return []
    from prompt_toolkit.shortcuts import checkboxlist_dialog

    try:
        return checkboxlist_dialog(
            title=('永久删除归档（不可恢复）' if delete else
                   '恢复归档' if restore else '归档旧会话（可恢复）'),
            text='↑↓ 移动，空格多选，Tab 切换按钮，Enter 确认；不影响项目文件。',
            values=[(row['id'], session_label(row)) for row in rows],
            ok_text='选择完成', cancel_text='取消',
        ).run() or []
    except (EOFError, KeyboardInterrupt):
        return []
