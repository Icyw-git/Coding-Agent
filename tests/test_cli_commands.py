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
    assert command_error('/diff', 'extra')
    assert command_error('/undo', 'extra')
    assert command_error('/model', 'extra')
    assert parse_command('/perms') == ('/approvals', '')
    assert command_error('/compact', 'extra')


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


def test_workspace_commands_validate_arguments():
    assert command_error('/workspaces', 'extra')
    assert command_error('/workspace', 'add') is not None
    assert command_error('/workspace', 'remove') is not None
    assert command_error('/workspace', 'bogus now') is not None
    assert command_error('/workspace', '') is None
    assert command_error('/workspace', 'list') is None
    assert command_error('/workspace', 'add some/path --name n') is None


def test_workspace_add_list_and_remove(tmp_path, monkeypatch, capsys):
    import harness

    project = tmp_path / 'My Project'
    project.mkdir()
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)

    harness.handle_command('/workspaces', [], None)
    assert '尚未登记工作区' in capsys.readouterr().out

    harness.handle_command('/workspace add "My Project" --name 项目', [], None)
    output = capsys.readouterr().out
    assert '已登记工作区' in output and '项目' in output

    harness.handle_command('/workspaces', [], None)
    output = capsys.readouterr().out
    assert '项目' in output and str(project.resolve()) in output
    assert '（当前）' not in output   # WORKDIR 是 tmp_path，不是登记的项目目录

    harness.handle_command('/workspace remove 项目', [], None)
    assert '已移除登记' in capsys.readouterr().out
    harness.handle_command('/workspaces', [], None)
    assert '尚未登记工作区' in capsys.readouterr().out


def test_repl_new_ws_binds_registered_workspace(tmp_path, monkeypatch, capsys):
    import harness
    import workspaces

    project = tmp_path / 'project'
    project.mkdir()
    sessions = harness.SESSIONS_DIR
    sessions.mkdir(parents=True, exist_ok=True)
    workspaces.add_workspace(project, name='proj', file=harness.workspace_registry_path())
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    initial = harness.SessionEventStore(sessions, session_id='initial')
    commands = iter(['/new --ws proj', '/exit'])
    monkeypatch.setattr('builtins.input', lambda _: next(commands))

    harness.run_repl([], session_store=initial)

    assert harness.WORKDIR == project.resolve()
    assert '已开始新会话' in capsys.readouterr().out
    created = [path for path in sessions.glob('session_*.jsonl')
               if path.stem != 'session_initial']
    assert len(created) == 1
    assert harness.SessionEventStore.open(created[0]).session_summary()['workspace'] \
        == str(project.resolve())


def test_repl_new_ws_unknown_selector_does_not_create_session(
        tmp_path, monkeypatch, capsys):
    import harness

    sessions = harness.SESSIONS_DIR
    sessions.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    initial = harness.SessionEventStore(sessions, session_id='initial')
    initial.append_message({'role': 'user', 'content': 'previous'})
    commands = iter(['/new --ws missing', '/exit'])
    monkeypatch.setattr('builtins.input', lambda _: next(commands))

    harness.run_repl([], session_store=initial)

    output = capsys.readouterr().out
    assert '未找到工作区' in output
    assert [path.stem for path in sessions.glob('session_*.jsonl')] \
        == ['session_initial']


def test_repl_new_rejects_unknown_flag(tmp_path, monkeypatch, capsys):
    import harness

    sessions = harness.SESSIONS_DIR
    sessions.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    initial = harness.SessionEventStore(sessions, session_id='initial')
    initial.append_message({'role': 'user', 'content': 'previous'})
    commands = iter(['/new --wsfoo', '/exit'])
    monkeypatch.setattr('builtins.input', lambda _: next(commands))

    harness.run_repl([], session_store=initial)

    assert '未知参数' in capsys.readouterr().out
    assert [path.stem for path in sessions.glob('session_*.jsonl')] \
        == ['session_initial']
