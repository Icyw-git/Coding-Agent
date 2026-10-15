"""Interactive input adapter; command execution stays in the REPL."""

import os
import sys
import time
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion

from cli_commands import COMMANDS, parse_command


def session_label(row):
    updated = time.strftime('%Y-%m-%d %H:%M', time.localtime(row['updated']))
    return (f"  {row['title']}\n"
            f"    工作区：{row['workspace'] or '未记录（旧会话）'}\n"
            f"    最近任务：{row['preview']}\n"
            f"    {updated}  ID={row['id']}  events={row['events']}")


def choose_session(rows, current_id=None):
    """Select by recognizable context, not an opaque identifier."""
    if not rows:
        print('  (no saved sessions)')
        return None
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('\n\n'.join(session_label(row) for row in rows))
        print('  用 /resume <会话ID或唯一前缀> 恢复会话。')
        return None
    from prompt_toolkit.shortcuts import radiolist_dialog

    try:
        return radiolist_dialog(
            title='恢复会话',
            text='方向键选择，空格选中，Tab 切换按钮，Enter 确认；取消不会切换会话。',
            values=[(row['id'], ('[当前]\n' if row['id'] == current_id else '')
                     + session_label(row)) for row in rows],
            default=current_id if any(row['id'] == current_id for row in rows) else None,
            ok_text='恢复', cancel_text='取消',
        ).run()
    except (EOFError, KeyboardInterrupt):
        return None


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
        if command == '/help':
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
