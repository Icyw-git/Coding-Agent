"""post-action 反馈信号（D1）。

PAHF 论文的核心结论：持续个性化的第三步——行动后收到反馈写回记忆——才是纠错
的关键，**pre-action 询问无法解决偏好漂移**。

典型例子（论文原文）：第一天记「Kate 喜欢可乐」，第二天她困了、送上可乐、她说
「我困的时候更想喝茶」。只记「喜欢可乐」会永久错，正确记忆必须带条件。
"""
from __future__ import annotations

import pytest

import memory_feedback as mf

# ---------------------------------------------------------------- 显式纠正

@pytest.mark.parametrize('text', [
    '不对，我要的是按周分组',
    '不是这样，重来',
    '你搞错了',
    '我说的是生产环境',
    'no, that is not what I asked',
])
def test_explicit_correction_detected(text):
    messages = [
        {'role': 'user', 'content': '帮我整理报告'},
        {'role': 'assistant', 'content': '好的'},
        {'role': 'user', 'content': text},
    ]
    signals = mf.collect(messages)
    assert any(item['kind'] == 'correction' for item in signals), text
    assert signals[0]['weight'] == 'high'


def test_first_message_is_not_a_correction():
    """会话首轮说「不是这样」不是对模型的纠正。"""
    messages = [{'role': 'user', 'content': '不对，我要改的是另一个文件'}]
    assert mf.collect(messages) == []


# ---------------------------------------------------------------- 条件性偏好（PAHF 核心）

def test_pahf_conditional_preference_is_detected():
    """「我困的时候更想喝茶」——不含否定词，但是关键的偏好收窄。"""
    messages = [
        {'role': 'user', 'content': '我困了，给我杯饮料'},
        {'role': 'assistant', 'content': '已经送可乐了'},
        {'role': 'user', 'content': '我困的时候更想喝茶'},
    ]
    signals = mf.collect(messages)
    assert [item['kind'] for item in signals] == ['refine']
    assert signals[0]['weight'] == 'high'


def test_refine_prompt_asks_for_conditional_form():
    """提示词必须要求保留条件，不能覆盖成无条件偏好。"""
    block = mf.as_prompt_block([{'kind': 'refine', 'quote': 'x', 'weight': 'high'}])
    assert 'CONDITIONAL' in block
    assert 'when <condition>' in block


# ---------------------------------------------------------------- 正反馈

@pytest.mark.parametrize('text', ['好', '好的', '可以', '就这样', '不错', 'OK', 'LGTM'])
def test_approval_detected(text):
    messages = [
        {'role': 'user', 'content': '查天气'},
        {'role': 'assistant', 'content': '晴'},
        {'role': 'user', 'content': text},
    ]
    signals = mf.collect(messages)
    assert any(item['kind'] == 'approval' and item['weight'] == 'low'
               for item in signals), text


@pytest.mark.parametrize('text', [
    '好主意',                       # 评价提议，不是满意
    '可以吗',                       # 疑问
    '好的话说回来那个脚本还有问题',    # 转折
    '好的，但能不能再快一点',          # 有条件
])
def test_not_approval(text):
    """整句匹配，不是前缀匹配：这三个都不该算认可。"""
    assert not mf._is_approval(text)


# ---------------------------------------------------------------- 失败反馈

def test_tool_failure_is_recorded():
    messages = [
        {'role': 'user', 'content': '跑测试'},
        {'role': 'assistant', 'content': '好的'},
        {'role': 'tool', 'content': 'Error: command not found'},
        {'role': 'user', 'content': '那就用 python -m pytest'},
    ]
    signals = mf.collect(messages)
    assert any(item['kind'] == 'failure' for item in signals)


def test_successful_tool_is_not_a_failure():
    messages = [
        {'role': 'user', 'content': '跑测试'},
        {'role': 'tool', 'content': '120 passed'},
        {'role': 'user', 'content': '好的'},
    ]
    signals = mf.collect(messages)
    assert not any(item['kind'] == 'failure' for item in signals)


# ---------------------------------------------------------------- 排序与上限

def test_correction_outranks_approval():
    messages = [
        {'role': 'user', 'content': 'x'},
        {'role': 'assistant', 'content': 'y'},
        {'role': 'user', 'content': '好的'},
        {'role': 'user', 'content': '不对，重来'},
    ]
    signals = mf.collect(messages)
    assert signals[0]['kind'] == 'correction'


def test_signals_are_capped():
    messages = [{'role': 'user', 'content': 'x'}, {'role': 'assistant', 'content': 'y'}]
    messages += [{'role': 'user', 'content': '不对，再来一次'} for _ in range(20)]
    assert len(mf.collect(messages)) <= mf.MAX_SIGNALS


def test_duplicates_are_removed():
    messages = [
        {'role': 'user', 'content': 'x'},
        {'role': 'assistant', 'content': 'y'},
        {'role': 'user', 'content': '好的'},
        {'role': 'user', 'content': '好的'},
    ]
    signals = [item for item in mf.collect(messages) if item['kind'] == 'approval']
    assert len(signals) == 1


# ---------------------------------------------------------------- 边界

def test_empty_messages():
    assert mf.collect([]) == []


def test_multimodal_content_is_flattened():
    messages = [
        {'role': 'user', 'content': 'x'},
        {'role': 'assistant', 'content': 'y'},
        {'role': 'user', 'content': [{'type': 'text', 'text': '不对，改一下'}]},
    ]
    assert any(item['kind'] == 'correction' for item in mf.collect(messages))


def test_prompt_block_empty_without_signals():
    assert mf.as_prompt_block([]) == ''


def test_quote_is_truncated():
    long_text = '不对' + '很长的说明' * 100
    messages = [
        {'role': 'user', 'content': 'x'},
        {'role': 'assistant', 'content': 'y'},
        {'role': 'user', 'content': long_text},
    ]
    quote = mf.collect(messages)[0]['quote']
    assert len(quote) <= mf.QUOTE_CHARS + 1


# ---------------------------------------------------------------- 接入 extract_memories

def test_extract_prompt_includes_feedback_block(monkeypatch):
    """信号必须真的进到抽取提示词里，否则等于没做。"""
    import memory

    captured = {}

    class _Resp:
        class _Choice:
            class _Msg:
                content = '[]'
                reasoning_content = ''
            message = _Msg()
        choices = [_Choice()]

    def fake_completion(_role, messages, **_kwargs):
        captured['prompt'] = messages[0]['content']
        return _Resp()

    monkeypatch.setattr('llm_route.completion', fake_completion)
    monkeypatch.setattr(memory, 'list_memory_files', lambda: [])

    memory.extract_memories([
        {'role': 'user', 'content': '整理报告'},
        {'role': 'assistant', 'content': '好的'},
        {'role': 'user', 'content': '不对，我要按周分组'},
    ])

    prompt = captured['prompt']
    assert 'USER CORRECTED' in prompt
    assert 'REPLACES the earlier wrong assumption' in prompt


# ---------------------------------------------------------------- D2 覆盖语义

def test_consolidate_prompt_makes_correction_win():
    """D2：一条错误假设和它的修正同时存在时，召回可能取到错的那个。"""
    import inspect

    import memory
    source = inspect.getsource(memory.consolidate_memories)
    assert 'A correction ALWAYS wins' in source
    assert 'delete the old one instead of keeping both' in source


def test_consolidate_prompt_protects_conditions():
    """条件性偏好不是矛盾：不能因为「喜欢可乐」就把「困了想喝茶」删掉。"""
    import inspect

    import memory
    source = inspect.getsource(memory.consolidate_memories)
    assert 'Conditional preferences are not contradictions' in source
    assert 'Never drop the condition' in source


# ---------------------------------------------------------------- D4 记忆来源

def test_render_memory_records_source():
    import memory
    text = memory._render_memory('n', 'preference', 'd', 'body',
                                 source='sess-123', quote='我周三下午不看代码')
    assert 'source_session: sess-123' in text
    assert 'source_quote:' in text


def test_render_memory_without_source_is_clean():
    """没有来源信息时不应该写空字段。"""
    import memory
    text = memory._render_memory('n', 'preference', 'd', 'body')
    assert 'source_session' not in text
    assert 'source_quote' not in text


def test_source_quote_is_redacted():
    """原话里可能夹带密钥——来源字段同样要脱敏。"""
    import memory
    text = memory._render_memory('n', 'preference', 'd', 'body',
                                 quote='我的 key 是 sk-' + 'a' * 24)
    assert 'sk-' + 'a' * 24 not in text
    assert '[redacted:openai_key]' in text


def test_source_quote_is_single_line_and_bounded():
    import memory
    text = memory._render_memory('n', 'preference', 'd', 'body',
                                 quote='第一行\n第二行' + 'x' * 500)
    line = next(l for l in text.splitlines() if l.startswith('source_quote:'))
    assert len(line) < 260


def test_remember_stores_session_as_source():
    """remember 工具必须把当前会话 id 记下来，否则 /why 无法追溯。"""
    import inspect

    import tool_memory
    source = inspect.getsource(tool_memory.remember)
    assert 'current_session_id' in source
    assert 'source=session_id' in source
