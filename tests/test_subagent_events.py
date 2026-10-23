"""子代理事件：独立日志可回放，父会话只留引用；记录失败不影响执行。"""
from types import SimpleNamespace

import pytest

import harness
import subagent
import subagent_events
from session_store import SessionEventStore


def _tool_call(call_id='c1', name='read', arguments='{"path":"a.txt"}'):
    return SimpleNamespace(
        id=call_id, type='function',
        function=SimpleNamespace(name=name, arguments=arguments))


def _assistant(content=None, tool_calls=None):
    message = SimpleNamespace(role='assistant', content=content,
                              tool_calls=tool_calls)

    def model_dump():
        calls = [{'id': call.id, 'type': call.type,
                  'function': {'name': call.function.name,
                               'arguments': call.function.arguments}}
                 for call in (tool_calls or [])]
        return {'role': 'assistant', 'content': content,
                'tool_calls': calls or None}

    message.model_dump = model_dump
    return message


def _response(message, finish='stop'):
    return SimpleNamespace(choices=[SimpleNamespace(message=message,
                                                    finish_reason=finish)])


def _fake_client(monkeypatch, responses):
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(harness, 'client', SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    return seen


@pytest.fixture
def parent_store(tmp_path):
    """把 manager 指向一个独立父会话；用完恢复 ContextVar。"""
    store = SessionEventStore(tmp_path / 'sessions', session_id='parent')
    token = harness._checkpoint_manager.set(
        SimpleNamespace(session_id='parent', session_store=store))
    try:
        yield store
    finally:
        harness._checkpoint_manager.reset(token)


def test_subagent_writes_child_log_and_parent_reference(parent_store, monkeypatch):
    monkeypatch.setitem(harness.tool_registry, 'read', lambda path: 'file-body')
    _fake_client(monkeypatch, [
        _response(_assistant(tool_calls=[_tool_call()]), finish='tool_calls'),
        _response(_assistant(content='done')),
    ])

    assert subagent.spawn_subagent('inspect files') == 'done'

    parent_events = parent_store.read_events()
    started = next(e for e in parent_events if e['type'] == 'subagent_started')
    finished = next(e for e in parent_events if e['type'] == 'subagent_finished')
    assert started['description'] == 'inspect files'
    assert finished['subagent_id'] == started['subagent_id']
    assert finished['status'] == 'completed' and finished['turns'] == 2

    child_path = parent_store.path.parent / started['log']
    child = SessionEventStore.open(child_path)
    roles = [m['role'] for m in child.rebuild_openai_history()]
    assert roles == ['system', 'user', 'assistant', 'tool', 'assistant']
    child_events = child.read_events()
    assert [e['type'] for e in child_events][0] == 'subagent_started'
    assert child_events[-1]['type'] == 'subagent_finished'
    assert child_events[-1]['status'] == 'completed'

    # 父 store 是另一个实例，子代理写事件后它必须重新读取 seq 再追加
    parent_store.append_message({'role': 'assistant', 'content': 'after'})
    events = parent_store.read_events()
    assert [event['seq'] for event in events] == list(range(1, len(events) + 1))


def test_subagent_failure_records_error_and_reraises(parent_store, monkeypatch):
    _fake_client(monkeypatch, [RuntimeError('boom')])

    with pytest.raises(RuntimeError, match='boom'):
        subagent.spawn_subagent('fail')

    finished = [e for e in parent_store.read_events()
                if e['type'] == 'subagent_finished']
    assert len(finished) == 1
    assert finished[0]['status'] == 'error'
    assert finished[0]['error_type'] == 'RuntimeError'
    started = next(e for e in parent_store.read_events()
                   if e['type'] == 'subagent_started')
    child = SessionEventStore.open(parent_store.path.parent / started['log'])
    child_finished = [e for e in child.read_events()
                      if e['type'] == 'subagent_finished']
    assert child_finished[0]['error'] == 'boom'


def test_recorder_failure_does_not_break_subagent(parent_store, monkeypatch):
    def broken_store(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(subagent_events, 'SessionEventStore', broken_store)
    _fake_client(monkeypatch, [_response(_assistant(content='done'))])

    assert subagent.spawn_subagent('inspect') == 'done'
    assert not (parent_store.path.parent / 'subagents').exists()


def test_subagent_without_session_context_still_runs(tmp_path, monkeypatch):
    token = harness._checkpoint_manager.set(None)
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')
    try:
        _fake_client(monkeypatch, [_response(_assistant(content='done'))])
        assert subagent.spawn_subagent('inspect') == 'done'
    finally:
        harness._checkpoint_manager.reset(token)
    assert not (tmp_path / 'sessions' / 'subagents').exists()
