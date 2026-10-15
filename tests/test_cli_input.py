from pathlib import Path

from prompt_toolkit.document import Document

from cli_input import SlashCompleter, create_reader


def choices(completer, text):
    return list(completer.get_completions(Document(text), None))


def test_commands_include_descriptions_and_resolve_alias(tmp_path):
    completer = SlashCompleter(lambda: tmp_path, lambda: [])
    items = choices(completer, '/')
    assert '/resume' in [item.text for item in items]
    assert all(item.display_meta for item in items)
    assert [item.text for item in choices(completer, '/r')] == ['/resume', '/rewind']
    assert choices(completer, '普通消息 /r') == []


def test_session_and_help_completion(tmp_path):
    completer = SlashCompleter(lambda: tmp_path,
                               lambda: [('abc123', '会话主题'), ('def456', '其他')])
    items = choices(completer, '/r ab')
    assert [item.text for item in items] == ['abc123']
    assert items[0].start_position == -2
    assert [item.text for item in choices(completer, '/help rew')] == ['rewind']


def test_directory_completion_uses_current_workspace(tmp_path):
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    (first / 'My Project').mkdir()
    (first / 'My File.txt').touch()
    current = first
    completer = SlashCompleter(lambda: current, lambda: [])
    items = choices(completer, '/cd My')
    assert len(items) == 1
    assert items[0].text.startswith('"My Project')
    assert items[0].text.endswith('"')
    assert choices(completer, '/cd "My')
    current = second
    assert choices(completer, '/cd My') == []
    assert choices(completer, '/cd missing/child') == []


def test_rewind_flags(tmp_path):
    completer = SlashCompleter(lambda: tmp_path, lambda: [])
    assert [item.text for item in choices(completer, '/rewind last --se')] == ['--session']
    assert '--force' not in [item.text for item in choices(completer, '/rewind last --force ')]


def test_noninteractive_reader_uses_input(monkeypatch):
    monkeypatch.setattr('cli_input.sys.stdin.isatty', lambda: False)
    assert create_reader(lambda: Path.cwd(), lambda: []) is input


def test_prompt_session_tab_and_eof(tmp_path):
    import asyncio

    import pytest
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    async def exercise():
        with create_pipe_input() as pipe:
            session = PromptSession(
                input=pipe, output=DummyOutput(),
                completer=SlashCompleter(lambda: tmp_path, lambda: []),
                complete_while_typing=False,
            )
            task = asyncio.create_task(session.prompt_async('> '))
            pipe.send_text('/sta\t')
            # Completion is scheduled asynchronously by prompt_toolkit.
            for _ in range(100):
                if session.default_buffer.text == '/status':
                    break
                await asyncio.sleep(0.01)
            pipe.send_text('\n')
            assert await asyncio.wait_for(task, timeout=3) == '/status'
            pipe.send_bytes(b'\x04')
            with pytest.raises(EOFError):
                await session.prompt_async('> ')

    asyncio.run(exercise())
