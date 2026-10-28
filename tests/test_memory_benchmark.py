"""记忆召回基准：用数字衡量"能不能召回对的"，而不是只测机制不报错。

为什么单独一个文件
------------------
`test_memory_eval.py` 测的是**语义契约**（superseded 要退出召回、条件偏好要按
条件词召回），用精确断言。这个文件测的是**召回质量**：给一批真实风格的查询，
算命中率和排序，用数字守住回归。

两组数据，用来暴露能力的**边界**而不是只报喜：

- ``IN_VOCAB``：术语表覆盖内的常用开发/偏好表达 —— 这是日常多数情况。
- ``OUT_OF_VOCAB``：术语表**没有**覆盖的说法 —— 用来量化"字面匹配"的天花板，
  避免"测了 100% 就以为语义召回也行了"。

``OUT_OF_VOCAB`` 的失败是**已知且记录在案**的设计限制，不是 bug；断言它保持
失败，是为了将来真做了语义召回时能立刻看到变化。
"""
from __future__ import annotations

import pytest

import harness
import memory

#: 记忆库：(name, type, description, keywords)。keywords 留空表示由描述派生，
#: 模拟"抽取时没给双语关键词、只靠术语表兜底"的老记忆。
STORE = [
    ('deploy-prod', 'fact', 'production deploys go through make ship-prod', ''),
    ('deploy-staging', 'fact', 'staging deploys use make ship-staging', ''),
    ('test-cmd', 'fact', 'run tests with python -m pytest -m not-slow', ''),
    ('repo-layout', 'context', 'src holds runtime modules, tests mirrors them', ''),
    ('api-key-rotation', 'context', 'API keys rotate every 90 days', ''),
    ('sleepy-tea', 'preference', 'when sleepy: prefers tea over coffee', ''),
    ('pref-brief', 'preference', 'prefers brief answers without preamble', ''),
    ('pref-chinese', 'preference', 'prefers replies in Chinese', ''),
    ('commit-style', 'correction', 'commit messages use imperative mood', ''),
    ('pref-tabs', 'preference', 'prefers tabs over spaces in code', ''),
]

#: 命中集合：中文查询（记忆是英文写的，考验术语表兜底）
IN_VOCAB = [
    ('production 部署怎么发？', 'deploy-prod'),
    ('staging 怎么发', 'deploy-staging'),
    ('跑一下测试', 'test-cmd'),
    ('回答简短点', 'pref-brief'),
    ('用中文回复我', 'pref-chinese'),
    ('我困了想喝点东西', 'sleepy-tea'),
    ('代码缩进用 tab 还是空格', 'pref-tabs'),
    ('commit message 怎么写', 'commit-style'),
    ('密钥多久轮换一次', 'api-key-rotation'),
    ('src 目录放什么', 'repo-layout'),
    # 英文查询（同语言，字面直接对得上）
    ('how do I deploy to production', 'deploy-prod'),
    ('which test command', 'test-cmd'),
    ('keep answers brief', 'pref-brief'),
]

#: 术语表**未覆盖**的中文表达。字面匹配必然漏，用来量化能力边界。
#: 挑选时确保不含任何术语表词（含了就会命中，就不是"表外"了）。
OUT_OF_VOCAB = [
    ('上线流程是什么', 'deploy-prod'),      # 「上线」不在表里（「部署/发布」才在）
    ('别啰嗦，说重点', 'pref-brief'),        # 「啰嗦/重点」不在表里
    ('代码怎么组织的', 'repo-layout'),       # 「组织」不在表里（「目录/结构」才在）
]


@pytest.fixture
def store(tmp_path, monkeypatch):
    h = harness
    d = tmp_path / 'mem'
    d.mkdir()
    monkeypatch.setattr(h, 'MEMORY_DIR', d, raising=False)
    monkeypatch.setattr(h, 'MEMORY_INDEX', d / 'MEMORY.md', raising=False)
    memory._cache_clear()
    for name, mem_type, desc, keywords in STORE:
        memory.write_memory_file(name, mem_type, desc, desc,
                                 keywords=keywords or None)
    yield d
    memory._cache_clear()


def _recall(query: str, k: int = 3):
    return [name.replace('.md', '') for name in
            memory.local_relevant_memories(
                [{'role': 'user', 'content': query}], max_items=k)]


def _rank_of(expect: str, got: list[str]) -> int:
    return got.index(expect) + 1 if expect in got else 0


def test_in_vocabulary_recall_is_high(store):
    """常用表达必须召回得到，且大多排第一。"""
    hits = sum(1 for query, expect in IN_VOCAB if expect in _recall(query))
    print(f'\nIN_VOCAB 命中 {hits}/{len(IN_VOCAB)}')
    assert hits >= 12, f'常用表达召回退化了: {hits}/{len(IN_VOCAB)}'


def test_in_vocabulary_ranks_correct_memory_first(store):
    """召回得到还不够——排第一才有用（注入有 token 预算）。"""
    first = sum(1 for query, expect in IN_VOCAB
                if _rank_of(expect, _recall(query)) == 1)
    print(f'IN_VOCAB 首位命中 {first}/{len(IN_VOCAB)}')
    assert first >= 10, f'排序退化了: {first}/{len(IN_VOCAB)}'


def test_unrelated_query_returns_nothing(store):
    """无关查询不该硬凑召回——错召回比不召回更糟（会误导模型）。"""
    for query in ('帮我重启一下数据库容器', '今天天气怎么样'):
        got = _recall(query)
        assert not got, f'{query!r} 不该召回 {got}'


def test_out_of_vocabulary_is_a_known_limit(store):
    """字面匹配的天花板：术语表外的说法召不回。

    这条**断言失败**（即保持召回不到），把限制记录成可观测事实：
    将来换成语义召回时它会开始失败，提醒我们更新这组数据和文档。
    """
    missed = [query for query, expect in OUT_OF_VOCAB
              if expect not in _recall(query)]
    print(f'\nOUT_OF_VOCAB 召不回 {len(missed)}/{len(OUT_OF_VOCAB)}')
    assert len(missed) == len(OUT_OF_VOCAB), (
        '术语表外已经能召回了？说明能力变了，更新这组数据和 '
        'docs/PERSONAL_AGENT_LEARNING_RSI_DESIGN.md 的限制说明')


def test_injected_block_is_token_bounded(store):
    """召回结果进入请求的体积要可控，不能随记忆条数线性膨胀。"""
    block = memory.load_memories([{'role': 'user', 'content': 'deploy 部署'}])
    assert block
    # 3 条上限 × 单条正文，留足余量；超了说明召回或裁剪出了问题
    assert len(block) < 4000, f'召回块 {len(block)} 字符，过大'


def test_recall_cost_is_local(store, monkeypatch):
    """召回决策不得发起网络请求（这是本地筛选存在的意义）。"""
    def boom(*_args, **_kwargs):
        raise AssertionError('recall must not call the model')

    monkeypatch.setattr('llm_route.completion', boom)
    assert _recall('deploy 部署怎么发')
