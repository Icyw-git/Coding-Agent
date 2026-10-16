import json
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

from agent_runtime import AgentRuntime
from feishu_gateway import FeishuGateway
from gateway_sim import GatewayClient
from harness_api import create_app
from session_store import SessionEventStore
from test_feishu_gateway import _sync_asgi_transport
from turn_progress import TurnProgress


def test_progress_tracks_tool_events_without_exposing_arguments(tmp_path):
    store = SessionEventStore(tmp_path)
    progress = TurnProgress()
    progress.follow(store, 0)
    store.append('tool_started', name='bash', arguments='private arguments')
    status = progress.snapshot()
    assert status['phase'] == 'tool' and status['tool'] == 'bash'
    assert 'private' not in json.dumps(status)
    store.append_message({'role': 'tool', 'tool_call_id': 'one', 'content': 'done'})
    assert progress.snapshot()['phase'] == 'thinking'


def test_api_reports_blocked_tool_progress_and_records_stack(tmp_path, monkeypatch):
    monkeypatch.setattr('harness_api.STREAM_PROGRESS_INTERVAL', 0.01)
    monkeypatch.setattr('harness_api.STREAM_DIAGNOSTIC_INTERVAL', 0.02)

    def run_turn(messages, session_store, on_delta=None):
        on_delta('让我核实')
        session_store.append('tool_started', name='bash')
        time.sleep(0.1)
        session_store.append_message({'role': 'tool', 'tool_call_id': 'one', 'content': 'done'})
        return '核实完成'

    runtime = AgentRuntime(run_turn, SessionEventStore, tmp_path / 'sessions')
    sid = runtime.create_session('progress')
    log = tmp_path / 'api.log'
    from fastapi.testclient import TestClient
    with TestClient(create_app(runtime, api_token='t', task_root=tmp_path / 'tasks',
                               access_log_path=str(log))) as client:
        response = client.post(f'/v1/sessions/{sid}/messages/stream',
                               json={'content': 'check'}, headers={'Authorization': 'Bearer t'})
    assert 'event: done' in response.text
    assert '"phase": "tool"' in response.text
    assert '"tool": "bash"' in response.text
    assert 'agent turn still running' in log.read_text(encoding='utf-8')
    assert 'run_turn' in log.read_text(encoding='utf-8')


def test_gateway_rejects_truncated_reply_stream():
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, text='event: delta\ndata: {"text":"partial"}\n\n'))
    api = GatewayClient('http://test', 't', transport=transport)
    try:
        with pytest.raises(RuntimeError, match='提前结束'):
            api.send_stream('hello', 'session')
    finally:
        api.close()


class CardTransport:
    def __init__(self):
        self.created, self.updated = [], []

    def send_markdown(self, chat_id, text):
        self.created.append(text)
        return 'card'

    def update_markdown(self, message_id, text):
        self.updated.append(text)


def test_feishu_marks_failed_partial_card_without_repeating_task(tmp_path):
    calls = []

    def stream(text, **kwargs):
        calls.append(text)
        kwargs['on_delta']('让我核实')
        raise RuntimeError('connection lost')

    transport = CardTransport()
    gw = FeishuGateway(SimpleNamespace(send_stream=stream), transport=transport,
                        bindings_path=tmp_path / 'bindings.json')
    gw._send_agent_reply('chat', 'session', 'check')
    assert calls == ['check']
    assert len(transport.created) == 1
    assert '让我核实' in transport.updated[-1]
    assert transport.updated[-1].startswith('处理未完成')
    assert '处理未完成' in transport.updated[-1]


def test_feishu_shows_progress_then_replaces_it_with_final_answer(tmp_path):
    def stream(text, **kwargs):
        kwargs['on_progress']({'phase': 'tool', 'tool': 'bash', 'elapsed': 12, 'tool_elapsed': 8})
        return '最终答案'

    transport = CardTransport()
    gw = FeishuGateway(SimpleNamespace(send_stream=stream), transport=transport,
                        bindings_path=tmp_path / 'bindings.json')
    gw._send_agent_reply('chat', 'session', 'check')
    assert '正在处理' in transport.created[0]
    assert '工具' not in transport.created[0]
    assert '已用时' in transport.created[0]
    assert transport.updated[-1] == '最终答案'


def test_shell_timeout_returns_without_waiting_for_child_output(tmp_path, monkeypatch):
    import tools
    monkeypatch.setattr(tools, 'BASH_TIMEOUT', 0.2)
    started = time.monotonic()
    result = tools.run_bash(f'"{sys.executable}" -c "import time; time.sleep(3)"', cwd=str(tmp_path))
    assert 'timeout' in result
    assert time.monotonic() - started < 3


def test_shell_output_is_bounded_and_keeps_stderr(tmp_path):
    import tools
    result = tools.run_bash('echo stdout & echo stderr 1>&2' if sys.platform == 'win32'
                            else 'echo stdout; echo stderr >&2', cwd=str(tmp_path))
    assert 'stdout' in result and 'stderr' in result
