"""A1：召回块的优先级声明。

记忆块追加在**用户消息之后**（为保住前缀缓存），而模型对末尾内容权重高。
不声明优先级，旧偏好就会盖过用户当轮明确指令——这正是 PAHF injection eval
里 "over-influence" 那一项。
"""
from __future__ import annotations

import memory


def test_precedence_statement_exists():
    assert 'overrides' in memory.MEMORY_PRECEDENCE
    assert 'latest message' in memory.MEMORY_PRECEDENCE


def test_precedence_is_first_line_of_block(monkeypatch, tmp_path):
    """必须是第一行：放在中间或末尾都不起作用。"""
    import llm_route
    home = llm_route.harness()
    memories = tmp_path / 'memories'
    memories.mkdir()
    monkeypatch.setattr(home, 'MEMORY_DIR', memories, raising=False)
    monkeypatch.setattr(home, 'MEMORY_INDEX', memories / 'MEMORY.md', raising=False)
    (memories / 'x.md').write_text(
        '---\nname: x\ndescription: about deploy\ntype: preference\n---\n\nbody',
        encoding='utf-8')
    memory._cache_clear()

    messages = [{'role': 'user', 'content': 'about deploy'}]
    block = memory.load_memories(messages)
    assert block, 'should recall the memory'
    lines = [l for l in block.splitlines() if l.strip()]
    assert lines[0] == '<relevant_memories>'
    assert lines[1] == memory.MEMORY_PRECEDENCE
