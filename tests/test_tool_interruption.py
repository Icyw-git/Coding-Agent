from types import SimpleNamespace

import pytest

from agent_runtime import AgentRuntime
from session_store import SessionEventStore
from test_checkpoints import _loop_env


def call(call_id):
    return {'id': call_id, 'type': 'function',
            'function': {'name': 'bash', 'arguments': '{"command":"echo hi"}'}}


@pytest.mark.parametrize('legacy_user', [False, True])
def test_send_recovers_partial_tool_batch_without_reexecution(tmp_path, legacy_user):
    seen = []

    def run_turn(messages, session_store):
        SessionEventStore.validate_openai_history(messages)
        seen.extend(messages)
        return 'continued'

    runtime = AgentRuntime(run_turn, SessionEventStore, tmp_path)
    sid = runtime.create_session('interrupted')
    store = runtime.open_session(sid)
    store.append_message({'role': 'assistant', 'content': None,
                          'tool_calls': [call('done'), call('unknown')]})
    store.append_message({'role': 'tool', 'tool_call_id': 'done', 'content': 'real output'})
    if legacy_user:
        store.append_message({'role': 'user', 'content': 'previous failed retry'})
    assert runtime.send(sid, 'continue').content == 'continued'
    results = [m for m in seen if m['role'] == 'tool']
    assert results[0]['content'] == 'real output'
    assert results[1]['tool_call_id'] == 'unknown'
    assert 'outcome is unknown' in results[1]['content']
    assert runtime.history(sid) == seen
    runtime.send(sid, 'again')
    assert sum(e['type'] == 'tool_recovery' for e in store.read_events()) == 1


def test_recovery_keeps_strict_replay_until_explicitly_requested(tmp_path):
    store = SessionEventStore(tmp_path)
    store.append_message({'role': 'assistant', 'content': None, 'tool_calls': [call('one')]})
    assert len(store.rebuild_openai_history()) == 1
    recovered = store.rebuild_openai_history(recover_pending=True)
    assert len(recovered) == 2
    assert SessionEventStore.open(store.path).rebuild_openai_history() == recovered


def test_history_repairs_old_gap_without_writing_or_closing_active_tail(tmp_path):
    runtime = AgentRuntime(lambda *a, **kw: 'unused', SessionEventStore, tmp_path)
    sid = runtime.create_session('history')
    store = runtime.open_session(sid)
    store.append_message({'role': 'assistant', 'content': None, 'tool_calls': [call('old')]})
    store.append_message({'role': 'user', 'content': 'retry'})
    store.append_message({'role': 'assistant', 'content': None, 'tool_calls': [call('active')]})
    original = store.path.read_bytes()
    history = runtime.history(sid)
    assert history[1]['tool_call_id'] == 'old'
    assert history[-1]['tool_calls'][0]['id'] == 'active'
    SessionEventStore.validate_openai_history(history, allow_pending=True)
    assert store.path.read_bytes() == original


def test_gateway_reads_then_streams_legacy_broken_history(tmp_path):
    from harness_api import create_app
    from gateway_sim import GatewayClient
    from test_feishu_gateway import _sync_asgi_transport

    def run_turn(messages, session_store, on_delta=None):
        SessionEventStore.validate_openai_history(messages)
        on_delta('continued')
        session_store.append_message({'role': 'assistant', 'content': 'continued'})
        return 'continued'

    runtime = AgentRuntime(run_turn, SessionEventStore, tmp_path / 'sessions')
    sid = runtime.create_session('legacy')
    store = runtime.open_session(sid)
    store.append_message({'role': 'assistant', 'content': None,
                          'tool_calls': [call('done'), call('missing')]})
    store.append_message({'role': 'tool', 'tool_call_id': 'done', 'content': 'saved result'})
    store.append_message({'role': 'user', 'content': 'retry that failed'})
    app = create_app(runtime, api_token='test', task_root=tmp_path / 'tasks')
    api = GatewayClient('http://test', 'test', transport=_sync_asgi_transport(app))
    try:
        response = api.http.get(f'/v1/sessions/{sid}')
        response.raise_for_status()
        assert len(response.json()['history']) == 4
        deltas = []
        assert api.send_stream('continue', sid, on_delta=deltas.append) == 'continued'
        assert deltas == ['continued']
        SessionEventStore.validate_openai_history(store.rebuild_openai_history())
        assert sum(e['type'] == 'tool_recovery' for e in store.read_events()) == 1
    finally:
        api.close()


@pytest.mark.parametrize('arguments', ['{"command":', '[]', 'null'])
def test_malformed_tool_arguments_produce_a_tool_result(arguments):
    import harness
    tool = SimpleNamespace(id='bad', function=SimpleNamespace(name='bash', arguments=arguments))
    _, _, result = harness.exec_tool_call(tool, registry={}, hooks=False)
    assert result['tool_call_id'] == 'bad'
    assert 'invalid tool arguments' in result['content']


def test_truncated_tool_call_is_not_executed_and_next_request_is_valid(tmp_path, monkeypatch):
    harness, workspace = _loop_env(tmp_path, monkeypatch)
    tool = SimpleNamespace(id='truncated', type='function',
                           function=SimpleNamespace(name='bash', arguments='{"command":'))
    responses = iter([
        SimpleNamespace(choices=[SimpleNamespace(finish_reason='length',
                        message=SimpleNamespace(content=None, tool_calls=[tool]))]),
        SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
                        message=SimpleNamespace(content='done', tool_calls=[]))]),
    ])
    requests = []

    def create(**kwargs):
        SessionEventStore.validate_openai_history(kwargs['messages'])
        requests.append(list(kwargs['messages']))
        return next(responses)

    monkeypatch.setattr(harness.client.chat.completions, 'create', create)
    monkeypatch.setattr(harness, 'exec_tool_call', lambda *a, **kw: pytest.fail('truncated tool executed'))
    store = SessionEventStore(tmp_path / 'sessions')
    user = {'role': 'user', 'content': 'run'}
    store.append_message(user)
    assert harness.agent_loop([user], session_store=store) == 'done'
    harness.wait_for_housekeeping()
    assert len(requests) == 2
    SessionEventStore.validate_openai_history(store.rebuild_openai_history())
