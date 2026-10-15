import threading

import pytest
import httpx

from agent_runtime import AgentRuntime
from session_store import SessionEventStore
from harness_api import create_app
from gateway_sim import GatewayClient
from feishu_gateway import FeishuGateway
from test_feishu_gateway import _sync_asgi_transport, FakeTransport


@pytest.fixture
def environment(tmp_path):
    runtime = AgentRuntime(run_turn=lambda *a, **kw: 'done',
                           session_store_cls=SessionEventStore, sessions_dir=tmp_path / 'sessions')
    runtime.create_session('old')
    store = runtime.open_session('old')
    store.record_workspace(tmp_path)
    store.append('message', ts=100, message={'role': 'user', 'content': '检查测试失败原因'})
    api = GatewayClient('http://test', 't', transport=_sync_asgi_transport(
        create_app(runtime, api_token='t', task_root=tmp_path / 'tasks', rate_limit=10000)))
    transport = FakeTransport()
    gw = FeishuGateway(api, transport=transport, bindings_path=tmp_path / 'bindings.json')
    yield runtime, api, gw, transport
    api.close()


def test_list_search_and_switch_uses_names(environment):
    runtime, api, gw, transport = environment
    gw.on_message('chat', '/sessions')
    assert '检查测试失败原因' in transport.last()
    assert 'events=' not in transport.last()
    assert '工作区' in transport.last()
    gw.on_message('chat', '/resume 1')
    assert gw.bindings['chat'] == 'old'
    assert '仅切换对话' in transport.last()


def test_explicit_search_keeps_empty_candidates(environment):
    runtime, api, gw, transport = environment
    runtime.create_session('empty-a')
    runtime.create_session('empty-b')
    gw.on_message('chat', '/resume empty-')
    assert len(gw._session_choices['chat']) == 2
    gw.on_message('chat', '/resume 1')
    assert gw.bindings['chat'].startswith('empty-')


def test_confirmation_rechecks_bindings_and_expiry(environment, monkeypatch):
    runtime, api, gw, transport = environment
    gw.on_message('chat', '/archive all')
    gw.on_message('chat', '/archive select 1')
    gw.bindings['other'] = 'old'
    gw.on_message('chat', '/confirm')
    assert runtime.list_sessions()
    assert '仍被聊天使用' in transport.all_text()
    gw.bindings.clear()
    gw.on_message('chat', '/archive all')
    gw.on_message('chat', '/archive select 1')
    selected_at = gw._session_confirmation['chat'][2]
    monkeypatch.setattr('feishu_gateway.time.monotonic', lambda: selected_at + 301)
    gw.on_message('chat', '/confirm')
    assert '没有有效' in transport.last()
    assert runtime.list_sessions()


def test_archive_restore_and_delete_confirmation(environment):
    runtime, api, gw, transport = environment
    gw.on_message('chat', '/archive all')
    gw.on_message('chat', '/archive select 1')
    assert runtime.list_sessions()
    gw.on_message('chat', '/confirm')
    assert runtime.list_sessions() == []
    gw.on_message('chat', '/archive restore')
    gw.on_message('chat', '/archive select 1')
    gw.on_message('chat', '/confirm')
    assert runtime.list_sessions()[0]['id'] == 'old'
    gw.on_message('chat', '/archive all')
    gw.on_message('chat', '/archive select 1')
    gw.on_message('chat', '/confirm')
    gw.on_message('chat', '/delete')
    gw.on_message('chat', '/delete select 1')
    gw.on_message('chat', '/confirm')
    assert api.list_archives()
    gw.on_message('chat', '/delete select 1')
    gw.on_message('chat', '/confirm 删除')
    assert api.list_archives() == []


def test_bound_session_and_busy_turn_cannot_archive(environment):
    runtime, api, gw, transport = environment
    gw.bindings['other'] = 'old'
    gw.on_message('chat', '/archive all')
    assert '没有符合条件' in transport.last()
    gw.bindings.clear()
    row = runtime.list_sessions()[0]
    ready, release = threading.Event(), threading.Event()
    def hold():
        with runtime._session_lock('old'):
            ready.set()
            release.wait(3)
    thread = threading.Thread(target=hold)
    thread.start()
    assert ready.wait(2)
    try:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            api.manage_session('archive', row)
        assert caught.value.response.status_code == 409
        assert runtime.list_sessions()
    finally:
        release.set()
        thread.join()


def test_changes_since_preview_and_cancel(environment):
    runtime, api, gw, transport = environment
    gw.on_message('chat', '/archive all')
    gw.on_message('chat', '/archive select 1')
    runtime.open_session('old').append_session_title('更新的任务')
    gw.on_message('chat', '/confirm')
    assert runtime.list_sessions()
    assert '未处理' in transport.all_text()
    gw.on_message('chat', '/archive all')
    gw.on_message('chat', '/archive select 1')
    gw.on_message('chat', '/cancel')
    gw.on_message('chat', '/confirm')
    assert runtime.list_sessions()


def test_management_api_auth_validation_and_metadata(environment):
    runtime, api, gw, transport = environment
    row = api.list_sessions()[0]
    assert row['workspace'] and row['preview'] == '检查测试失败原因'
    response = api.http.get('/v1/session-archives', headers={'Authorization': ''})
    assert response.status_code == 401
    for row in ({}, {'id': '../bad'}, {'id': []}):
        response = api.http.post('/v1/session-actions', json={'action': 'delete', 'session': row})
        assert response.status_code == 400
