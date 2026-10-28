"""记忆的显式工具（M1）与落盘脱敏（N1）。

M1：memory.py 的自动抽取一直在跑，缺的是「点名记住 / 点名删掉 / 主动查」。
N1：记忆是长期状态，一条密钥被记下来会跟着每一次召回，泄露面比会话大得多。
"""
from __future__ import annotations

import pytest

import memory_redact
import tool_memory


@pytest.fixture
def memory_home(tmp_path, monkeypatch):
    """把 MEMORY_DIR/INDEX 指到临时目录，避免碰真实记忆。"""
    import llm_route
    home = llm_route.harness()
    memories = tmp_path / 'memories'
    memories.mkdir()
    monkeypatch.setattr(home, 'MEMORY_DIR', memories, raising=False)
    monkeypatch.setattr(home, 'MEMORY_INDEX', memories / 'MEMORY.md', raising=False)
    monkeypatch.setattr(home, 'MEMORY_TYPES',
                        ['preference', 'correction', 'fact', 'context'], raising=False)
    import memory
    memory._cache_clear()
    return memories


# ---------------------------------------------------------------- M1 remember

def test_remember_writes_a_file(memory_home):
    out = tool_memory.remember('prefer tabs over spaces', mem_type='preference')
    assert out.startswith('Saved memory')
    assert 'type=preference' in out
    assert list(memory_home.glob('*.md'))


def test_remember_rejects_empty_text(memory_home):
    assert tool_memory.remember('   ').startswith('Error: text is required')


@pytest.mark.parametrize('kind', tool_memory.TYPES)
def test_remember_accepts_every_declared_type(memory_home, kind):
    assert tool_memory.remember(f'something about {kind}', mem_type=kind).startswith('Saved')


def test_remember_rejects_unknown_type(memory_home):
    """类型必须收窄：自由文本型别会让记忆分类失去意义。"""
    out = tool_memory.remember('x', mem_type='random')
    assert 'Error: type must be one of' in out


def test_remember_derives_name_from_text(memory_home):
    out = tool_memory.remember('deploys go through make ship-prod')
    assert out.startswith('Saved memory "deploys go through make')
    assert 'type=preference' in out


# ---------------------------------------------------------------- M1 recall

def test_recall_empty_query_lists_all(memory_home):
    tool_memory.remember('alpha fact', name='alpha')
    tool_memory.remember('beta fact', name='beta')
    out = tool_memory.recall('')
    assert 'alpha' in out and 'beta' in out


def test_recall_finds_by_keyword(memory_home):
    tool_memory.remember('production deploys use make ship-prod', name='deploy')
    tool_memory.remember('unrelated note', name='other')
    out = tool_memory.recall('ship-prod')
    assert 'deploy' in out
    assert 'other' not in out


def test_recall_reports_no_match_with_next_step(memory_home):
    tool_memory.remember('something', name='thing')
    out = tool_memory.recall('nonexistent-token')
    assert 'no memories matching' in out
    assert 'empty query' in out      # 给出下一步动作


def test_recall_on_empty_store(memory_home):
    assert tool_memory.recall('') == '(no memories saved yet)'


def test_recall_respects_limit(memory_home):
    for i in range(5):
        tool_memory.remember(f'item number {i}', name=f'n{i}')
    out = tool_memory.recall('', limit=2)
    assert '3 more match' in out


def test_recall_rejects_bad_limit(memory_home):
    assert tool_memory.recall('', limit='abc').startswith('Error:')


# ---------------------------------------------------------------- M1 forget

def test_forget_removes_memory(memory_home):
    tool_memory.remember('temporary thing', name='temp')
    assert 'temp' in tool_memory.recall('')
    assert tool_memory.forget('temp').startswith('Forgotten')
    assert 'temp' not in tool_memory.recall('')


def test_forget_unknown_lists_known_names(memory_home):
    tool_memory.remember('known thing', name='known')
    out = tool_memory.forget('missing')
    assert 'no memory named' in out
    assert 'known' in out


def test_forget_requires_name(memory_home):
    assert tool_memory.forget('').startswith('Error: name is required')


def test_forget_does_not_escape_memory_dir(memory_home, tmp_path):
    """名字穿越必须被挡住，不能删到 memories/ 之外。"""
    outside = tmp_path / 'outside.md'
    outside.write_text('keep me', encoding='utf-8')
    assert tool_memory.forget('../../outside') is not None
    assert outside.exists()


# ---------------------------------------------------------------- N1 落盘脱敏

def test_redact_openai_key():
    out, kinds = memory_redact.redact('key is sk-abcdefghijklmnopqrstuvwxyz123456')
    assert '[redacted:openai_key]' in out
    assert 'sk-abc' not in out
    assert 'openai_key' in kinds


def test_redact_private_key_block():
    text = ('-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n'
            '-----END RSA PRIVATE KEY-----')
    out, kinds = memory_redact.redact(text)
    assert 'MIIEow' not in out
    assert 'private_key' in kinds


def test_redact_kv_forms():
    for text in ('API_KEY=supersecret123', 'password: hunter2000',
                 'db password is "p@ssw0rd!!"'):
        out, kinds = memory_redact.redact(text)
        assert '[redacted:kv]' in out, text
        assert 'kv' in kinds


def test_redact_cjk_forms():
    out, kinds = memory_redact.redact('我的密码是 hunter2000')
    assert '[redacted:kv]' in out
    assert 'kv_cjk' in kinds


def test_redact_leaves_natural_language_alone():
    """'token 预算是 8000' 是自然语言，不是凭据：过度脱敏会让记忆不可用。"""
    out, kinds = memory_redact.redact('token 预算是 8000')
    assert out == 'token 预算是 8000'
    assert kinds == []


def test_redact_plain_text_unchanged():
    out, kinds = memory_redact.redact('deploys go through make ship-prod')
    assert kinds == []
    assert out == 'deploys go through make ship-prod'


def test_redact_known_credential_value(monkeypatch):
    """已知真实凭据值比形态匹配可靠：值一定对。"""
    out, kinds = memory_redact.redact(
        'the key is zzzzzzzzzzzzzzzz', extra_values=['zzzzzzzzzzzzzzzz'])
    assert 'zzzz' not in out
    assert 'known_credential' in kinds


def test_redact_ignores_short_extra_values():
    """太短的值会造成大范围误替换（比如 'abc'）。"""
    out, kinds = memory_redact.redact('abc def', extra_values=['abc'])
    assert out == 'abc def'
    assert kinds == []


def test_redact_disable_switch(monkeypatch):
    monkeypatch.setenv(memory_redact.ENV, '0')
    out, kinds = memory_redact.redact('password: hunter2000')
    assert out == 'password: hunter2000'
    assert kinds == []


def test_contains_secret_detects():
    assert memory_redact.contains_secret('ghp_' + 'a' * 30)
    assert not memory_redact.contains_secret('just a normal sentence')


# ---------------------------------------------------------------- N1 接入写入路径

def test_remember_redacts_before_disk(memory_home):
    """最关键的断言：密钥绝不能被写进长期记忆。"""
    tool_memory.remember('my api key is sk-abcdefghijklmnopqrstuvwxyz123456',
                         name='creds')
    written = ''.join(p.read_text(encoding='utf-8')
                      for p in memory_home.glob('*.md'))
    assert 'sk-abc' not in written
    assert '[redacted:openai_key]' in written


def test_render_memory_redacts_body():
    import memory
    out = memory._render_memory('n', 'user', 'd',
                                'password: hunter2000 here')
    assert 'hunter2000' not in out
    assert '[redacted:kv]' in out


def test_render_memory_redacts_description():
    import memory
    out = memory._render_memory('n', 'user', 'token=abcdef123456', 'body')
    assert 'abcdef123456' not in out
