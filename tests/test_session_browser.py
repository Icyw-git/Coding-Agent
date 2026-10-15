from session_store import SessionEventStore


def test_summary_records_workspace_changes_and_task(tmp_path):
    store = SessionEventStore(tmp_path, session_id='demo')
    store.record_workspace(tmp_path)
    store.record_workspace(tmp_path)
    assert len(store.read_events()) == 1
    store.append_message({'role': 'user', 'content': '修复 CLI 会话选择器'})
    store.append_session_title('CLI 会话管理')
    summary = store.session_summary()
    assert summary['title'] == 'CLI 会话管理'
    assert summary['workspace'] == str(tmp_path.resolve())
    assert summary['preview'] == '修复 CLI 会话选择器'
    assert store.rebuild_openai_history() == [{'role': 'user', 'content': '修复 CLI 会话选择器'}]


def test_resume_restores_workspace(tmp_path, monkeypatch, capsys):
    import harness
    import cli_input

    workspace = tmp_path / 'project'
    workspace.mkdir()
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    monkeypatch.setattr(harness, 'SKILLS_DIR', tmp_path / 'skills')
    saved = SessionEventStore(tmp_path, session_id='saved')
    saved.record_workspace(workspace)
    saved.append_message({'role': 'user', 'content': '检查代码质量'})
    initial = SessionEventStore(tmp_path, session_id='initial')
    monkeypatch.setattr(cli_input, 'choose_session', lambda rows, current: 'saved')
    commands = iter(['/resume', '/quit'])
    monkeypatch.setattr(cli_input, 'create_reader', lambda *args: lambda _: next(commands))
    harness.run_repl([], initial)
    assert harness.WORKDIR == workspace.resolve()
    output = capsys.readouterr().out
    assert '检查代码质量' in output
    assert str(workspace) in output


def test_missing_workspace_does_not_switch_session(tmp_path, monkeypatch, capsys):
    import harness
    import cli_input

    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    saved = SessionEventStore(tmp_path, session_id='missing')
    saved.record_workspace(tmp_path / 'deleted')
    initial = SessionEventStore(tmp_path, session_id='initial')
    seen = []
    monkeypatch.setattr(harness, 'handle_command', lambda line, messages, store: seen.append(store.session_id))
    commands = iter(['/resume missing', '/status', '/quit'])
    monkeypatch.setattr(cli_input, 'create_reader', lambda *args: lambda _: next(commands))
    harness.run_repl([], initial)
    assert seen == ['initial']
    assert saved.saved_session_title() is None
    assert harness.WORKDIR == tmp_path


def test_picker_cancel_keeps_current_session(tmp_path, monkeypatch):
    import cli_input
    import prompt_toolkit.shortcuts
    from types import SimpleNamespace

    monkeypatch.setattr(cli_input.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli_input.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(prompt_toolkit.shortcuts, 'radiolist_dialog',
                        lambda **kwargs: SimpleNamespace(run=lambda: None))
    store = SessionEventStore(tmp_path)
    store.record_workspace(tmp_path)
    assert cli_input.choose_session([store.session_summary()]) is None


def test_title_is_automatically_generated_after_turn(tmp_path, monkeypatch):
    import harness

    store = SessionEventStore(tmp_path)
    messages = [{'role': 'user', 'content': '检查代码质量并修复会话切换'}]
    store.append_message(messages[0])
    monkeypatch.setattr(harness, 'generate_session_title', lambda _: '会话切换代码审查')
    monkeypatch.setattr(harness, 'extract_memories', lambda _: None)
    monkeypatch.setattr(harness, 'consolidate_memories', lambda: None)
    harness._post_turn_housekeeping(messages, messages, store)
    assert store.session_summary()['title'] == '会话切换代码审查'
