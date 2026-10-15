"""Interactive input adapter; command execution stays in the REPL."""

import os
import sys
import time
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion

from cli_commands import COMMANDS, parse_command


def relative_time(timestamp, now=None):
    if not timestamp:
        return '未知时间'
    elapsed = max(0, int((time.time() if now is None else now) - timestamp))
    if elapsed < 60:
        return '刚刚'
    if elapsed < 3600:
        return f'{elapsed // 60} 分钟前'
    if elapsed < 86400:
        return f'{elapsed // 3600} 小时前'
    if elapsed < 604800:
        return f'{elapsed // 86400} 天前'
    return time.strftime('%Y-%m-%d', time.localtime(timestamp))


def session_label(row):
    workspace = row['workspace'] or '工作目录未记录'
    title = row['title'] or '未命名任务'
    preview = row.get('preview') or ''
    recent = '' if preview == title else preview
    lines = [f'  {title}', f'    工作区：{workspace}']
    if recent:
        lines.append(f'    最近：{recent}')
    lines.append(f"    {relative_time(row.get('updated', 0))}")
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
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.widgets import TextArea

        self.rows = rows
        self.workspace = workspace
        self.current_id = current_id
        self.show_all = workspace is None
        self.index = 0
        self.search = TextArea(height=1, prompt='搜索：', multiline=False)
        self.search.buffer.on_text_changed += self._reset_selection
        self.content = FormattedTextControl(self._render)
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
                event.app.exit(result=rows[self.index]['id'])

        @bindings.add('escape')
        @bindings.add('c-c')
        @bindings.add('c-d')
        def cancel(event):
            event.app.exit(result=None)

        @bindings.add('c-a')
        def toggle_scope(event):
            self.show_all = not self.show_all
            self.index = 0

        root = HSplit([
            Window(FormattedTextControl('恢复会话 · ↑↓ 选择 · Enter 恢复 · Esc 取消 · Ctrl+A 切换工作区范围'), height=1),
            self.search,
            Window(self.content, wrap_lines=True),
        ])
        self.application = Application(layout=Layout(root, focused_element=self.search),
                                       key_bindings=bindings, full_screen=True,
                                       **application_kwargs)

    def _reset_selection(self, buffer):
        self.index = 0

    def visible(self):
        rows = filter_session_rows(self.rows, query=self.search.text)
        if not self.show_all:
            rows = [row for row in rows if row.get('workspace') == str(self.workspace)]
        return rows

    def _render(self):
        rows = self.visible()
        scope = '全部工作区' if self.show_all else str(self.workspace)
        output = [('', f'范围：{scope} · {len(rows)} 个会话\n\n')]
        if not rows:
            return output + [('', '没有匹配的会话；可清空搜索或按 Ctrl+A 查看全部工作区。')]
        # Keep the selection visible without rendering an unbounded list.
        start = max(0, self.index - 2)
        for index in range(start, min(len(rows), start + 5)):
            row = rows[index]
            label = session_label(row).lstrip()
            marker = '› ' if index == self.index else '  '
            current = ' [当前]' if row['id'] == self.current_id else ''
            output.append(('reverse' if index == self.index else '', marker + label + current + '\n\n'))
        return output


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
                                       ('empty', '仅空会话'), ('restore', '恢复已归档会话')):
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
                if session_id.startswith(argument):
                    yield Completion(session_id, start_position=-len(argument),
                                     display_meta=title)
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
        quote = argument[:1] if argument.startswith(('"', "'")) else ''
        raw = argument[1:] if quote else argument
        if quote and raw.endswith(quote):
            raw = raw[:-1]
        expanded = Path(raw).expanduser()
        ends_with_separator = raw.endswith(('/', '\\'))
        parent = expanded if ends_with_separator or not raw else expanded.parent
        prefix = '' if ends_with_separator or not raw else expanded.name
        directory = parent if parent.is_absolute() else Path(self.workspace()) / parent
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
            for child in children:
                if child.is_dir() and child.name.casefold().startswith(prefix.casefold()):
                    value = str(parent / child.name) + os.sep
                    if quote or any(char.isspace() for char in value):
                        marker = quote or '"'
                        value = marker + value + marker
                    yield Completion(value, start_position=-len(argument),
                                     display=child.name + os.sep, display_meta='目录')
        except OSError:
            return


def create_reader(workspace, sessions):
    """Use plain input for pipes and test runners, never force a terminal UI."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input
    session = PromptSession(completer=SlashCompleter(workspace, sessions),
                            complete_while_typing=True)
    return session.prompt


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
