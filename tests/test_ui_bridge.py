"""UI 事件桥：主循环把流式文本、工具与状态指标发给 ui_events 总线。"""
from types import SimpleNamespace

import harness
import ui_events


def _collect():
    events = []
    ui_events.bus.subscribe(events.append)
    return events


def _usage(prompt=10, completion=2, cached=0):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached))


def test_agent_loop_emits_streaming_and_status_events(tmp_path, monkeypatch,
                                                      capsys):
    events = _collect()
    monkeypatch.setattr(harness, 'MEMORY_DIR', tmp_path / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX', tmp_path / 'mem' / 'MEMORY.md')
    response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(role='assistant', content='hi',
                                    tool_calls=None),
            finish_reason='stop')],
        usage=_usage())

    def fake_stream(kwargs, hook):
        hook('hi')
        return response

    monkeypatch.setattr(harness, '_stream_completion', fake_stream)
    store = harness.SessionEventStore(tmp_path, session_id='ui-events')
    try:
        harness.agent_loop([{'role': 'user', 'content': 'hello'}],
                           session_store=store, housekeeping=False)
    finally:
        ui_events.bus.unsubscribe(events.append)

    types = [event.type for event in events]
    assert 'assistant_start' in types
    assert 'assistant_delta' in types
    assert 'assistant_end' in types
    assert 'round_end' in types
    assert 'status' in types
    assert 'session_end' in types

    delta = next(e for e in events if e.type == 'assistant_delta')
    assert delta.payload['delta'] == 'hi'
    status = next(e for e in events if e.type == 'status')
    assert status.payload['model'] == harness.os.getenv('LLM_MODEL_ID', '')
    assert status.payload['ctx_tokens'] == 10
    round_end = next(e for e in events if e.type == 'round_end')
    assert round_end.payload['prompt_tokens'] == 10


def test_exec_tool_call_emits_tool_events(monkeypatch):
    events = _collect()
    tool_call = SimpleNamespace(
        id='call-1',
        function=SimpleNamespace(name='read', arguments='{"path":"a.txt"}'))
    try:
        harness.exec_tool_call(tool_call, registry={'read': lambda path: 'ok'},
                               hooks=False)
    finally:
        ui_events.bus.unsubscribe(events.append)

    started = next(e for e in events if e.type == 'tool_start')
    ended = next(e for e in events if e.type == 'tool_end')
    assert started.payload['name'] == 'read'
    assert started.payload['args'] == {'path': 'a.txt'}
    assert ended.payload['ok'] is True
    assert ended.payload['outcome'] == 'completed'
    assert ended.payload['duration_ms'] >= 0


def test_transcript_export_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'TRANSCRIPT_DIR', tmp_path / 'transcripts')
    messages = [{'role': 'user', 'content': 'hello'},
                {'role': 'assistant', 'content': 'hi'}]

    path = harness.export_session(messages)

    assert path.is_file()
    assert path in harness.list_transcripts()
    assert harness.load_transcript(path) == messages
