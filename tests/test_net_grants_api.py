"""域名策略的 API 面：增删、审计、撤销与策略生效。"""
import pytest
from fastapi.testclient import TestClient

import net_grants

TOKEN = 'test-token'


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    from harness_api import create_app

    app = create_app(object(), api_token=TOKEN, task_root=tmp_path / 'tasks',
                     workspace=tmp_path, rate_limit=10000)
    with TestClient(app) as client:
        yield client


def _auth():
    return {'Authorization': f'Bearer {TOKEN}'}


def test_api_add_list_remove(api_client):
    client = api_client

    created = client.post('/v1/net-grants', headers=_auth(),
                          json={'host': '*.example.com', 'note': 'docs'})
    assert created.status_code == 201
    assert created.json()['host'] == '*.example.com'
    assert created.json()['scope'] == 'allow'

    listed = client.get('/v1/net-grants', headers=_auth())
    assert [row['host'] for row in listed.json()['domains']] == ['*.example.com']

    removed = client.post('/v1/net-grants/remove', headers=_auth(),
                          json={'host': '*.example.com'})
    assert removed.status_code == 200
    assert client.get('/v1/net-grants', headers=_auth()).json()['domains'] == []


def test_api_rejects_invalid_domains(api_client):
    client = api_client

    for payload in ({'host': '*'},
                    {'host': 'http://x.com/path'},
                    {'host': 'x.com', 'scope': 'maybe'},
                    {'host': 'x.com', 'expires': '2000-01-01'}):
        response = client.post('/v1/net-grants', headers=_auth(), json=payload)
        assert response.status_code in (400, 422), payload


def test_api_remove_missing_returns_404(api_client):
    response = api_client.post('/v1/net-grants/remove', headers=_auth(),
                               json={'host': 'nope.example.com'})
    assert response.status_code == 404


def test_api_audits_domain_changes_in_session_log(api_client, tmp_path, monkeypatch):
    import harness

    session_dir = tmp_path / 'sessions'
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(harness, 'SESSIONS_DIR', session_dir)
    path = session_dir / 'session_sess-net.jsonl'
    harness.SessionEventStore.open(path).append_message(
        {'role': 'user', 'content': 'grant a domain'})

    client = api_client
    client.post('/v1/net-grants', headers=_auth(),
                json={'host': 'docs.example.com', 'session_id': 'sess-net'})
    client.post('/v1/net-grants/remove', headers=_auth(),
                json={'host': 'docs.example.com', 'session_id': 'sess-net'})

    events = harness.SessionEventStore.open(path).read_events()
    types = [event['type'] for event in events
             if event['type'].startswith('domain_')]
    assert types == ['domain_added', 'domain_removed']


def test_revoke_permissions_drops_session_domains(api_client):
    """会话撤销只收回本会话创建的域名规则，人手动加的全局规则保留。"""
    net_grants.add('temp.example.com', session_id='sess-a')
    net_grants.add('other.example.com', session_id='sess-b')
    net_grants.add('global.example.com')

    revoked = api_client.post('/v1/permissions/revoke', headers=_auth(),
                              json={'session_id': 'sess-a', 'by': 'test'})
    assert revoked.status_code == 200
    assert revoked.json()['domains_revoked'] == 1
    assert {row['host'] for row in net_grants.list_rules()} == {
        'other.example.com', 'global.example.com'}


def test_domain_rule_changes_approval_outcome(api_client, monkeypatch):
    """名单真的影响策略判定：allow 免审、deny 直接拒（不用改档位）。"""
    import hooks

    url = 'https://docs.example.com/page'
    assert hooks.evaluate_call('web_fetch', {'url': url}).action == 'ask'

    net_grants.add('*.example.com')
    assert hooks.evaluate_call('web_fetch', {'url': url}).action == 'allow'

    net_grants.add('docs.example.com', scope='deny')
    decision = hooks.evaluate_call('web_fetch', {'url': url})
    assert decision.action == 'deny' and decision.code == 'domain_denied'
