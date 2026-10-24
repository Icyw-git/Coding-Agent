"""agent_loop 的结构不变量回归。

锁住 _TurnState / _run_llm_request / _run_tool_calls / _finish_response
拆分前的行为契约：事件顺序、整体替换历史后的 token 重估、压缩跳过窗口与
snip 状态携带，以及 harness 命名空间上的 patch 点。
"""
import copy
from types import SimpleNamespace

import pytest

import harness
import llm_config
from session_store import SessionEventStore


@pytest.fixture(autouse=True)
def _stub_side_channels(monkeypatch):
    """主循环里的副通道与后台线程一律打桩，脚本响应队列才不会被消费错位。"""
    monkeypatch.setattr(harness, 'load_memories', lambda messages: '')
    monkeypatch.setattr(harness, 'extract_memories', lambda pre_compress: None)
    monkeypatch.setattr(harness, 'consolidate_memories', lambda: None)
    monkeypatch.setattr(harness, 'generate_session_title', lambda messages: 'invariants')
    monkeypatch.setattr(harness, '_post_turn_housekeeping', lambda *args, **kwargs: None)
    monkeypatch.setattr(harness, 'wait_for_mcp_tools', lambda timeout=10.0: True)
    monkeypatch.setattr(harness, 'streaming_enabled', lambda: False)


def _usage(prompt=100, completion=10):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion,
        total_tokens=prompt + completion, prompt_tokens_details=None,
        prompt_cache_hit_tokens=None, cache_creation_input_tokens=None,
        completion_tokens_details=None)


class _Call:
    def __init__(self, call_id, name, arguments='{}'):
        self.id = call_id
        self.type = 'function'
        self.function = SimpleNamespace(name=name, arguments=arguments)


class _Message:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self):
        message = {'role': 'assistant', 'content': self.content}
        if self.tool_calls:
            message['tool_calls'] = [
                {'id': call.id, 'type': 'function',
                 'function': {'name': call.function.name,
                              'arguments': call.function.arguments}}
                for call in self.tool_calls]
        return message


def _response(content=None, tool_calls=None, finish_reason='stop', usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=_Message(content, tool_calls),
                                 finish_reason=finish_reason)],
        usage=usage)


class _Scripted:
    """按队列返回响应（或抛异常），并留存每次请求的 messages 快照。"""

    def __init__(self, items):
        self.items = list(items)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs.get('messages', [])))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _fake_exec(tool_call, *, on_event=None, on_activity=None):
    return ('fake', {}, {'role': 'tool', 'tool_call_id': tool_call.id,
                         'content': f'out:{tool_call.id}'}, [])


def test_event_order_assistant_tool_turn_completed(tmp_path, monkeypatch):
    """assistant(tool_calls) → tool 结果 → 最终 assistant → turn_completed。"""
    store = SessionEventStore(tmp_path / 'sessions', session_id='order')
    scripted = _Scripted([
        _response(tool_calls=[_Call('c1', 'bash', '{"command": "echo hi"}')],
                  finish_reason='tool_calls', usage=_usage()),
        _response(content='done', usage=_usage(prompt=120)),
    ])
    monkeypatch.setattr(harness.client.chat.completions, 'create', scripted.create)
    monkeypatch.setattr(harness, 'exec_tool_call', _fake_exec)

    assert harness.agent_loop([{'role': 'user', 'content': 'go'}],
                              session_store=store,
                              housekeeping=False) == 'done'

    events = store.read_events()
    kinds = [event['type'] for event in events]
    assert kinds.count('turn_completed') == 1
    assert kinds[-1] == 'turn_completed'

    def _index(predicate):
        return next(i for i, event in enumerate(events) if predicate(event))

    assistant_at = _index(
        lambda e: e['type'] == 'message'
        and e['message'].get('role') == 'assistant'
        and e['message'].get('tool_calls'))
    tool_at = _index(
        lambda e: e['type'] == 'message'
        and e['message'].get('role') == 'tool'
        and e['message'].get('tool_call_id') == 'c1')
    final_at = _index(
        lambda e: e['type'] == 'message'
        and e['message'].get('content') == 'done')
    assert assistant_at < tool_at < final_at


def test_tool_compact_reestimates_history_and_skips_micro(tmp_path, monkeypatch):
    """compact 整体替换历史后必须重估 token，并把 micro 压缩跳过 3 轮。"""
    store = SessionEventStore(tmp_path / 'sessions', session_id='compact')
    scripted = _Scripted([
        _response(tool_calls=[_Call('c1', 'compact')], finish_reason='tool_calls',
                  usage=_usage()),
        _response(content='done', usage=_usage(prompt=60)),
    ])
    monkeypatch.setattr(harness.client.chat.completions, 'create', scripted.create)
    monkeypatch.setattr(harness, 'compact_history',
                        lambda messages: [{'role': 'user', 'content': '[Compacted] summary'}])

    estimates = []
    monkeypatch.setattr(harness, 'estimate_size',
                        lambda messages: estimates.append(copy.deepcopy(messages)) or 7)

    skip_args = []
    snip_states = []

    def fake_pre_compact(messages, skip=0, **kwargs):
        skip_args.append(skip)
        snip_states.append(kwargs['snip_state'])
        if len(snip_states) == 1:
            kwargs['snip_state']['window'] = 5
        return messages, skip

    monkeypatch.setattr(harness, 'pre_compact_messages', fake_pre_compact)

    assert harness.agent_loop([{'role': 'user', 'content': 'go'}],
                              session_store=store,
                              housekeeping=False) == 'done'

    compacted = [batch for batch in estimates
                 if any('[Compacted]' in str(m.get('content', '')) for m in batch)]
    assert compacted, '整体替换历史后必须以新历史重估 token'
    assert any(m.get('content') == '[Compacted] summary' for m in compacted[0])
    assert skip_args == [0, 3], f'micro 跳过窗口错误: {skip_args}'
    assert snip_states[0] is snip_states[1], 'snip 状态必须跨轮携带同一对象'
    assert snip_states[1]['window'] == 5


def test_reactive_compact_reestimates_history_and_skips_micro(tmp_path, monkeypatch):
    """请求超限触发 reactive compact 后同样重估 token 并跳过 3 轮 micro。"""
    store = SessionEventStore(tmp_path / 'sessions', session_id='reactive')
    scripted = _Scripted([
        _response(tool_calls=[_Call('c1', 'bash')], finish_reason='tool_calls',
                  usage=_usage(prompt=100)),
        RuntimeError('prompt_too_long: reduce input'),
        _response(content='recovered', usage=_usage(prompt=40)),
    ])
    monkeypatch.setattr(harness.client.chat.completions, 'create', scripted.create)
    monkeypatch.setattr(harness, 'exec_tool_call', _fake_exec)
    monkeypatch.setattr(
        harness, 'reactive_compact',
        lambda messages, keep=5, budget_tokens=None: [
            {'role': 'user', 'content': '[Reactive compact] summary'}])

    estimates = []
    monkeypatch.setattr(harness, 'estimate_size',
                        lambda messages: estimates.append(copy.deepcopy(messages)) or 7)

    skip_args = []

    def fake_pre_compact(messages, skip=0, **kwargs):
        skip_args.append(skip)
        return messages, skip

    monkeypatch.setattr(harness, 'pre_compact_messages', fake_pre_compact)

    assert harness.agent_loop([{'role': 'user', 'content': 'go'}],
                              session_store=store,
                              housekeeping=False) == 'recovered'

    assert any(m.get('content') == '[Reactive compact] summary'
               for batch in estimates for m in batch), 'reactive 压缩后必须重估 token'
    assert skip_args == [0, 0, 3], f'micro 跳过窗口错误: {skip_args}'


def test_stream_completion_patch_point_still_applies(tmp_path, monkeypatch):
    """拆分后 _stream_completion 仍是 harness 模块级 patch 点，且收到完整工具面。"""
    store = SessionEventStore(tmp_path / 'sessions', session_id='stream')
    response = _response(content='streamed', usage=_usage())
    seen = {}

    def fake_stream(kwargs, hook, api=None):
        seen['kwargs'] = kwargs
        hook('streamed')
        return response

    monkeypatch.setattr(harness, '_stream_completion', fake_stream)
    monkeypatch.setattr(harness, 'streaming_enabled', lambda: True)

    def no_target(role='main'):
        raise llm_config.ConfigError('test: no model target')

    monkeypatch.setattr(harness, 'model_target', no_target)

    assert harness.agent_loop([{'role': 'user', 'content': 'go'}],
                              session_store=store,
                              housekeeping=False) == 'streamed'
    assert seen['kwargs']['tools'] is harness.TOOLS
