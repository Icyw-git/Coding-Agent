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
    import harness

    manager = SimpleNamespace(
        list=lambda: [dict(cp_id='cp_test', label='修改前', trigger='manual',
                           status='open', files={})], head=lambda: 'cp_test')
    output = harness.format_checkpoint_list(manager)
    assert '修改前 · 当前 · 尚未完成' in output
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
