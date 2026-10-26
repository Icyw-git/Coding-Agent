"""目录级持久授权：账本、策略放宽范围、API 与撤销入口。"""

import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import exec_policy
import grants
from exec_policy import ALLOW, ASK, DENY, PATH_OUTSIDE, PATH_SECRET, PATH_SYSTEM, Policy


@pytest.fixture()
def roots(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    outside = tmp_path / 'notes'
    outside.mkdir()
    return workspace, outside


# ============================================================
# 账本
# ============================================================

def test_add_list_find_and_remove(roots):
    _, outside = roots
    record = grants.add_path(outside, note='长期资料', created_by='cli',
                             session_id='s1')

    assert record['path'] == str(outside.resolve())
    assert record['scope'] == 'write'
    assert record['expires'] == ''
    assert grants.find_path(outside)['note'] == '长期资料'
    rows = grants.list_grants()
    assert [row['path'] for row in rows] == [str(outside.resolve())]
    assert rows[0]['active'] is True

    removed = grants.remove_path(outside)
    assert removed['created_by'] == 'cli'
    assert grants.list_grants() == []
    assert grants.remove_path(outside) is None


def test_add_is_idempotent_and_preserves_created_at(roots):
    _, outside = roots
    first = grants.add_path(outside, note='一')
    again = grants.add_path(outside, note='二', expires=date.today().isoformat())

    assert again['created_at'] == first['created_at']
    rows = grants.list_grants()
    assert len(rows) == 1 and rows[0]['note'] == '二'
    assert rows[0]['expires'] == date.today().isoformat()


def test_validation_rejects_unsafe_paths(tmp_path, monkeypatch):
    system = tmp_path / 'system'
    system.mkdir()
    monkeypatch.setattr(grants, 'system_roots', lambda: (system.resolve(),))
    a_file = tmp_path / 'file.txt'
    a_file.write_text('x', encoding='utf-8')

    for path, message in (
            ('', 'empty'),
            ('relative/dir', 'absolute'),
            (tmp_path / 'missing', 'does not exist'),
            (a_file, 'not a directory'),
            (system, 'system directory'),
            (Path(tmp_path.anchor), 'filesystem root')):
        with pytest.raises(grants.GrantError, match=message):
            grants.add_path(path)


def test_expiry_validation_and_active_filter(roots):
    _, outside = roots
    with pytest.raises(grants.GrantError, match='past'):
        grants.add_path(outside, expires=(date.today() - timedelta(days=1)).isoformat())
    with pytest.raises(grants.GrantError, match='YYYY-MM-DD'):
        grants.add_path(outside, expires='next week')

    grants._save([{
        'path': str(outside.resolve()), 'scope': 'write',
        'expires': (date.today() - timedelta(days=2)).isoformat(),
        'created_by': '', 'note': '', 'session_id': '', 'created_at': '',
    }, {
        'path': str((outside / 'sub').resolve()), 'scope': 'write',
        'expires': '', 'created_by': '', 'note': '', 'session_id': '',
        'created_at': '',
    }])
    rows = grants.list_grants()
    assert [row['active'] for row in rows] == [False, True]
    assert grants.active_write_paths() == ((outside / 'sub').resolve(),)


def test_revoke_for_session_only_removes_that_session(roots, tmp_path):
    _, outside = roots
    other = tmp_path / 'other'
    other.mkdir()
    grants.add_path(outside, session_id='s1')
    grants.add_path(other, session_id='s2')

    removed = grants.revoke_for_session('s1')
    assert [row['path'] for row in removed] == [str(outside.resolve())]
    assert [row['path'] for row in grants.list_grants()] == [str(other.resolve())]
    assert grants.revoke_for_session('') == []


def test_corrupt_ledger_fails_closed_and_blocks_mutation(roots):
    _, outside = roots
    grants.DURABLE_PATH.write_text('{not json', encoding='utf-8')

    assert grants.active_write_paths() == ()
    assert grants.list_grants() == []
    with pytest.raises(grants.GrantError):
        grants.add_path(outside)
    with pytest.raises(grants.GrantError):
        grants.remove_path(outside)


def test_unknown_version_is_treated_as_corrupt(roots):
    _, outside = roots
    grants.DURABLE_PATH.write_text(
        json.dumps({'version': 99, 'paths': []}), encoding='utf-8')

    assert grants.active_write_paths() == ()
    with pytest.raises(grants.GrantError, match='version'):
        grants.add_path(outside)


def test_malformed_records_are_skipped(roots, tmp_path):
    _, outside = roots
    grants.DURABLE_PATH.write_text(json.dumps({
        'version': 1,
        'paths': [
            'not-a-dict',
            {'path': 'relative', 'scope': 'write'},
            {'path': str(outside.resolve()), 'scope': 'read'},
            {'path': str(outside.resolve()), 'scope': 'write',
             'expires': 'whenever'},
            {'path': str(outside.resolve()), 'scope': 'write'},
        ],
    }), encoding='utf-8')

    rows = grants.list_grants()
    assert [row['path'] for row in rows] == [str(outside.resolve())]


# ============================================================
# 策略层
# ============================================================

def test_write_outside_is_relaxed_only_for_granted_tree(roots):
    workspace, outside = roots
    grant = outside.resolve()

    def decide(path, write_grants):
        return Policy().evaluate(
            'write', {'path': str(path)}, path_zone=PATH_OUTSIDE,
            workspace=str(workspace), write_grants=write_grants)

    assert decide(outside / 'a.txt', ()).action == ASK
    covered = decide(outside / 'a.txt', (grant,))
    assert covered.action == ALLOW and covered.code == 'path_grant'
    assert decide(outside / 'sub' / 'a.txt', (grant,)).action == ALLOW
    assert decide(workspace.parent / 'elsewhere.txt', (grant,)).action == ASK


def test_grants_do_not_override_secret_or_system_zones(roots):
    workspace, outside = roots
    grant = outside.resolve()
    secret = outside / '.env'
    secret.write_text('TOKEN=x', encoding='utf-8')

    assert Policy().evaluate(
        'write', {'path': str(secret)}, path_zone=PATH_SECRET,
        workspace=str(workspace), write_grants=(grant,)).action == DENY
    assert Policy().evaluate(
        'write', {'path': str(secret)}, path_zone=PATH_SYSTEM,
        workspace=str(workspace), write_grants=(grant,)).action == DENY


def test_read_only_tier_still_asks_with_a_grant(roots):
    workspace, outside = roots
    decision = Policy('read-only').evaluate(
        'write', {'path': str(outside / 'a.txt')}, path_zone=PATH_OUTSIDE,
        workspace=str(workspace), write_grants=(outside.resolve(),))

    assert decision.action == ASK and decision.code == 'sandbox_read_only'


def test_apply_patch_requires_every_path_covered(roots):
    workspace, outside = roots
    grant = outside.resolve()
    inside_patch = workspace / 'b.txt'
    outside_patch = outside / 'a.txt'

    def decide(paths):
        return Policy().evaluate(
            'apply_patch', {'path': str(paths[0])}, path_zone=PATH_OUTSIDE,
            workspace=str(workspace), write_grants=(grant,), paths=paths)

    assert decide([outside_patch, inside_patch]).action == ALLOW
    assert decide([outside_patch, workspace.parent / 'other.txt']).action == ASK


def test_bash_outside_write_uses_grants(roots, monkeypatch):
    workspace, outside = roots
    monkeypatch.setattr(exec_policy, 'system_roots', lambda: ())

    def decide(command, write_grants=()):
        return Policy().evaluate(
            'bash', {'command': command}, deny_list=[], destructive=[],
            secrets=('.env',), workspace=str(workspace),
            write_grants=write_grants)

    target = outside / 'out.txt'
    assert decide(f'echo hi > "{target}"').action == ASK
    decision = decide(f'echo hi > "{target}"', (outside.resolve(),))
    assert decision.action == ALLOW
    # 授权目录里的系统/秘密目标不被放宽（这里用系统分区验证重新分类）
    monkeypatch.setattr(exec_policy, 'system_roots', lambda: (outside.resolve(),))
    assert decide(f'echo hi > "{target}"', (outside.resolve(),)).action == ASK


def test_bash_grant_does_not_cover_sibling_or_parent(roots, monkeypatch):
    workspace, outside = roots
    monkeypatch.setattr(exec_policy, 'system_roots', lambda: ())
    sibling = workspace.parent / 'sibling'
    sibling.mkdir()

    decision = Policy().evaluate(
        'bash', {'command': f'echo hi > "{sibling / "x.txt"}"'},
        deny_list=[], destructive=[], secrets=('.env',),
        workspace=str(workspace), write_grants=(outside.resolve(),))
    assert decision.action == ASK


def test_hooks_inject_active_grants_only_in_workspace_write(roots, monkeypatch):
    import harness
    import hooks

    workspace, outside = roots
    grants.add_path(outside)
    monkeypatch.setattr(harness, 'get_workdir', lambda: workspace)
    monkeypatch.setattr(harness, 'DENY_LIST', [])
    monkeypatch.setattr(harness, 'DESTRUCTIVE', [])

    decision = hooks.evaluate_call('write', {'path': str(outside / 'a.txt')})
    assert decision.action == ALLOW and decision.code == 'path_grant'

    monkeypatch.setenv('HARNESS_SANDBOX_MODE', 'read-only')
    assert hooks.evaluate_call(
        'write', {'path': str(outside / 'a.txt')}).code == 'sandbox_read_only'


# ============================================================
# API / 网关客户端 / 撤销工具
# ============================================================

fastapi_available = importlib.util.find_spec('fastapi') is not None


@pytest.fixture()
def api_client(tmp_path):
    if not fastapi_available:
        pytest.skip('FastAPI is an optional dependency')
    from fastapi.testclient import TestClient

    from harness_api import create_app

    app = create_app(object(), api_token='t',
                     task_root=tmp_path / 'tasks', workspace=tmp_path,
                     rate_limit=10000)
    with TestClient(app) as client:
        yield client


def _headers():
    return {'Authorization': 'Bearer t'}


def test_api_add_list_remove_and_audit(api_client, roots):
    _, outside = roots

    response = api_client.post('/v1/grants', headers=_headers(), json={
        'path': str(outside), 'note': 'api', 'by': 'test',
        'expires': (date.today() + timedelta(days=5)).isoformat()})
    assert response.status_code == 201
    record = response.json()
    assert record['path'] == str(outside.resolve())

    listed = api_client.get('/v1/grants', headers=_headers()).json()['grants']
    assert [row['path'] for row in listed] == [str(outside.resolve())]

    bad = api_client.post('/v1/grants', headers=_headers(),
                          json={'path': 'relative'})
    assert bad.status_code == 400
    assert 'absolute' in bad.json()['detail']

    removed = api_client.post('/v1/grants/remove', headers=_headers(),
                              json={'path': str(outside), 'by': 'test'})
    assert removed.status_code == 200
    assert removed.json()['removed']['note'] == 'api'
    assert api_client.post('/v1/grants/remove', headers=_headers(),
                           json={'path': str(outside)}).status_code == 404


def test_api_audits_grant_changes_in_session_log(api_client, roots):
    import harness

    _, outside = roots
    log = harness.SESSIONS_DIR / 'session_sess-audit.jsonl'
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('', encoding='utf-8')

    api_client.post('/v1/grants', headers=_headers(), json={
        'path': str(outside), 'session_id': 'sess-audit', 'by': 'test'})
    api_client.post('/v1/grants/remove', headers=_headers(), json={
        'path': str(outside), 'session_id': 'sess-audit', 'by': 'test'})

    events = harness.SessionEventStore.open(log).read_events()
    assert [event['type'] for event in events] == ['grant_added', 'grant_removed']
    assert events[0]['path'] == str(outside.resolve())


def test_revoke_endpoint_also_drops_session_grants(api_client, roots):
    _, outside = roots
    api_client.post('/v1/grants', headers=_headers(), json={
        'path': str(outside), 'session_id': 'sess-g', 'by': 'test'})

    response = api_client.post('/v1/permissions/revoke', headers=_headers(),
                               json={'session_id': 'sess-g'})

    assert response.status_code == 200
    assert response.json()['grants_revoked'] == 1
    assert api_client.get('/v1/grants', headers=_headers()).json()['grants'] == []


def test_gateway_client_grant_methods(api_client, roots):
    from gateway_sim import GatewayClient

    _, outside = roots
    client = GatewayClient('http://test', 't',
                           transport=_asgi_transport(api_client))
    record = client.add_grant(str(outside), note='gw', session_id='s')
    assert client.list_grants()[0]['path'] == record['path']
    assert client.remove_grant(str(outside), session_id='s')['path'] == record['path']
    client.close()


def _asgi_transport(test_client):
    """把 TestClient 包成 httpx MockTransport，供 GatewayClient 走真实路由。"""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ('content-length', 'host')}
        raw = test_client.request(request.method, request.url.path,
                                  params=dict(request.url.params),
                                  content=request.content, headers=headers)
        return httpx.Response(raw.status_code, headers=dict(raw.headers),
                              content=raw.content)

    return httpx.MockTransport(handler)


def test_revoke_tool_reports_and_audits_session_grants(monkeypatch, roots):
    from types import SimpleNamespace

    import tool_sessions

    _, outside = roots
    grants.add_path(outside, session_id='sess-1')
    appended = []

    class Store:
        session_id = 'sess-1'

        def append(self, event, **payload):
            appended.append((event, payload))

    class Gate:
        def revoke_session(self, session_id):
            return 4

    monkeypatch.setattr(tool_sessions, '_gate', lambda: SimpleNamespace(
        GATE=Gate(), current_session_id=lambda: 'sess-1'))
    monkeypatch.setattr(tool_sessions, '_h', lambda: SimpleNamespace(
        active_checkpoint_manager=lambda: SimpleNamespace(session_store=Store())))

    message = tool_sessions.run_revoke_permissions()
    assert 'Revoked 4 permission grant(s)' in message
    assert '1 path grant(s)' in message
    assert appended and appended[0][0] == 'grant_removed'
    assert grants.list_grants() == []
