"""审计查询：事件白名单、字段裁剪、跨会话聚合与 API 契约。"""
import json

import pytest
from fastapi.testclient import TestClient

import audit

TOKEN = 'audit-token'


def _write(path, events):
    path.write_text(
        ''.join(json.dumps(event, ensure_ascii=False) + '\n' for event in events),
        encoding='utf-8')


def _session(session_id, events):
    return {'v': 1, 'session_id': session_id, **events}


def test_only_whitelisted_events_are_projected():
    assert audit.project({'type': 'message', 'seq': 1}) is None
    assert audit.project({'type': 'tool_started', 'seq': 2}) is None

    event = audit.project({'type': 'desktop_action', 'seq': 3, 'ts': 10.0,
                           'session_id': 's1', 'action': 'type',
                           'process': 'notepad', 'text': '私密内容'})
    assert event is not None
    # 白名单里没有 text，输入内容不会出现在审计流里
    assert 'text' not in event.fields
    assert event.fields['action'] == 'type'


def test_long_fields_are_clipped():
    event = audit.project({'type': 'desktop_action_failed', 'seq': 1, 'ts': 1.0,
                           'reason': 'x' * 500})
    assert len(event.fields['reason']) <= audit.MAX_FIELD_CHARS + 1


def test_collect_across_sessions_newest_first(tmp_path):
    _write(tmp_path / 'session_a.jsonl', [
        _session('a', {'type': 'screen_captured', 'seq': 1, 'ts': 100.0,
                       'sha1': 'aaa'}),
        _session('a', {'type': 'message', 'seq': 2, 'ts': 101.0}),
        _session('a', {'type': 'grant_added', 'seq': 3, 'ts': 102.0,
                       'path': 'D:/x'}),
    ])
    _write(tmp_path / 'session_b.jsonl', [
        _session('b', {'type': 'desktop_action', 'seq': 1, 'ts': 200.0,
                       'action': 'click'}),
    ])

    events = audit.collect(tmp_path)
    assert [event['type'] for event in events] == [
        'desktop_action', 'grant_added', 'screen_captured']
    assert {event['session_id'] for event in events} == {'a', 'b'}


def test_collect_filters_by_type_and_since(tmp_path):
    _write(tmp_path / 'session_a.jsonl', [
        _session('a', {'type': 'screen_captured', 'seq': 1, 'ts': 100.0}),
        _session('a', {'type': 'grant_added', 'seq': 2, 'ts': 300.0,
                       'path': 'D:/x'}),
    ])

    assert [event['type'] for event in audit.collect(tmp_path, since=200.0)] == [
        'grant_added']
    assert [event['type'] for event in
            audit.collect(tmp_path, types=['screen_captured'])] == [
        'screen_captured']


def test_collect_skips_corrupt_lines_and_files(tmp_path):
    path = tmp_path / 'session_bad.jsonl'
    path.write_text(
        '{"type": "grant_added", "seq": 1, "ts": 5.0, "path": "D:/x"}\n'
        'this is not json\n'
        '{"type": "screen_captured", "seq": 2, "ts": 6.0}\n',
        encoding='utf-8')

    events = audit.collect(tmp_path)
    assert [event['type'] for event in events] == ['screen_captured', 'grant_added']


def test_collect_reports_session_title(tmp_path):
    _write(tmp_path / 'session_t.jsonl', [
        _session('t', {'type': 'session_title', 'seq': 1, 'ts': 1.0,
                       'title': '部署检查'}),
        _session('t', {'type': 'grant_added', 'seq': 2, 'ts': 2.0}),
    ])
    assert audit.collect(tmp_path)[0]['title'] == '部署检查'


def test_since_parsing():
    assert audit.since_from('') == 0.0
    assert audit.since_from('garbage') == 0.0
    assert audit.since_from('1000') == 1000.0
    assert audit.since_from('2026-01-02T03:04:05') > 0


# ---------- API ----------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    import harness
    from harness_api import create_app

    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    monkeypatch.setattr(harness, 'SESSIONS_DIR', sessions)
    _write(sessions / 'session_api.jsonl', [
        _session('api', {'type': 'clipboard_read', 'seq': 1, 'ts': 50.0,
                         'bytes': 12, 'chars': 10}),
        _session('api', {'type': 'desktop_action', 'seq': 2, 'ts': 60.0,
                         'action': 'type', 'steps': 2}),
    ])
    app = create_app(object(), api_token=TOKEN, task_root=tmp_path / 'tasks',
                     workspace=tmp_path, rate_limit=10000)
    with TestClient(app) as test_client:
        yield test_client


def _headers():
    return {'Authorization': f'Bearer {TOKEN}'}


def test_api_lists_audit_events(client):
    response = client.get('/v1/audit', headers=_headers())
    assert response.status_code == 200
    payload = response.json()
    assert [event['type'] for event in payload['events']] == [
        'desktop_action', 'clipboard_read']
    assert payload['counts'] == {'desktop_action': 1, 'clipboard_read': 1}


def test_api_filters_by_type(client):
    response = client.get('/v1/audit', headers=_headers(),
                          params={'types': 'clipboard_read'})
    assert [event['type'] for event in response.json()['events']] == [
        'clipboard_read']


def test_api_rejects_unknown_type_and_bad_since(client):
    unknown = client.get('/v1/audit', headers=_headers(),
                         params={'types': 'nope'})
    assert unknown.status_code == 400
    assert 'nope' in unknown.json()['detail']

    bad = client.get('/v1/audit', headers=_headers(), params={'since': 'yesterday-ish'})
    assert bad.status_code == 400


def test_api_requires_token(client):
    assert client.get('/v1/audit').status_code == 401


def test_desktop_list_is_audited():
    """新增的桌面事件必须登记进白名单，否则审计里看不到「它列过哪些窗口」。"""
    assert 'desktop_list_windows' in audit.AUDIT_EVENTS


def test_desktop_list_projection_keeps_only_counts():
    event = audit.project({'type': 'desktop_list_windows', 'seq': 1, 'ts': 1.0,
                           'count': 5, 'total': 23,
                           'windows': [{'title': 'secret document'}]})
    assert event.fields == {'count': 5, 'total': 23}
    assert 'windows' not in event.fields      # 窗口标题不进审计流
