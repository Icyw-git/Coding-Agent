"""FeishuGateway 核心测试：fake transport + 真实 FastAPI app（ASGITransport）。

不依赖 lark-oapi 与网络——只测 on_message 编排 / 命令分发 / 审批联动。
"""

import threading
import time

import httpx
import pytest

from fastapi.testclient import TestClient

import gateway_sim
from feishu_gateway import FeishuGateway, format_permission_prompt
from harness_api import create_app
from hooks import GATE as permission_gate


class FakeTransport:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    def last(self) -> str:
        return self.sent[-1][1] if self.sent else ''

    def all_text(self) -> str:
        return '\n'.join(text for _, text in self.sent)


@pytest.fixture()
def api_app(tmp_path):
    from agent_runtime import AgentRuntime
    from session_store import SessionEventStore

    runtime = AgentRuntime(
        run_turn=lambda messages, **kwargs: 'agent-reply-ok',
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path / 'sessions',
    )
    return create_app(runtime, api_token='t', task_root=tmp_path / 'tasks')


def _sync_asgi_transport(app):
    """同步桥：httpx.MockTransport → TestClient → ASGI app（不发网络请求）。

    注：此环境的 starlette TestClient 基于 httpx2（环境同时装有 httpx 0.28 与
    httpx2 2.x），必须把 httpx2.Response 转回 httpx.Response 再交给 MockTransport。
    """
    test_client = TestClient(app)

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ('content-length', 'host')}
        raw = test_client.request(
            request.method, request.url.path,
            params=dict(request.url.params),
            content=request.content,
            headers=headers,
        )
        return httpx.Response(raw.status_code, headers=dict(raw.headers),
                              content=raw.content)

    return httpx.MockTransport(handler)


@pytest.fixture()
def gateway(api_app, tmp_path, monkeypatch):
    monkeypatch.setattr(permission_gate, 'backend', 'remote')
    api = gateway_sim.GatewayClient(
        'http://test', 't', transport=_sync_asgi_transport(api_app))
    fake = FakeTransport()
    gw = FeishuGateway(api, transport=fake,
                       bindings_path=tmp_path / 'bindings.json')
    yield gw, fake
    api.close()
    monkeypatch.setattr(permission_gate, 'backend', 'console')


def test_first_message_creates_session_and_binds(gateway, tmp_path):
    gw, fake = gateway
    gw.on_message('chat-1', '你好，帮我看看项目')
    assert 'agent-reply-ok' in fake.last()
    assert gw.bindings['chat-1']

    # 绑定持久化
    reloaded = FeishuGateway(gw.api, transport=fake,
                             bindings_path=tmp_path / 'bindings.json')
    assert reloaded.bindings['chat-1'] == gw.bindings['chat-1']


def test_commands_run_status_logs_stop(gateway):
    gw, fake = gateway
    gw.on_message('c1', '/run echo feishu-task')
    assert '任务已创建' in fake.last()

    task_id = gw.api.list_tasks()[0]['id']
    for _ in range(100):
        if gw.api.get_task(task_id)['status'] in ('succeeded', 'failed'):
            break
        time.sleep(0.05)

    gw.on_message('c1', f'/status {task_id}')
    assert 'succeeded' in fake.last()
    gw.on_message('c1', f'/logs {task_id}')
    assert 'feishu-task' in fake.last()


def test_unknown_command_reports_error(gateway):
    gw, fake = gateway
    gw.on_message('c1', '/nope')
    assert '未知命令' in fake.last()


def _drain_stale_permissions():
    """清理全局 gate 中上次测试遗留的 pending 请求（测试隔离）。"""
    for stale in permission_gate.pending():
        try:
            permission_gate.resolve(stale.req_id, allow=False, by='test-cleanup')
        except RuntimeError:
            pass


def test_permission_approval_via_chat(gateway):
    gw, fake = gateway
    _drain_stale_permissions()
    gw.on_message('c1', 'hi')   # 先建立聊天→会话绑定
    results = {}

    def waiter():
        results['allowed'] = permission_gate.decide(
            'bash', {'command': 'del x'}, 'destructive')[0]

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    for _ in range(50):
        if permission_gate.pending():
            break
        time.sleep(0.02)
    req_id = permission_gate.pending()[0].req_id

    # 轮询器推送提醒（直接调用核心方法验证格式）
    gw.poll_permissions()
    assert req_id in fake.all_text()
    assert format_permission_prompt(
        {'req_id': req_id, 'tool': 'bash', 'reason': 'destructive',
         'args': {'command': 'del x'}}).startswith('⚠ 需要审批')

    # 聊天里回复审批
    gw.on_message('c1', f'/approve {req_id} y')
    thread.join(timeout=2)
    assert results['allowed'] is True
    assert f'{req_id} → approved' in fake.last()

    # 同一请求不会重复推送
    calls_before = len(fake.sent)
    gw.poll_permissions()
    assert len(fake.sent) == calls_before


def test_api_permission_endpoints_flow(api_app):
    """HTTP 层契约：pending 列表 → resolve 200 → 重复 resolve 409 → 未知 404。"""
    from fastapi.testclient import TestClient

    monkey_backend = permission_gate.backend
    permission_gate.backend = 'remote'
    _drain_stale_permissions()
    try:
        client = TestClient(api_app)
        headers = {'Authorization': 'Bearer t'}
        results = {}

        def waiter():
            results['allowed'] = permission_gate.decide(
                'bash', {'command': 'x'}, 'destructive')[0]

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        for _ in range(50):
            if permission_gate.pending():
                break
            time.sleep(0.02)
        req_id = permission_gate.pending()[0].req_id

        listing = client.get('/v1/permissions', headers=headers)
        assert listing.status_code == 200
        assert listing.json()['pending'][0]['req_id'] == req_id

        resolved = client.post(f'/v1/permissions/{req_id}/resolve',
                               json={'allow': True}, headers=headers)
        assert resolved.status_code == 200
        assert resolved.json()['request']['status'] == 'approved'
        thread.join(timeout=2)
        assert results['allowed'] is True

        assert client.post(f'/v1/permissions/{req_id}/resolve',
                           json={'allow': False}, headers=headers).status_code == 409
        assert client.post('/v1/permissions/perm_missing/resolve',
                           json={'allow': True}, headers=headers).status_code == 404
    finally:
        permission_gate.backend = monkey_backend
