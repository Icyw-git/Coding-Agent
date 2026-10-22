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


def test_events_carry_session_and_agent_tags(monkeypatch):
    import ui_bridge

    events = _collect()
    try:
        ui_bridge.set_session('s-1')
        ui_bridge.set_agent('lead')
        ui_bridge.emit('activity', state='thinking')
        with ui_bridge.agent_context('subagent'):
            ui_bridge.emit('activity', state='tool')
    finally:
        ui_events.bus.unsubscribe(events.append)

    assert events[0].payload['session_id'] == 's-1'
    assert events[0].payload['agent'] == 'lead'
    assert events[1].payload['agent'] == 'subagent'
    # 离开 agent_context 后恢复 lead
    assert ui_bridge._AGENT.get() == 'lead'


def test_textual_filters_events_from_other_sessions():
    from ui.harness_app import HarnessApp

    app = HarnessApp([], session_id='s-mine')
    app._on_event(ui_events.UIEvent('status', {'session_id': 's-other'}))
    app._on_event(ui_events.UIEvent('status', {'session_id': 's-mine'}))
    app._on_event(ui_events.UIEvent('status', {}))   # 未打标（测试/旧事件）仍可见

    assert app._events.qsize() == 2


def test_repl_renderer_skips_channels_the_repl_prints(capsys):
    from renderer import ReplRenderer

    renderer = ReplRenderer()
    renderer.on_event(ui_events.UIEvent('tool_start',
                                        {'name': 'read', 'args': {}}))
    assert capsys.readouterr().out == ''

    renderer.on_event(ui_events.UIEvent('error', {'msg': 'boom'}))
    assert 'boom' in capsys.readouterr().out


def test_status_payload_carries_branch_and_sandbox(tmp_path, monkeypatch, capsys):
    events = _collect()
    monkeypatch.setattr(harness, 'MEMORY_DIR', tmp_path / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX', tmp_path / 'mem' / 'MEMORY.md')
    response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(role='assistant', content='hi',
                                    tool_calls=None),
            finish_reason='stop')],
        usage=_usage())

    monkeypatch.setattr(harness, '_stream_completion',
                        lambda kwargs, hook: response)
    store = harness.SessionEventStore(tmp_path, session_id='ui-status')
    try:
        harness.agent_loop([{'role': 'user', 'content': 'hello'}],
                           session_store=store, housekeeping=False)
    finally:
        ui_events.bus.unsubscribe(events.append)

    status = next(e for e in events if e.type == 'status')
    assert 'branch' in status.payload and 'sandbox' in status.payload


def test_agent_loop_honours_cancel_request(tmp_path, monkeypatch):
    import turn_control

    monkeypatch.setattr(harness, 'MEMORY_DIR', tmp_path / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX', tmp_path / 'mem' / 'MEMORY.md')
    store = harness.SessionEventStore(tmp_path, session_id='ui-cancel')

    turn_control.begin_turn()
    turn_control.request_cancel()
    try:
        try:
            harness.agent_loop([{'role': 'user', 'content': 'x'}],
                               session_store=store, housekeeping=False)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError('cancelled turn should raise KeyboardInterrupt')
    finally:
        turn_control.end_turn()


def test_status_reports_estimated_cost_for_known_model(tmp_path, monkeypatch,
                                                      capsys):
    events = _collect()
    monkeypatch.setenv('LLM_MODEL_ID', 'deepseek-v4-flash')
    monkeypatch.setattr(harness, 'MEMORY_DIR', tmp_path / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX', tmp_path / 'mem' / 'MEMORY.md')
    response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(role='assistant', content='hi',
                                    tool_calls=None),
            finish_reason='stop')],
        usage=_usage(prompt=1000, completion=100))
    monkeypatch.setattr(harness, '_stream_completion',
                        lambda kwargs, hook: response)
    store = harness.SessionEventStore(tmp_path, session_id='ui-cost')
    try:
        harness.agent_loop([{'role': 'user', 'content': 'hello'}],
                           session_store=store, housekeeping=False)
    finally:
        ui_events.bus.unsubscribe(events.append)

    status = next(e for e in events if e.type == 'status')
    assert status.payload['cost_source'] == 'estimated'
    assert status.payload['currency'] == 'CNY'
    assert float(status.payload['cost']) > 0


def test_agent_loop_injects_steering_messages_between_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'MEMORY_DIR', tmp_path / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX', tmp_path / 'mem' / 'MEMORY.md')
    seen = []

    response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(role='assistant', content='ok',
                                    tool_calls=None),
            finish_reason='stop')],
        usage=_usage())

    def fake_stream(kwargs, hook):
        seen.append([m.get('content') for m in kwargs['messages']])
        return response

    monkeypatch.setattr(harness, '_stream_completion', fake_stream)
    pending = ['steered note']

    def take():
        if pending:
            return [pending.pop()]
        return []

    store = harness.SessionEventStore(tmp_path, session_id='steer')
    harness.agent_loop([{'role': 'user', 'content': 'hello'}],
                       session_store=store, housekeeping=False,
                       steer_provider=take)
    assert seen
    assert 'steered note' in seen[0]
