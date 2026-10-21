from pathlib import Path
from types import SimpleNamespace


def test_status_is_readable_without_internal_ids(tmp_path, monkeypatch):
    import harness

    store = harness.SessionEventStore(tmp_path, session_id='internal-session-id')
    store.append_message({'role': 'user', 'content': '改进终端输出'})
    monkeypatch.setattr(harness, 'get_checkpoint_manager',
                        lambda _: SimpleNamespace(list=lambda **kwargs: []))
    monkeypatch.setattr(harness, 'list_tasks', lambda: [])
    output = harness.format_status(store)
    assert '改进终端输出' in output
    assert '工作区：' in output and '模型：' in output
    assert 'internal-session-id' not in output and 'events=' not in output


def test_checkpoint_listing_keeps_actionable_reference():
    import time

    import harness

    manager = SimpleNamespace(
        list=lambda: [dict(cp_id='cp_test', label='修改前', trigger='manual',
                           status='open', files={}, created_at=time.time() - 300)],
        head=lambda: 'cp_test')
    output = harness.format_checkpoint_list(manager)
    assert '修改前 · 当前 · 尚未完成' in output
    assert '5 分钟前' in output   # 快照时间锚点：能分辨「刚才」与「两小时前」
    assert '/rewind cp_test' in output
    assert 'files=' not in output


def test_restore_preview_keeps_warnings_and_scope():
    import harness

    plan = dict(cp_id='cp_test', label='修改前', scope='all', chain=[], actions=[],
                conflicts=['a.py'], unverified=['b.py'], drift=['c.py'],
                side_effects={}, ok=False, reason='不能安全恢复')
    output = harness.format_restore_report(plan)
    for text in ('a.py', 'b.py', 'c.py', '不能安全恢复', '不随回溯撤销', '会话与文件'):
        assert text in output
    assert 'chain=' not in output


def test_help_overview_keeps_flags_in_details_only():
    from cli_commands import format_command_help

    assert '--force' not in format_command_help()
    assert '--force' in format_command_help('rewind')


def test_workspace_diff_reports_status_and_line_changes(monkeypatch, tmp_path):
    import harness
    import workspace_diff

    def fake_git(args, **kwargs):
        if 'rev-parse' in args:
            return SimpleNamespace(returncode=0, stdout=f'{tmp_path}\n')
        if 'status' in args:
            return SimpleNamespace(returncode=0, stdout=' M src/app.py\0')
        if 'diff' in args:
            return SimpleNamespace(
                returncode=0,
                stdout=('diff --git a/src/app.py b/src/app.py\n'
                        '--- a/src/app.py\n+++ b/src/app.py\n'
                        '@@ -1 +1 @@\n-old\n+new\n'))
        raise AssertionError(f'unexpected git call: {args}')

    monkeypatch.setattr(workspace_diff.subprocess, 'run', fake_git)
    output = harness.format_workspace_diff(tmp_path)
    assert 'src/app.py' in output and '文件状态' in output
    assert '逐行改动' in output and '+new' in output and '-old' in output

    def clean_git(args, **kwargs):
        if 'rev-parse' in args:
            return SimpleNamespace(returncode=0, stdout=f'{tmp_path}\n')
        return SimpleNamespace(returncode=0, stdout='')

    monkeypatch.setattr(workspace_diff.subprocess, 'run', clean_git)
    assert '工作区干净' in harness.format_workspace_diff(tmp_path)


def test_model_command_does_not_expose_api_key(monkeypatch, capsys):
    import harness

    monkeypatch.setenv('LLM_MODEL_ID', 'test-model')
    monkeypatch.setenv('LLM_BASE_URL', 'https://example.test/v1')
    monkeypatch.setenv('LLM_API_KEY', 'secret-value')
    harness.handle_command('/model', [], None)
    output = capsys.readouterr().out
    assert 'test-model' in output and 'example.test' in output
    assert 'secret-value' not in output


def test_pending_approvals_show_the_command_and_hide_internal_ids(monkeypatch):
    import harness

    request = SimpleNamespace(
        req_id='perm_secret', tool='bash', reason='破坏性命令',
        args={'command': 'pytest -q\ngit push --force-with-lease'})
    monkeypatch.setattr(harness.hooks.GATE, 'pending', lambda: [request])
    output = harness.format_pending_approvals()
    assert '破坏性命令' in output
    assert 'bash' in output
    # 决议要看的就是这条命令：只列工具名等于让人盲批
    assert 'pytest -q' in output and 'git push --force-with-lease' in output
    assert 'perm_secret' not in output


def test_manual_compact_replaces_history_and_records_event(monkeypatch, tmp_path, capsys):
    import harness

    store = harness.SessionEventStore(tmp_path, session_id='compact')
    messages = [{'role': 'user', 'content': 'old request'}]
    store.append_message(messages[0])
    monkeypatch.setattr(harness, 'compact_history',
                        lambda current: [{'role': 'user', 'content': '[Compacted]\n\nsummary'}])
    harness.handle_command('/compact', messages, store)
    assert messages == [{'role': 'user', 'content': '[Compacted]\n\nsummary'}]
    assert any(event['type'] == 'compaction' for event in store.read_events())
    assert '完整记录已保存' in capsys.readouterr().out


def test_exec_tool_call_prints_activity_line_on_tty(monkeypatch):
    import io
    import sys

    import harness

    class _TTY(io.StringIO):
        def isatty(self):
            return True

    fake = _TTY()
    monkeypatch.setattr(sys, 'stdout', fake)
    tool_call = SimpleNamespace(
        id='call-1',
        function=SimpleNamespace(name='read', arguments='{"path":"a.py"}'))

    harness.exec_tool_call(tool_call, registry={'read': lambda path: 'ok'},
                           hooks=False,
                           on_activity=harness._print_tool_activity)

    output = fake.getvalue()
    assert '• read' in output and 'a.py' in output and '✓' in output


def test_exec_tool_call_is_silent_without_a_tty(capsys):
    import harness

    tool_call = SimpleNamespace(
        id='call-1',
        function=SimpleNamespace(name='read', arguments='{"path":"a.py"}'))

    harness.exec_tool_call(tool_call, registry={'read': lambda path: 'ok'},
                           hooks=False)

    assert capsys.readouterr().out == ''


def test_repl_header_shows_model_policy_and_shortcuts(tmp_path, monkeypatch, capsys):
    import cli_input
    import harness

    monkeypatch.setattr(cli_input, 'create_reader', lambda *args: lambda _: '/quit')
    monkeypatch.setenv('LLM_MODEL_ID', 'demo-model')
    store = harness.SessionEventStore(tmp_path, session_id='abcdef1234567890')

    harness.run_repl([], store)

    output = capsys.readouterr().out
    assert 'abcdef12' in output          # 会话只显示短前缀
    assert 'demo-model' in output
    assert '档位' in output and '沙箱' in output
    assert 'Ctrl-C' in output


def test_first_ctrl_c_hints_and_second_exits(tmp_path, monkeypatch, capsys):
    import cli_input
    import harness

    calls = []

    def reader(_prompt):
        calls.append(1)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_input, 'create_reader', lambda *args: reader)
    store = harness.SessionEventStore(tmp_path, session_id='s1')

    harness.run_repl([], store)

    output = capsys.readouterr().out
    assert '再按一次 Ctrl-C' in output
    assert '会话已结束' in output
    assert len(calls) == 2


def test_prompt_text_shows_workspace_and_mode(monkeypatch):
    import harness

    monkeypatch.setattr(harness, 'get_workdir', lambda: Path('D:/proj'))
    monkeypatch.setattr(harness, 'load_policy',
                        lambda: SimpleNamespace(sandbox_mode='read-only'))

    assert harness.prompt_text().strip() == 'proj [ro] >'


def test_prompt_text_includes_git_branch(tmp_path, monkeypatch):
    import harness

    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'HEAD').write_text(
        'ref: refs/heads/main\n', encoding='utf-8')
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    monkeypatch.setattr(harness, 'load_policy',
                        lambda: SimpleNamespace(sandbox_mode='workspace-write'))

    assert harness.prompt_text().strip() == f'{tmp_path.name} (main) [rw] >'
