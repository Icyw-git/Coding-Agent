"""记忆召回策略测试：HARNESS_MEMORY_RECALL = llm / local / off。

- off / HARNESS_LOAD_MEMORIES=0：零 LLM 调用，返回空
- local：中文二元组 + ASCII 关键词打分；偏好命中、无关不召回、
  话题切换（窗口外旧话题不再命中）、零 LLM 调用
- llm：保持旧行为（一次筛选往返）；LLM 异常时 fallback 到本地筛选

运行: python test_memory_recall.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pytest

import agent_core
import harness


def _make_memory(dirname: Path, filename: str, name: str, description: str,
                 body: str = '内容正文。') -> None:
    dirname.mkdir(parents=True, exist_ok=True)
    (dirname / filename).write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: user\n---\n{body}",
        encoding='utf-8')


@pytest.fixture()
def memory_dir(tmp_path, monkeypatch):
    d = tmp_path / 'memories'
    monkeypatch.setattr(harness, 'MEMORY_DIR', d)
    monkeypatch.setattr(harness, 'MEMORY_INDEX', d / 'MEMORY.md')
    return d


def _never_llm(monkeypatch):
    def boom(**kwargs):
        raise AssertionError('local/off 模式不应调用 LLM')
    monkeypatch.setattr(harness.client.chat.completions, 'create', boom)


def test_off_returns_empty_without_llm(memory_dir, monkeypatch):
    _make_memory(memory_dir, 'style.md', 'user-style', '用户偏好简短回答')
    _never_llm(monkeypatch)
    monkeypatch.setenv('HARNESS_MEMORY_RECALL', 'off')
    assert agent_core.load_memories(
        [{'role': 'user', 'content': '怎么回答？'}]) == ''


def test_legacy_load_memories_zero_still_off(memory_dir, monkeypatch):
    _make_memory(memory_dir, 'style.md', 'user-style', '用户偏好简短回答')
    _never_llm(monkeypatch)
    monkeypatch.setenv('HARNESS_LOAD_MEMORIES', '0')
    monkeypatch.delenv('HARNESS_MEMORY_RECALL', raising=False)
    assert agent_core.load_memories(
        [{'role': 'user', 'content': '怎么回答？'}]) == ''


def test_local_chinese_pref_selected(memory_dir):
    _make_memory(memory_dir, 'style.md', 'user-style',
                 '用户偏好简短回答，不要长篇大论')
    _make_memory(memory_dir, 'deploy.md', 'project-deploy',
                 '部署流程先跑测试再推服务器')
    selected = agent_core.local_relevant_memories(
        [{'role': 'user', 'content': '回答请保持简短一些'}])
    assert selected == ['style.md']


def test_local_ignores_unrelated_topic(memory_dir):
    _make_memory(memory_dir, 'deploy.md', 'project-deploy',
                 '部署流程先跑测试再推服务器')
    selected = agent_core.local_relevant_memories(
        [{'role': 'user', 'content': '今天天气不错，适合散步'}])
    assert selected == []


def test_local_topic_switch_drops_old_topic(memory_dir):
    """话题切换：旧话题滑出最近 3 条窗口后不再命中。"""
    _make_memory(memory_dir, 'feishu.md', 'feishu-gateway', '飞书网关长连接与审批')
    _make_memory(memory_dir, 'db.md', 'db-schema', '数据库表结构与迁移约定')
    msgs = [
        {'role': 'user', 'content': '飞书网关的审批怎么配置？'},   # 旧话题，滑出窗口
        {'role': 'assistant', 'content': '……'},
        {'role': 'user', 'content': '好的，换个问题'},
        {'role': 'user', 'content': '数据库迁移有什么约定？'},
        {'role': 'user', 'content': '表结构变更需要注意什么？'},
    ]
    selected = agent_core.local_relevant_memories(msgs)
    assert selected == ['db.md']


def test_local_ascii_keyword_single_hit(memory_dir):
    _make_memory(memory_dir, 'ds.md', 'llm-provider', 'deepseek API 上游配置')
    selected = agent_core.local_relevant_memories(
        [{'role': 'user', 'content': 'deepseek 的超时怎么改？'}])
    assert selected == ['ds.md']   # 单个 ASCII 关键词权重 2 即达阈值


def test_local_no_llm_call(memory_dir, monkeypatch):
    _make_memory(memory_dir, 'style.md', 'user-style', '用户偏好简短回答')
    _never_llm(monkeypatch)
    monkeypatch.setenv('HARNESS_MEMORY_RECALL', 'local')
    out = agent_core.load_memories(
        [{'role': 'user', 'content': '回答保持简短'}])
    assert 'user-style' in out


def test_llm_mode_keeps_old_behavior(memory_dir, monkeypatch):
    _make_memory(memory_dir, 'a.md', 'user-style', '用户偏好简短回答')
    _make_memory(memory_dir, 'b.md', 'project-deploy', '部署流程先跑测试')
    called = {}

    def fake_create(**kwargs):
        called['yes'] = True
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='[1]'))])

    monkeypatch.setenv('HARNESS_MEMORY_RECALL', 'llm')
    monkeypatch.setattr(harness.client.chat.completions, 'create', fake_create)
    out = agent_core.load_memories([{'role': 'user', 'content': '部署流程是什么？'}])
    assert called.get('yes'), 'llm 模式应保留筛选往返'
    assert 'project-deploy' in out


def test_llm_failure_falls_back_to_local(memory_dir, monkeypatch):
    _make_memory(memory_dir, 'style.md', 'user-style', '用户偏好简短回答')

    def boom(**kwargs):
        raise RuntimeError('upstream down')

    monkeypatch.setenv('HARNESS_MEMORY_RECALL', 'llm')
    monkeypatch.setattr(harness.client.chat.completions, 'create', boom)
    selected = agent_core.select_relevant_memories(
        [{'role': 'user', 'content': '回答保持简短'}])
    assert selected == ['style.md']


def test_latest_question_survives_long_older_message():
    files = [{'filename': 'feishu.md', 'name': 'feishu', 'description': '飞书网关审批配置'}]
    messages = [
        {'role': 'user', 'content': '旧背景' * 1000},
        {'role': 'user', 'content': '飞书网关审批怎么配置？'},
    ]
    assert agent_core.local_relevant_memories(messages, files) == ['feishu.md']
    assert len(agent_core._recent_user_text(messages)) == 2000


def test_recall_reads_dict_text_blocks():
    messages = [{'role': 'user', 'content': [
        {'type': 'text', 'text': '飞书网关审批'},
        {'type': 'image_url', 'image_url': {'url': 'ignored'}},
        SimpleNamespace(type='text', text='配置'),
    ]}]
    assert agent_core._recent_user_text(messages) == '飞书网关审批 配置'
    assert agent_core._recent_user_text(messages, max_chars=0) == ''
