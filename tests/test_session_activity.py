import os

from session_store import SessionEventStore


def test_creation_and_conversation_times_ignore_metadata_and_tools(tmp_path):
    store = SessionEventStore(tmp_path, session_id='legacy')
    # Older logs may start with a message rather than session_created.
    store.append('message', ts=100, message={'role': 'user', 'content': '任务'})
    store.append('message', ts=200, message={'role': 'assistant', 'content': None,
                                         'tool_calls': []})
    store.append('message', ts=300, message={'role': 'tool', 'content': 'result'})
    store.append('session_title', ts=400, title='任务主题')
    store.append('workspace_changed', ts=500, workspace=str(tmp_path))
    summary = SessionEventStore.open(store.path).session_summary()
    assert summary['created'] == 100
    assert summary['updated'] == 200


def test_empty_session_uses_creation_time(tmp_path):
    store = SessionEventStore(tmp_path)
    assert store.session_summary()['created'] == 0
    store.append('session_created', ts=100)
    store.append('workspace_changed', ts=500, workspace=str(tmp_path))
    assert store.session_summary()['created'] == 100
    assert store.session_summary()['updated'] == 100


def test_generic_confirmation_is_not_a_session_title(tmp_path):
    store = SessionEventStore(tmp_path, session_id='generic')
    store.append('session_title', ts=200, title='go')
    store.append('message', ts=300, message={'role': 'user', 'content': 'x'})
    assert store.session_summary()['title'] == '新会话'


def test_cli_and_runtime_sort_before_limit_by_message_time(tmp_path, monkeypatch):
    import harness
    from agent_runtime import AgentRuntime

    old = SessionEventStore(tmp_path, session_id='old')
    old.append('message', ts=100, message={'role': 'user', 'content': '旧任务'})
    recent = SessionEventStore(tmp_path, session_id='recent')
    recent.append('message', ts=200, message={'role': 'user', 'content': '新任务'})
    # A background title and a copied/touched log must not reorder sessions.
    old.append('session_title', ts=900, title='旧任务标题')
    os.utime(old.path, (9000, 9000))
    os.utime(recent.path, (1000, 1000))
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    assert harness.list_session_paths(1) == [recent.path]
    assert [row['id'] for row in harness.session_rows(1)] == ['recent']
    assert [row['id'] for row in harness.session_rows(1, query='旧任务')] == ['old']
    runtime = AgentRuntime(run_turn=lambda *a, **kw: '',
                           session_store_cls=SessionEventStore, sessions_dir=tmp_path)
    rows = runtime.list_sessions(limit=1)
    assert rows[0]['id'] == 'recent'
    assert rows[0]['updated'] == 200 and rows[0]['created'] == 200
