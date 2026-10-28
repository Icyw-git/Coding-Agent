"""记忆评估集（B4）：按 PAHF 的 eval 框架测记忆质量。

为什么这不是普通单测
--------------------
普通单测验证"函数返回了什么"。记忆系统的失败模式是**时间性的**：
同一条信息在场景 A 应该记住、在场景 B 应该忘掉；旧偏好不能压过当轮指令。
这些只能用**场景**表达，不能用单点断言。

PAHF 把记忆评估分两组：

- **Distillation**（写了什么）：precision（只存 durable）、recall（关键偏好没漏）、
  safety（敏感写入被拦）
- **Injection**（怎么用）：recency correctness（冲突时用最新）、
  over-influence（记忆不该压过当轮指令）、token efficiency

另外 EvoMemBench 的 FAMA 指标提醒：**只测"记住没有"会奖励"什么都记"**，
必须同时测"该忘的忘了没有"（`test_superseded_memory_leaves_recall`）。

这些用例是**契约**：改动 schema / 召回 / 合并之后，它们必须继续通过。
"""
from __future__ import annotations

import pytest

import memory
import memory_schema

# ---------------------------------------------------------------- 场景夹具


@pytest.fixture
def home(tmp_path, monkeypatch):
    """把记忆目录指到临时目录，并清掉解析缓存。"""
    import llm_route
    h = llm_route.harness()
    memories = tmp_path / 'memories'
    memories.mkdir()
    monkeypatch.setattr(h, 'MEMORY_DIR', memories, raising=False)
    monkeypatch.setattr(h, 'MEMORY_INDEX', memories / 'MEMORY.md', raising=False)
    memory._cache_clear()
    return memories


def _write(memories, name, description, body, mem_type='preference', **extra):
    entry = {'name': name, 'description': description, 'type': mem_type,
             'body': body, **extra}
    text = memory_schema.render_frontmatter(entry) + f'\n{body}\n'
    (memories / f'{name}.md').write_text(text, encoding='utf-8')
    memory._cache_clear()
    return memories / f'{name}.md'


# ---------------------------------------------------------------- Precision
# 只存 durable 的：一次性范围的东西不该变成长期偏好

def test_durable_preference_is_kept(home):
    """跨场景成立的偏好应该留下。"""
    _write(home, 'pref-aisle', 'prefers aisle seats', 'Prefers aisle seats.')
    names = [item['name'] for item in memory.list_memory_files()]
    assert 'pref-aisle' in names


def test_session_scoped_note_is_kept_but_marked(home):
    """一次性范围（"这次"）允许存，但必须标成 session，不能冒充 global。"""
    _write(home, 'trip-nobag', 'this trip: no checked bags',
           'For this trip only, avoid checked bags.', scope='session')
    entry = next(item for item in memory.list_memory_files()
                 if item['name'] == 'trip-nobag')
    assert entry['scope'] == 'session'


# ---------------------------------------------------------------- Recall
# 关键偏好出现时要召回得到

def test_conditional_preference_is_recalled_by_condition(home):
    """条件性偏好的关键词在**条件词**上：问"困了"要能召回"困了想喝茶"。"""
    _write(home, 'sleepy-tea', 'when sleepy: prefers tea over cola',
           'When sleepy, prefers tea. Otherwise cola is fine.',
           keywords=['sleepy', 'tea'])
    recalled = memory.local_relevant_memories(
        [{'role': 'user', 'content': '我困了，给我点喝的 sleepy'}])
    assert 'sleepy-tea.md' in recalled


def test_unrelated_memory_is_not_recalled(home):
    _write(home, 'pref-aisle', 'prefers aisle seats', 'Aisle seats.', keywords=['aisle', 'seat'])
    recalled = memory.local_relevant_memories(
        [{'role': 'user', 'content': '帮我看看这个部署脚本'}])
    assert 'pref-aisle.md' not in recalled


# ---------------------------------------------------------------- Recency
# 冲突时用最新那条

def test_newer_memory_wins_on_same_topic(home):
    """同主题两条时，后写的 last_update_date 更晚，应排在前面。"""
    _write(home, 'deploy-old', 'deploy via script.sh', 'old way',
           mem_type='fact', keywords=['deploy'], last_update_date='2024-01-01')
    _write(home, 'deploy-new', 'deploy via make ship-prod', 'new way',
           mem_type='fact', keywords=['deploy'], last_update_date='2026-10-01')
    entries = memory.list_memory_files()
    dates = {item['name']: item['last_update_date'] for item in entries}
    assert dates['deploy-new'] > dates['deploy-old']


# ---------------------------------------------------------------- FAMA：该忘的忘了没有

def test_superseded_memory_leaves_recall(home):
    """被修正的记忆必须退出召回。

    这是 FAMA 惩罚的行为：错误假设和它的修正同时可召回，取到错的那个就等于
    "没忘干净"。文件保留在磁盘上（不删用户数据），但不参与检索。
    """
    _write(home, 'deploy-wrong', 'deploy via script.sh', 'wrong',
           mem_type='fact', keywords=['deploy'], superseded_by='deploy-right')
    _write(home, 'deploy-right', 'deploy via make ship-prod', 'right',
           mem_type='correction', keywords=['deploy'])
    names = [item['name'] for item in memory.list_memory_files()]
    assert 'deploy-wrong' not in names
    assert 'deploy-right' in names
    # 文件本身没被删
    assert (home / 'deploy-wrong.md').is_file()


# ---------------------------------------------------------------- Over-influence

def test_memory_block_declares_current_intent_wins(home):
    """injection eval 的 over-influence 项：记忆不能压过当轮指令。"""
    _write(home, 'pref-brief', 'prefers brief answers', 'Keep it short.',
           keywords=['brief', 'answer'])
    block = memory.load_memories([{'role': 'user', 'content': 'brief answer please'}])
    assert block
    assert memory.MEMORY_PRECEDENCE in block
    # 声明必须在正文之前
    assert block.index(memory.MEMORY_PRECEDENCE) < block.index('Keep it short')


# ---------------------------------------------------------------- 蒸馏：schema 往返

@pytest.mark.parametrize('desc', [
    'when sleepy: prefers tea',      # 带冒号：条件偏好的标准写法
    'prefers tabs over spaces',
    'has #hash and : colon',
])
def test_conditional_description_survives_roundtrip(home, desc):
    """带冒号的描述不能让整个 frontmatter 变成空映射。

    实测踩过：`description: when sleepy: prefers tea` 会让 YAML 报
    "mapping values are not allowed here"，type/scope/keywords 全丢。
    """
    _write(home, 'x', desc, 'body')
    entry = next(item for item in memory.list_memory_files() if item['name'] == 'x')
    assert entry['description'] == desc
    assert entry['type'] == 'preference'


def test_legacy_type_vocabulary_still_readable(home):
    """旧文件用 user/feedback/project/reference，仍要能读且归一。"""
    (home / 'legacy.md').write_text(
        '---\nname: legacy\ndescription: old one\ntype: user\n---\n\nbody\n',
        encoding='utf-8')
    memory._cache_clear()
    entry = next(item for item in memory.list_memory_files()
                 if item['name'] == 'legacy')
    assert entry['type'] == 'preference'


# ---------------------------------------------------------------- Safety

def test_secret_is_redacted_before_disk(home):
    text = memory._render_memory('creds', 'fact', 'api key',
                                 'key is sk-' + 'a' * 24)
    assert 'sk-' + 'a' * 24 not in text
    assert '[redacted:openai_key]' in text


# ---------------------------------------------------------------- Token 效率

def test_recall_is_bounded(home):
    """召回条数有上限：记忆多了不能把请求撑爆。"""
    for index in range(20):
        _write(home, f'note-{index}', f'note about deploy {index}',
               f'body {index}', keywords=['deploy'])
    recalled = memory.local_relevant_memories(
        [{'role': 'user', 'content': 'deploy 部署'}], max_items=5)
    assert len(recalled) <= 5
