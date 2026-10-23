"""MCP wrapper telemetry: server, tool, duration, and error class."""
from types import SimpleNamespace

import pytest

import harness
import mcp_bridge
from session_store import SessionEventStore


class FakeClient:
    def __init__(self, name='filesystem', result='ok', error=None):
        self.name = name
        self._timeout = 5
        self.result = result
        self.error = error

    async def call_tool(self, tool_name, arguments):
        if self.error is not None:
            raise self.error
        return self.result


def wrapper(**client_kwargs):
    return mcp_bridge._make_sync_wrapper(FakeClient(**client_kwargs), 'search')


def test_success_event_has_server_tool_and_duration():
    events = []
    with mcp_bridge.telemetry_sink(events.append):
        assert wrapper()(query='x') == 'ok'
    assert len(events) == 1
    event = events[0]
    assert event['server'] == 'filesystem'
    assert event['tool'] == 'search'
    assert event['status'] == 'ok'
    assert event['error_type'] == ''
    assert event['duration_ms'] >= 0


@pytest.mark.parametrize('result,error,error_type', [
    ('MCP error: tool call timeout', None, 'timeout'),
    ('MCP error: not connected', None, 'not_connected'),
    ('MCP error: server exploded', None, 'server_error'),
    (None, TimeoutError('too slow'), 'timeout'),
    (None, RuntimeError('bridge down'), 'RuntimeError'),
])
def test_error_events_classify_failure(result, error, error_type):
    events = []
    with mcp_bridge.telemetry_sink(events.append):
        output = wrapper(result=result, error=error)()
    assert output.startswith('MCP error:')
    assert events[0]['status'] == 'error'
    assert events[0]['error_type'] == error_type


def test_sink_failure_does_not_change_the_tool_result():
    def broken(_event):
        raise RuntimeError('telemetry down')

    with mcp_bridge.telemetry_sink(broken):
        assert wrapper()() == 'ok'


def _fake_exec(wrapper):
    def execute(tool_call, **kwargs):
        return (
            tool_call.function.name,
            {},
            {'role': 'tool', 'tool_call_id': tool_call.id,
             'content': wrapper(query='x')},
            [],
        )

    return execute


def _tool_call(call_id, name='mcp_search'):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments='{"query":"x"}'),
    )


def test_tool_batch_records_mcp_call_without_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'exec_tool_call', _fake_exec(wrapper()))
    store = SessionEventStore(tmp_path, session_id='mcp')
    harness._run_tool_batch([_tool_call('m1')], store, [])

    events = [event for event in store.read_events() if event['type'] == 'mcp_call']
    assert len(events) == 1
    assert events[0]['server'] == 'filesystem'
    assert events[0]['tool'] == 'search'
    assert events[0]['status'] == 'ok'
    assert 'query' not in str(events[0])


def test_parallel_batch_keeps_a_sink_in_each_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'exec_tool_call', _fake_exec(wrapper()))
    store = SessionEventStore(tmp_path, session_id='parallel')
    calls = [_tool_call('m1', name='read'), _tool_call('m2', name='read')]
    harness._run_tool_batch(calls, store, [])

    events = [event for event in store.read_events() if event['type'] == 'mcp_call']
    assert len(events) == 2
    assert {event['server'] for event in events} == {'filesystem'}
