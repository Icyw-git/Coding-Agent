"""子代理执行路径：必须复用 harness.exec_tool_call（审批/截断/事件/checkpoint）。"""
from types import SimpleNamespace

import harness
import subagent
import ui_events


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
        return responses.pop(0)

    monkeypatch.setattr(harness, 'client', SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    return seen


def test_subagent_tools_go_through_exec_tool_call(monkeypatch):
    calls = []

    def fake_exec(tool_call, registry=None, *, hooks=True, background=True,
                  **kwargs):
        calls.append((tool_call.function.name, hooks, background, registry))
        image = {'type': 'harness_image', 'path': 'img.png',
                 'name': 'img.png', 'mime': 'image/png', 'tokens': 1}
        return ('read', {'path': 'a.txt'},
                {'role': 'tool', 'tool_call_id': tool_call.id,
                 'content': 'tool-output'}, [image])

    monkeypatch.setattr(harness, 'exec_tool_call', fake_exec)
    seen = _fake_client(monkeypatch, [
        _response(_assistant(tool_calls=[_tool_call()]), finish='tool_calls'),
        _response(_assistant(content='done')),
    ])

    assert subagent.spawn_subagent('inspect') == 'done'

    assert calls and calls[0][0] == 'read'
    assert calls[0][1] is True and calls[0][2] is False
    assert 'read' in calls[0][3]
    # 第二次请求里能看到工具结果与紧随其后的图片附件
    second_messages = seen[1]['messages']
    tool = next(m for m in second_messages if m.get('role') == 'tool')
    assert tool['content'] == 'tool-output'
    assert second_messages[-1]['role'] == 'user'


def test_subagent_tool_events_are_tagged_and_output_is_bounded(monkeypatch):
    big = 'x' * 30_000
    monkeypatch.setitem(harness.tool_registry, 'read', lambda path: big)
    monkeypatch.setattr(harness, 'trigger_hooks', lambda *args, **kwargs: None)
    seen = _fake_client(monkeypatch, [
        _response(_assistant(tool_calls=[_tool_call()]), finish='tool_calls'),
        _response(_assistant(content='done')),
    ])
    events = []
    ui_events.bus.subscribe(events.append)
    try:
        assert subagent.spawn_subagent('inspect') == 'done'
    finally:
        ui_events.bus.unsubscribe(events.append)

    tool_end = next(e for e in events if e.type == 'tool_end')
    assert tool_end.payload['agent'] == 'subagent'

    tool = next(m for m in seen[1]['messages'] if m.get('role') == 'tool')
    assert 'truncated' in tool['content']
    assert len(tool['content']) < 30_000
