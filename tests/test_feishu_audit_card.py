"""飞书审计入口：/audit 命令、卡片与翻页。"""
import pytest

import feishu_audit_card as card
import gateway_sim
from feishu_gateway import FeishuGateway
from hooks import GATE as permission_gate
from test_feishu_gateway import FakeTransport, _sync_asgi_transport


@pytest.fixture()
def gateway(tmp_path, monkeypatch):
    """与 test_feishu_gateway 相同的构造方式：真实网关 + 假传输层 + ASGI 桥。"""
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


def test_audit_card_renders_event_details():
    events = [
        {'type': 'desktop_action', 'ts': 0, 'action': 'type', 'process': 'notepad',
         'pid': 42, 'steps': 2, 'title': '记事本'},
        {'type': 'domain_removed', 'ts': 0, 'scope': 'deny',
         'host': 'ads.example.com'},
        {'type': 'approval_timeout', 'ts': 0, 'tool': 'bash', 'timeout_s': 300},
    ]
    payload = card.audit_card(events, counts={'desktop_action': 1})
    text = str(payload)
    assert '🖱️' in text and 'type · notepad · pid=42 · 2 步' in text
    assert 'domain_removed' in text and 'deny ads.example.com' in text
    assert 'bash · 等待 300s' in text
    assert '共 3 条' in text


def test_audit_card_empty_state_lists_what_is_tracked():
    text = str(card.audit_card([]))
    assert '最近没有安全相关事件' in text
    assert '截屏/OCR' in text


def test_audit_card_pages():
    events = [{'type': 'screen_captured', 'ts': 0, 'sha1': str(index)}
              for index in range(12)]
    payload = card.audit_card(events, token='tok')
    pages = [button['value']['page'] for element in payload['elements']
             for button in (element.get('actions') or [])
             if (button.get('value') or {}).get('audit_action') == 'page']
    assert pages == [1]


def test_audit_command_lists_events(gateway, tmp_path, monkeypatch):
    import harness

    sessions = tmp_path / 'audit-sessions'
    sessions.mkdir()
    monkeypatch.setattr(harness, 'SESSIONS_DIR', sessions)
    path = sessions / 'session_aud.jsonl'
    harness.SessionEventStore.open(path).append(
        'grant_added', path='D:/notes', scope='write', by='test')

    gw, fake = gateway
    gw.on_message('chat-audit', '/audit')
    assert 'D:/notes' in fake.last_card() or 'D:/notes' in fake.last()


def test_audit_command_rejects_unknown_type(gateway):
    gw, fake = gateway
    gw.on_message('chat-audit2', '/audit not-a-type')
    assert '用法' in fake.last()


def test_audit_command_all_lists_types(gateway):
    gw, fake = gateway
    gw.on_message('chat-audit3', '/audit all')
    assert 'screen_captured' in fake.last()


def test_truncated_card_says_there_is_more():
    """只写「共 N 条」会让人以为看到了全部；上限必须明说。"""
    events = [{'type': 'screen_captured', 'ts': 0, 'sha1': str(i)}
              for i in range(20)]
    plain = str(card.audit_card(events))
    truncated = str(card.audit_card(events, truncated=True))
    assert '已达显示上限' not in plain
    assert '已达显示上限' in truncated
    assert '共 20 条' in truncated
