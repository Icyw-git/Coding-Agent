"""飞书域名策略入口：命令、卡片与撤销按钮。

复用 ``test_feishu_gateway`` 的网关构造方式，但在这里独立定义 fixture：
跨测试模块 import fixture 会与测试函数的同名参数冲突，也会让 ruff 报重定义。
"""
import pytest

import feishu_domain_card as card
import gateway_sim
from feishu_gateway import FeishuGateway
from hooks import GATE as permission_gate
from test_feishu_gateway import FakeTransport, _sync_asgi_transport


@pytest.fixture()
def gateway(tmp_path, monkeypatch):
    from agent_runtime import AgentRuntime
    from harness_api import create_app
    from session_store import SessionEventStore

    legacy = AgentRuntime(
        run_turn=lambda messages, **kwargs: 'agent-reply-ok',
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path / 'sessions',
    )
    app = create_app(legacy, api_token='t', task_root=tmp_path / 'tasks',
                     workspace=tmp_path, rate_limit=10000)
    monkeypatch.setattr(permission_gate, 'backend', 'remote')
    api = gateway_sim.GatewayClient(
        'http://test', 't', transport=_sync_asgi_transport(app))
    fake = FakeTransport()
    gw = FeishuGateway(api, transport=fake,
                       bindings_path=tmp_path / 'bindings.json')
    yield gw, fake
    api.close()
    monkeypatch.setattr(permission_gate, 'backend', 'console')


def _domain_button(payload, action, host):
    for element in payload['elements']:
        for button in (element.get('actions') or []):
            value = button.get('value') or {}
            if (value.get('domain_action') == action
                    and value.get('domain_host') == host):
                return value
    raise AssertionError(f'no {action} button for {host}')


def test_domain_card_lists_allow_and_deny():
    rows = [{'host': '*.example.com', 'scope': 'allow', 'active': True},
            {'host': 'ads.example.com', 'scope': 'deny', 'active': True}]
    payload = card.domain_card(rows, token='t0k3n')

    text = str(payload)
    assert '✅ 允许  *.example.com' in text
    assert '⛔ 拒绝  ads.example.com' in text
    assert '允许 1 · 拒绝 1' in text
    assert '*.example.com 不含 example.com 本身' in text


def test_domain_card_empty_state_explains_default():
    payload = card.domain_card([])
    text = str(payload)
    assert '没有域名规则' in text
    assert '仍会逐次询问' in text


def test_domain_command_adds_and_lists(gateway):
    gw, fake = gateway

    gw.on_message('chat-dom', '/domains allow "*.example.com" note docs')

    assert '已添加域名规则' in fake.last()
    assert [row['host'] for row in gw.api.list_domains()] == ['*.example.com']


def test_domain_command_deny_and_remove(gateway):
    gw, fake = gateway
    gw.on_message('chat-dom2', '/domains deny ads.example.com')
    assert '硬拒' in fake.last()

    gw.on_message('chat-dom2', '/domains remove ads.example.com')
    assert '已撤销域名规则' in fake.last()
    assert gw.api.list_domains() == []


def test_domain_command_rejects_bad_host(gateway):
    gw, fake = gateway
    gw.on_message('chat-dom3', '/domains allow *')
    assert 'host' in fake.last() or '域名' in fake.last()
    assert gw.api.list_domains() == []


def test_domain_card_revoke_button_checks_view_token(gateway):
    gw, fake = gateway
    gw.on_message('chat-dom4', '/domains allow docs.example.com')
    gw.on_message('chat-dom4', '/domains')
    host = gw.api.list_domains()[0]['host']
    value = _domain_button(fake.cards[-1][1], 'remove', host)

    toast, ok = gw.handle_card_action(
        'ou_me', {**value, 'domain_view': 'stale'}, None, chat_id='chat-dom4')
    assert not ok and '过期' in toast

    toast, ok = gw.handle_card_action('ou_me', value, None, chat_id='chat-dom4')
    assert ok
    gw._handle_domain_action('chat-dom4', value)
    assert '已撤销域名规则' in fake.last()
    assert gw.api.list_domains() == []
    assert not gw._domain_view_ok('chat-dom4', value)


def test_domain_card_pages(gateway):
    gw, fake = gateway
    for index in range(8):
        gw.on_message('chat-dom5', f'/domains allow h{index}.example.com')
    gw.on_message('chat-dom5', '/domains')

    payload = fake.cards[-1][1]
    page_values = [button['value'] for element in payload['elements']
                   for button in (element.get('actions') or [])
                   if (button.get('value') or {}).get('domain_action') == 'page']
    assert page_values and page_values[0]['page'] == 1
