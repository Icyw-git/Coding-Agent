import asyncio
import sys
from pathlib import Path

import pytest

import harness
import hooks
from MCPclient import MCPclient
from harness_api import create_app
from harness_sdk import create_default_client


@pytest.fixture
def shared_tools(monkeypatch):
    calls = []
    schema = {'type': 'function', 'function': {
        'name': 'mcp_echo', 'description': 'test', 'parameters': {'type': 'object'}}}

    def register(registry):
        calls.append('connect')
        registry['mcp_echo'] = lambda text: f'echo:{text}'
        return [schema]

    monkeypatch.setattr(harness, '_mcp_initialized', False)
    monkeypatch.setattr(harness, 'TOOLS', [])
    monkeypatch.setattr(harness, 'tool_registry', {})
    monkeypatch.setattr(harness, 'register_mcp_tools', register)
    monkeypatch.setattr(harness, 'shutdown_mcp', lambda: calls.append('close'))
    return calls


def test_default_sdk_registers_tools_once(shared_tools):
    create_default_client()
    create_default_client()
    assert shared_tools == ['connect']
    assert len(harness.TOOLS) == 1
    assert harness.tool_registry['mcp_echo'](text='sdk') == 'echo:sdk'


def test_mcp_registration_marks_tools_as_external(shared_tools):
    """注册进来的工具同时登记为第三方工具：策略层不知道它们的副作用，默认询问。"""
    harness.initialize_mcp_tools()
    assert 'mcp_echo' in hooks.EXTERNAL_TOOLS
    decision = hooks.evaluate_call('mcp_echo', {'text': 'x'})
    assert decision.action == 'ask' and decision.code == 'external_tool'


def test_default_api_loads_mcp_in_lifespan_and_closes_it(shared_tools, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')
    monkeypatch.setattr(harness, 'CHECKPOINT_DIR', tmp_path / 'checkpoints')

    def turn(messages, session_store, on_delta=None):
        assert harness.TOOLS[0]['function']['name'] == 'mcp_echo'
        return harness.tool_registry['mcp_echo'](text=messages[-1]['content'])

    monkeypatch.setattr(harness, 'agent_loop', turn)
    app = create_app(api_token='t', task_root=tmp_path / 'tasks',
                     access_log_path=str(tmp_path / 'access.log'))
    assert shared_tools == []  # Merely constructing an app must not spawn MCP.
    with TestClient(app) as client:
        assert shared_tools == ['connect']
        headers = {'Authorization': 'Bearer t'}
        client.post('/v1/sessions', json={'id': 'api-mcp'}, headers=headers).raise_for_status()
        response = client.post('/v1/sessions/api-mcp/messages',
                               json={'content': 'api'}, headers=headers)
        response.raise_for_status()
        assert response.json()['content'] == 'echo:api'
    assert shared_tools == ['connect', 'close']


def test_injected_runtime_does_not_start_or_stop_global_mcp(shared_tools, tmp_path):
    from fastapi.testclient import TestClient
    from agent_runtime import AgentRuntime
    from session_store import SessionEventStore

    runtime = AgentRuntime(lambda *a, **kw: 'ok', SessionEventStore, tmp_path / 'sessions')
    app = create_app(runtime, api_token='t', task_root=tmp_path / 'tasks',
                     access_log_path=str(tmp_path / 'access.log'))
    with TestClient(app):
        assert shared_tools == []
    assert shared_tools == []


def test_stdio_connection_can_be_opened_called_and_closed_in_different_tasks():
    async def scenario():
        server = Path(__file__).parent / 'fixtures' / 'mock_mcp_server.py'
        client = MCPclient('mock', sys.executable, [str(server)])
        try:
            tools = await asyncio.create_task(client.connect(timeout=10))
            assert {t['name'] for t in tools} == {'echo', 'add'}
            assert await asyncio.create_task(client.call_tool('echo', {'text': 'hello'})) == 'echo:hello'
            assert await asyncio.create_task(client.call_tool('add', {'a': 2, 'b': 3})) == 'sum:5'
        finally:
            await asyncio.create_task(client.close())
        assert client.session is None
        assert client._session_task is None
        await client.close()

    asyncio.run(scenario())


def test_failed_connection_cleans_up_owner_task(tmp_path):
    async def scenario():
        client = MCPclient('missing', str(tmp_path / 'missing-command.exe'))
        with pytest.raises(Exception):
            await asyncio.create_task(client.connect(timeout=2))
        assert client.session is None
        assert client._session_task is None
        await client.close()

    asyncio.run(scenario())
