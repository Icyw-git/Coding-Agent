"""两层记忆（A5）：session 层与 global 层，以及 durability 判定。

PAHF 的做法是 session notes 合并后清除，只有 durable 的进 global memory。
它给的例子：一趟旅行里说「这次不想托运行李」被**故意丢弃**，因为
*explicitly scoped to that specific trip*。这类一次性上下文进了长期记忆，
之后每次订票都会被干扰。

规则刻意保守：只有明确说「这次/本次」才降级，其余默认提升——漏判只是多一条冗余
（合并机制会处理），误判是把有用信息丢掉。
"""
from __future__ import annotations

import pytest

import harness
import memory
import memory_durability as dur

# ---------------------------------------------------------------- 判定

@pytest.mark.parametrize('text', [
    '这次旅行不想托运行李',
    '本次任务用临时目录',
    '这一回先这样',
    '暂时先不部署',
    'for now keep it simple',
    'just this once',
])
def test_ephemeral_wording_is_session_scope(text):
    scope, reason = dur.classify({'description': text})
    assert scope == 'session', text
    assert reason


@pytest.mark.parametrize('text', [
    '以后都用 pnpm',
    '总是先跑测试',
    'every time use the staging key',
    'from now on write docs in Chinese',
])
def test_durable_wording_is_global(text):
    scope, _reason = dur.classify({'description': text})
    assert scope == 'global', text


def test_conditional_preference_is_global():
    """条件会重复出现，所以条件性偏好是长期的。"""
    scope, reason = dur.classify({'description': 'when sleepy: prefers tea'})
    assert scope == 'global'
    assert 'conditional' in reason


def test_plain_fact_defaults_to_global():
    scope, reason = dur.classify({'description': 'deploys use make ship-prod'})
    assert scope == 'global'
    assert 'default' in reason


def test_ephemeral_beats_durable():
    """两种措辞同时出现时按短暂处理：多留一条冗余比误当长期偏好安全。"""
    scope, _reason = dur.classify({'description': '这次先这样，以后再说'})
    assert scope == 'session'


def test_apply_does_not_override_explicit_scope():
    """显式指定优先——人比规则准。"""
    entry = dur.apply({'description': '这次先这样', 'scope': 'global'})
    assert entry['scope'] == 'global'


def test_apply_fills_missing_scope():
    entry = dur.apply({'description': '这次先这样'})
    assert entry['scope'] == 'session'
    assert 'scope_reason' in entry


def test_session_only_filter():
    entries = [{'scope': 'session'}, {'scope': 'global'}, {}]
    assert len(dur.session_only(entries)) == 1


# ---------------------------------------------------------------- 接入写入

@pytest.fixture
def home(tmp_path, monkeypatch):
    h = harness
    d = tmp_path / 'mem'
    d.mkdir()
    monkeypatch.setattr(h, 'MEMORY_DIR', d, raising=False)
    monkeypatch.setattr(h, 'MEMORY_INDEX', d / 'MEMORY.md', raising=False)
    memory._cache_clear()
    yield d
    memory._cache_clear()


def test_write_assigns_session_scope_for_one_off(home):
    memory.write_memory_file('trip', 'context', '这次旅行不想托运行李', 'body')
    entry = next(item for item in memory.list_memory_files()
                 if item['name'] == 'trip')
    assert entry['scope'] == 'session'


def test_write_assigns_global_scope_for_durable(home):
    memory.write_memory_file('pref', 'preference', '以后都用 pnpm', 'body')
    entry = next(item for item in memory.list_memory_files()
                 if item['name'] == 'pref')
    assert entry['scope'] == 'global'


def test_explicit_scope_wins_on_write(home):
    memory.write_memory_file('trip', 'context', '这次旅行不想托运行李', 'body',
                             scope='global')
    entry = next(item for item in memory.list_memory_files()
                 if item['name'] == 'trip')
    assert entry['scope'] == 'global'


# ---------------------------------------------------------------- session 层回收

def test_session_scope_is_archived_after_a_day(home):
    """一次性上下文隔天就不该再参与召回。"""
    memory.write_memory_file('trip', 'context', '这次旅行不想托运行李', 'body')
    moved = memory.archive_session_scope(days=0)
    assert [item['name'] for item in moved] == ['trip']
    assert (home / '.archive' / 'trip.md').is_file()
    assert not [item for item in memory.list_memory_files()
                if item['name'] == 'trip']


def test_global_scope_is_not_archived(home):
    memory.write_memory_file('pref', 'preference', '以后都用 pnpm', 'body')
    assert memory.archive_session_scope(days=0) == []
    assert [item['name'] for item in memory.list_memory_files()] == ['pref']


def test_fresh_session_entry_survives_the_grace_window(home):
    """当天写入的 session 条目还要能用（同一会话后续轮次依赖它）。"""
    memory.write_memory_file('trip', 'context', '这次旅行不想托运行李', 'body')
    assert memory.archive_session_scope(days=1) == []


def test_session_cleanup_does_not_delete(home):
    memory.write_memory_file('trip', 'context', '这次暂时不部署', 'body')
    memory.archive_session_scope(days=0)
    archived = home / '.archive' / 'trip.md'
    assert archived.is_file()
    assert '这次暂时不部署' in archived.read_text(encoding='utf-8')
