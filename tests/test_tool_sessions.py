"""tool_sessions：会话搜索、改名与撤销授权的工具层（依赖按名字替换）。"""
from types import SimpleNamespace

import tool_sessions


def test_find_sessions_rejects_empty_query(monkeypatch):
    monkeypatch.setattr(tool_sessions.SessionEventStore, 'search_all',
                        lambda *args: [])

    assert 'must not be empty' in tool_sessions.run_find_sessions('  ')


def test_find_sessions_formats_hits_and_clamps_limit(monkeypatch):
    captured = {}

    def search(directory, query, limit):
        captured.update(directory=directory, query=query, limit=limit)
        return [{'id': 'abcdef1234567890', 'title': '项目复盘',
                 'workspace': 'D:/work', 'matched': '用户: 上次的结论'}]

    monkeypatch.setattr(tool_sessions.SessionEventStore, 'search_all', search)
    monkeypatch.setattr(tool_sessions, '_h',
                        lambda: SimpleNamespace(SESSIONS_DIR='D:/sessions'))

    output = tool_sessions.run_find_sessions('复盘', limit=99)

    assert captured['limit'] == 20
    assert 'abcdef123456' in output and '项目复盘' in output
    assert '上次的结论' in output


def test_find_sessions_reports_no_hits(monkeypatch):
    monkeypatch.setattr(tool_sessions.SessionEventStore, 'search_all',
                        lambda *args: [])
    monkeypatch.setattr(tool_sessions, '_h',
                        lambda: SimpleNamespace(SESSIONS_DIR='D:/sessions'))

    assert 'No sessions contain' in tool_sessions.run_find_sessions('不存在')


def test_rename_session_writes_user_title(monkeypatch):
    calls = []
    store = SimpleNamespace(append_session_title=lambda title, **kw: calls.append((title, kw)))
    monkeypatch.setattr(
        tool_sessions, '_h',
        lambda: SimpleNamespace(active_checkpoint_manager=lambda: SimpleNamespace(
            session_store=store)))

    output = tool_sessions.run_rename_session('  ' + 'x' * 100 + '  ')

    assert 'Renamed current session' in output
    assert calls and len(calls[0][0]) == 80
    assert calls[0][1] == {'source': 'user'}


def test_rename_session_requires_active_session(monkeypatch):
    monkeypatch.setattr(
        tool_sessions, '_h',
        lambda: SimpleNamespace(active_checkpoint_manager=lambda: None))

    assert 'no active session' in tool_sessions.run_rename_session('标题')


def test_rename_session_rejects_blank_title(monkeypatch):
    monkeypatch.setattr(
        tool_sessions, '_h',
        lambda: SimpleNamespace(active_checkpoint_manager=lambda: None))

    assert 'must not be empty' in tool_sessions.run_rename_session('   ')


def test_revoke_permissions_uses_current_session(monkeypatch):
    seen = {}

    class Gate:
        def revoke_session(self, session_id):
            seen['session_id'] = session_id
            return 3

    monkeypatch.setattr(tool_sessions, '_gate', lambda: SimpleNamespace(
        GATE=Gate(), current_session_id=lambda: 'sess-1'))

    assert tool_sessions.run_revoke_permissions() == (
        'Revoked 3 permission grant(s) for this session.')
    assert seen['session_id'] == 'sess-1'


def test_revoke_permissions_reports_errors(monkeypatch):
    class Gate:
        def revoke_session(self, session_id):
            raise RuntimeError('locked')

    monkeypatch.setattr(tool_sessions, '_gate', lambda: SimpleNamespace(
        GATE=Gate(), current_session_id=lambda: ''))

    assert 'Error: RuntimeError: locked' in tool_sessions.run_revoke_permissions()
