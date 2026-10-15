"""Interactive input adapter; command execution stays in the REPL."""

import os
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion

from cli_commands import COMMANDS, parse_command


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
