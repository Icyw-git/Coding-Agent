import pytest

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


@pytest.mark.parametrize('command', ['/resume', '/sessions'])
def test_resume_restores_workspace(tmp_path, monkeypatch, capsys, command):
    import cli_input
    import harness

    workspace = tmp_path / 'project'
    workspace.mkdir()
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    monkeypatch.setattr(harness, 'SKILLS_DIR', tmp_path / 'skills')
    saved = SessionEventStore(tmp_path, session_id='saved')
    saved.record_workspace(workspace)
    saved.append_message({'role': 'user', 'content': '检查代码质量'})
    initial = SessionEventStore(tmp_path, session_id='initial')
    monkeypatch.setattr(cli_input, 'choose_session', lambda rows, current, workspace: 'saved')
    commands = iter([command, '/quit'])
    monkeypatch.setattr(cli_input, 'create_reader', lambda *args: lambda _: next(commands))
    harness.run_repl([], initial)
    assert harness.WORKDIR == workspace.resolve()
    output = capsys.readouterr().out
    assert '检查代码质量' in output
    assert str(workspace) in output


def test_missing_workspace_does_not_switch_session(tmp_path, monkeypatch, capsys):
    import cli_input
    import harness

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
    from types import SimpleNamespace

    import cli_input

    monkeypatch.setattr(cli_input.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli_input.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(cli_input, 'SessionPicker',
                        lambda *args: SimpleNamespace(application=SimpleNamespace(run=lambda: None)))
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


def test_compact_labels_and_filtering():
    from cli_input import filter_session_rows, relative_time, session_label

    row = dict(id='internal-id', title='修复代码', preview='修复代码',
               workspace='D:/project', updated=10, events=99)
    empty = dict(row, id='empty', title='新会话', preview='尚无任务')
    label = session_label(row)
    assert label.count('修复代码') == 1
    assert 'internal-id' not in label and 'events' not in label
    assert filter_session_rows([row, empty]) == [row]
    assert filter_session_rows([row], query='project') == [row]
    assert filter_session_rows([row], workspace='other') == []
    assert relative_time(100, now=120) == '刚刚'
    assert relative_time(100, now=220) == '2 分钟前'


def test_picker_search_and_enter(tmp_path):
    import asyncio

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from cli_input import SessionPicker

    rows = [dict(id='first', title='修复代码', preview='修复代码', workspace=str(tmp_path), updated=10),
            dict(id='second', title='检查测试', preview='检查测试', workspace='other', updated=20)]

    async def exercise():
        with create_pipe_input() as pipe:
            picker = SessionPicker(rows, workspace=tmp_path, input=pipe, output=DummyOutput())
            assert [row['id'] for row in picker.visible()] == ['second', 'first']
            picker.show_all = False
            assert [row['id'] for row in picker.visible()] == ['first']
            task = asyncio.create_task(picker.application.run_async())
            pipe.send_bytes(b'\x01')  # toggle all workspaces
            pipe.send_text('检查\r')
            assert await asyncio.wait_for(task, timeout=3) == 'second'

    asyncio.run(exercise())
