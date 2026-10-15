import pytest

from cli_commands import command_error, format_command_help, parse_command


@pytest.mark.parametrize(('line', 'expected'), [
    ('/', ('/help', '')),
    ('/?', ('/help', '')),
    ('/R abc', ('/resume', 'abc')),
    ('/cp before refactor', ('/checkpoint', 'before refactor')),
    ('/cd "D:\\My Project"', ('/cd', '"D:\\My Project"')),
    ('/s', ('/sessions', '')),
    ('/exit', ('/quit', '')),
])
def test_parse_command(line, expected):
    assert parse_command(line) == expected


def test_help_and_aliases():
    help_text = format_command_help()
    for group in ('常用', '会话', '工作区', '快照'):
        assert group in help_text
    assert '/resume' in format_command_help('r')
    assert '/help' in format_command_help('unknown') or '未知命令' in format_command_help('unknown')


def test_invalid_arguments():
    assert command_error('/sessions', 'extra')
    assert command_error('/rewind', 'last --foce')
    assert command_error('/rewind', 'last head')
    assert command_error('/rewind', 'last --force --session --yes') is None
    assert '/sessions' in command_error('/sessons', '')


def test_help_does_not_initialize_checkpoints(monkeypatch, capsys):
    import harness

    def unexpected(*args):
        pytest.fail('help must not initialize checkpoint storage')

    monkeypatch.setattr(harness, 'get_checkpoint_manager', unexpected)
    harness.handle_command('/', [], None)
    assert '/resume' in capsys.readouterr().out


def test_cd_accepts_quoted_path(tmp_path, monkeypatch, capsys):
    import harness

    workspace = tmp_path / 'My Project'
    workspace.mkdir()
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    monkeypatch.setattr(harness, 'SKILLS_DIR', tmp_path / 'skills')
    harness.handle_command(f'/cd "{workspace}"', [], None)
    assert harness.WORKDIR == workspace
    assert str(workspace) in capsys.readouterr().out


def test_repl_resume_new_and_exit_aliases(tmp_path, monkeypatch, capsys):
    import harness

    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    saved = harness.SessionEventStore(tmp_path, session_id='saved-session')
    saved.append_message({'role': 'user', 'content': 'previous message'})
    initial = harness.SessionEventStore(tmp_path, session_id='initial')
    commands = iter(['/resume', '/r saved', '/n next-session', '/exit'])
    monkeypatch.setattr('builtins.input', lambda _: next(commands))
    monkeypatch.setattr(harness, 'format_session_list', lambda: 'saved-session')
    harness.run_repl([], session_store=initial)
    output = capsys.readouterr().out
    assert '已恢复：previous message' in output
    assert '已开始新会话' in output
    assert (tmp_path / 'session_next-session.jsonl').is_file()
