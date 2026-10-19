"""审批分级（会话级白名单）测试：飞书卡片「本次会话允许同类」→ 后续同类免审。

覆盖：GATE 白名单注册与命中、DENY_LIST 仍然优先、不同类仍需审批、
clear() 清空、飞书卡片 scope=session 全链路（HTTP resolve 透传）、
「同类」键一律由服务端按被审批内容重算（客户端传 klass 无效）。

运行: python test_approval_scope.py
"""
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pytest

import gateway_sim
import hooks
import feishu_gateway
from exec_policy import approval_class
from harness_api import create_app
from hooks import GATE
from agent_runtime import AgentRuntime
from session_store import SessionEventStore
from test_feishu_gateway import _sync_asgi_transport, _drain_stale_permissions


@pytest.fixture()
def remote_gate(monkeypatch):
    monkeypatch.setattr(GATE, 'backend', 'remote')
    _drain_stale_permissions()
    yield GATE
    GATE.clear()


def _fake_hooks_config(monkeypatch):
    class HarnessConfig:
        DENY_LIST = ['sudo']
        DESTRUCTIVE = ['rm ', 'del ']

    monkeypatch.setattr(hooks, '_h', lambda: HarnessConfig)


def _decide_async(command, results, key='allowed'):
    def waiter():
        results[key] = hooks.permission_hook('bash', {'command': command})
    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    for _ in range(100):
        if GATE.pending():
            break
        time.sleep(0.02)
    return thread


def test_approval_class_bash_uses_first_word():
    assert approval_class('bash', {'command': 'rm -rf x'}) == 'rm'
    assert approval_class('bash', {'command': '  DEL file'}) == 'del'
    assert approval_class('bash', {'command': ''}) == ''
    assert approval_class('write', {'path': 'a'}) == 'write'


def test_session_allowlist_skips_second_ask(remote_gate, monkeypatch):
    _fake_hooks_config(monkeypatch)
    results = {}
    thread = _decide_async('rm -rf build', results)
    GATE.resolve(GATE.pending()[0].req_id, True, by='test', scope='session')
    thread.join(timeout=2)
    assert results['allowed'] is None   # 第一次：审批后放行

    assert hooks.permission_hook('bash', {'command': 'rm -rf dist'}) is None
    assert not GATE.pending()           # 第二次：白名单命中，未再询问


def test_deny_list_wins_over_session_allowlist(remote_gate, monkeypatch):
    _fake_hooks_config(monkeypatch)
    GATE._session_allows.add('bash:sudo')
    result = hooks.permission_hook('bash', {'command': 'sudo rm x'})
    assert 'Dangerous command' in result   # 白名单不能越过黑名单


def test_different_class_still_requires_approval(remote_gate, monkeypatch):
    _fake_hooks_config(monkeypatch)
    GATE._session_allows.add('bash:rm')
    assert hooks.permission_hook('bash', {'command': 'rm -rf x'}) is None

    called = []
    original = GATE.decide

    def tracking(tool, args, reason):
        called.append(tool)
        return original(tool, args, reason)

    monkeypatch.setattr(GATE, 'decide', tracking)
    results = {}
    thread = _decide_async('del file', results)
    for _ in range(100):
        if called:
            break
        time.sleep(0.02)
    assert called == ['bash'], '不同类（del）应仍然走审批'
    GATE.resolve(GATE.pending()[0].req_id, False, by='test')
    thread.join(timeout=2)


def test_resolve_derives_the_class_from_the_approved_call(remote_gate, monkeypatch):
    """白名单的「同类」取自被审批的 args，不是调用方给的标签：
    批准一条 del 不能顺手预授权 rm。"""
    _fake_hooks_config(monkeypatch)
    results = {}
    thread = _decide_async('del file', results)
    GATE.resolve(GATE.pending()[0].req_id, True, by='test', scope='session')
    thread.join(timeout=2)
    assert GATE.session_allows('bash', {'command': 'del other'})
    assert not GATE.session_allows('bash', {'command': 'rm other'})


@pytest.mark.parametrize('command', [
    'python -c "print(1)"', 'pwsh -Command "Get-ChildItem"',
    'ssh host echo ok', 'docker run image', 'git push origin main',
])
def test_sensitive_commands_cannot_grant_session_wide_allow(remote_gate, monkeypatch,
                                                            command):
    _fake_hooks_config(monkeypatch)
    results = {}
    thread = _decide_async(command, results)
    for _ in range(100):
        if GATE.pending():
            break
        time.sleep(0.01)
    if GATE.pending():
        GATE.resolve(GATE.pending()[0].req_id, True, by='test', scope='session')
    thread.join(timeout=2)
    assert results['allowed'] is None
    assert not GATE._session_allows


def test_revoke_session_only_removes_that_sessions_grants(remote_gate):
    GATE._session_allows.update({'A|bash:rm', 'B|bash:rm'})
    assert GATE.revoke_session('A') == 1
    assert GATE._session_allows == {'B|bash:rm'}
    assert GATE.revoke_session() == 1
    assert not GATE._session_allows


def test_wrapped_command_only_whitelists_the_wrapped_action(remote_gate, monkeypatch):
    """批准 `cmd /c del a` 不免审 `cmd /c rd /s /q b`：同类键解一层包装。"""
    _fake_hooks_config(monkeypatch)
    results = {}
    thread = _decide_async('cmd /c "del a"', results)
    GATE.resolve(GATE.pending()[0].req_id, True, by='test', scope='session')
    thread.join(timeout=2)
    assert approval_class('bash', {'command': 'cmd /c "del a"'}) == 'del'
    assert GATE.session_allows('bash', {'command': 'cmd /c "del other"'})
    assert not GATE.session_allows('bash', {'command': 'cmd /c "rd /s /q build"'})


def test_clear_wipes_session_allowlist(remote_gate):
    GATE._session_allows.add('bash:rm')
    GATE.clear()
    assert not GATE.session_allows('bash', {'command': 'rm x'})


@pytest.fixture()
def api_app(tmp_path):
    runtime = AgentRuntime(
        run_turn=lambda messages, **kwargs: 'agent-reply-ok',
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path / 'sessions',
    )
    return create_app(runtime, api_token='t',
                      task_root=tmp_path / 'tasks', workspace=tmp_path,
                      rate_limit=10000)


def test_feishu_session_scope_button_end_to_end(api_app, tmp_path):
    """飞书点「本次会话允许同类」→ scope 透传 HTTP → GATE 白名单生效。"""
    api = gateway_sim.GatewayClient(
        'http://test', 't', transport=_sync_asgi_transport(api_app))

    class FakeTransport:
        def __init__(self):
            self.cards = []
            self.sent = []

        def send_approval_card(self, chat_id, prompt_md, req):
            self.cards.append((chat_id, prompt_md, req))
            return 'om-1'

        def send_text(self, chat_id, text):
            self.sent.append((chat_id, text))

    fake = FakeTransport()
    gw = feishu_gateway.FeishuGateway(
        api, transport=fake, bindings_path=tmp_path / 'bindings.json')
    gw.on_message('c1', 'hi')
    monkey = pytest.MonkeyPatch()
    monkey.setattr(GATE, 'backend', 'remote')
    try:
        _drain_stale_permissions()
        results = {}
        thread = _decide_async('rm -rf build', results)
        gw.poll_permissions()
        assert fake.cards, '应推送审批卡片'
        req = fake.cards[0][2]

        text, ok = gw.handle_card_action(
            None, {'approval': req['req_id'], 'allow': True,
                   'scope': 'session'}, 'om_1')
        assert ok and '已允许' in text
        thread.join(timeout=2)
        assert results['allowed'] is None
        assert GATE.session_allows('bash', {'command': 'rm -rf anything'})
    finally:
        monkey.undo()
        GATE.clear()
        api.close()


def test_http_resolve_ignores_a_client_supplied_class(api_app):
    """接口契约：多余的 klass 字段被忽略，白名单键仍按被审批的内容算。

    旧实现把客户端传来的 klass 直接写进白名单，于是「批准这条 del，
    顺带 klass=rm」可以给后续 rm 开门 —— 审批方给自己提权。
    """
    from fastapi.testclient import TestClient

    client = TestClient(api_app)
    headers = {'Authorization': 'Bearer t'}
    req = GATE.request('bash', {'command': 'del file'}, 'potentially destructive')
    response = client.post(f'/v1/permissions/{req.req_id}/resolve',
                           json={'allow': True, 'scope': 'session', 'klass': 'rm'},
                           headers=headers)

    assert response.status_code == 200
    assert GATE.session_allows('bash', {'command': 'del other'})
    assert not GATE.session_allows('bash', {'command': 'rm other'})
