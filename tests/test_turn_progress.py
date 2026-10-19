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
        kwargs['on_progress']({'phase': 'tool', 'tool': 'bash', 'elapsed': 12,
                               'elapsed_text': '00:12'})
        return '最终答案'

    transport = CardTransport()
    gw = FeishuGateway(SimpleNamespace(send_stream=stream), transport=transport,
                        bindings_path=tmp_path / 'bindings.json')
    gw._send_agent_reply('chat', 'session', 'check')
    assert '正在执行 bash' in transport.created[0]
    assert '00:12' in transport.created[0]
    assert 'tool_elapsed' not in transport.created[0]
    assert transport.updated[-1] == '最终答案'


def test_format_duration_boundaries():
    from turn_progress import format_duration

    assert format_duration(0) == '00:00'
    assert format_duration(59) == '00:59'
    assert format_duration(60) == '01:00'
    assert format_duration(3599) == '59:59'
    assert format_duration(3600) == '01:00:00'
    assert format_duration(7326) == '02:02:06'


def test_task_duration_display_compat_and_states():
    from feishu_gateway import _format_task_duration

    now = time.time()
    assert _format_task_duration({'status': 'queued'}) == '排队中'
    running = {'status': 'running', 'started_at': now - 100}
    assert _format_task_duration(running) == '01:40（进行中）'
    done = {'status': 'succeeded', 'started_at': now - 18,
            'ended_at': now, 'duration_ms': 18000}
    assert _format_task_duration(done) == '00:18'
    # 旧记录没有 duration_ms：按 started_at/ended_at 动态计算
    legacy = {'status': 'succeeded', 'started_at': now - 30, 'ended_at': now}
    assert _format_task_duration(legacy) == '00:30'


def test_progress_update_skips_identical_text(tmp_path):
    def stream(text, **kwargs):
        status = {'phase': 'thinking', 'elapsed': 0, 'elapsed_text': '00:00'}
        kwargs['on_progress'](status)
        kwargs['on_progress'](status)   # 同一秒重复推送 → 卡片不应重复更新
        return '完成'

    transport = CardTransport()
    gw = FeishuGateway(SimpleNamespace(send_stream=stream), transport=transport,
                        bindings_path=tmp_path / 'bindings.json')
    gw._send_agent_reply('chat', 'session', 'check')
    assert len(transport.created) == 1
    assert transport.updated == ['完成']   # 只剩最终回复更新，进度未重复刷新


def test_card_patch_gate_throttles_per_instance(tmp_path):
    gw = FeishuGateway(SimpleNamespace(), transport=CardTransport(),
                       bindings_path=tmp_path / 'bindings.json')
    assert gw._card_patch_gate() is True
    assert gw._card_patch_gate() is False   # 实例内最小间隔合并
    other = FeishuGateway(SimpleNamespace(), transport=CardTransport(),
                          bindings_path=tmp_path / 'bindings2.json')
    assert other._card_patch_gate() is True   # 其他实例不受影响


def test_shell_timeout_returns_without_waiting_for_child_output(tmp_path, monkeypatch):
    """这一条测的是超时回收（子进程还在睡也要立刻返回），不是审批。

    `python -c` 在默认档要审批（exec_policy 的解释器载荷判定），所以这里显式
    切到 danger-full-access，让判定层只剩黑名单。
    """
    import tools
    monkeypatch.setenv('HARNESS_SANDBOX_MODE', 'danger-full-access')
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
