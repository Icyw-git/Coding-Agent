"""逐会话的记忆开关（N3）。

进程级 HARNESS_LOAD_MEMORIES / HARNESS_MEMORY_RECALL 没有「这次聊天别记我」。
Codex 的 chat-level /memories 明确说了 **不改全局设置**——这里照做。
"""
from __future__ import annotations

import pytest

import memory
import memory_scope


@pytest.fixture(autouse=True)
def clean_scope(monkeypatch):
    """每个用例从干净状态开始：会话级状态是 ContextVar，别跨用例泄漏。"""
    memory_scope.reset()
    monkeypatch.delenv('HARNESS_LOAD_MEMORIES', raising=False)
    monkeypatch.delenv('HARNESS_MEMORY_RECALL', raising=False)
    monkeypatch.delenv('HARNESS_MEMORY_CONTRIBUTE', raising=False)
    yield
    memory_scope.reset()


# ---------------------------------------------------------------- 模式解析

def test_default_is_on():
    assert memory_scope.mode() == 'on'
    assert memory_scope.use_enabled()
    assert memory_scope.contribute_enabled()


@pytest.mark.parametrize('mode,use,contribute', [
    ('on', True, True),
    ('off', False, False),
    ('readonly', True, False),
    ('writeonly', False, True),
])
def test_set_mode_pairs(mode, use, contribute):
    assert memory_scope.set_mode(mode) == (use, contribute)
    assert memory_scope.use_enabled() is use
    assert memory_scope.contribute_enabled() is contribute


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError, match='mode must be one of'):
        memory_scope.set_mode('sometimes')


def test_reset_returns_to_global_default():
    memory_scope.set_mode('off')
    memory_scope.reset()
    assert memory_scope.mode() == 'on'


# ---------------------------------------------------------------- 全局默认照样生效

def test_global_env_still_drives_default(monkeypatch):
    monkeypatch.setenv('HARNESS_MEMORY_RECALL', 'off')
    assert memory_scope.mode() == 'off'


def test_session_choice_overrides_global(monkeypatch):
    """会话级选择优先于全局：否��� /memories 就没意义。"""
    monkeypatch.setenv('HARNESS_MEMORY_RECALL', 'off')
    memory_scope.set_mode('on')
    assert memory_scope.use_enabled()


def test_global_contribute_off(monkeypatch):
    monkeypatch.setenv('HARNESS_MEMORY_CONTRIBUTE', '0')
    assert memory_scope.mode() == 'readonly'


# ---------------------------------------------------------------- 接入读取路径

def test_load_memories_skipped_when_use_disabled(monkeypatch):
    """off/writeonly 时不注入已有记忆。"""
    memory_scope.set_mode('off')
    called = []
    monkeypatch.setattr(memory, 'local_relevant_memories',
                        lambda messages: called.append(1) or [])
    assert memory.load_memories([{'role': 'user', 'content': 'hi'}]) == ''
    assert called == []      # 连筛选都不该跑


def test_load_memories_runs_when_use_enabled(monkeypatch):
    memory_scope.set_mode('on')
    called = []
    monkeypatch.setattr(memory, 'local_relevant_memories',
                        lambda messages: called.append(1) or [])
    memory.load_memories([{'role': 'user', 'content': 'hi'}])
    assert called == [1]


def test_writeonly_still_reads_nothing(monkeypatch):
    """writeonly 的意义就是「不受旧记忆影响」。"""
    memory_scope.set_mode('writeonly')
    assert memory.load_memories([{'role': 'user', 'content': 'hi'}]) == ''


# ---------------------------------------------------------------- 接入写入路径

def test_extract_skipped_when_contribute_disabled(monkeypatch):
    """readonly/off 时不产生新记忆：先跳过，后面要调一次模型，能省就省。"""
    memory_scope.set_mode('readonly')
    called = []
    monkeypatch.setattr('llm_route.completion',
                        lambda *a, **k: called.append(1))
    memory.extract_memories([{'role': 'user', 'content': 'remember this'}])
    assert called == []


def test_extract_runs_when_contribute_enabled(monkeypatch):
    memory_scope.set_mode('on')
    called = []

    class _Resp:
        class _Choice:
            class _Msg:
                content = '[]'
            message = _Msg()
        choices = [_Choice()]

    monkeypatch.setattr('llm_route.completion', lambda *a, **k: called.append(1) or _Resp())
    memory.extract_memories([{'role': 'user', 'content': 'remember this'}])
    assert called == [1]


# ---------------------------------------------------------------- 恢复

class _FakeStore:
    def __init__(self, events):
        self._events = events

    def read_events(self):
        return self._events


def test_restore_from_session_uses_last_event():
    memory_scope.restore_from_session(_FakeStore([
        {'type': 'memory_scope', 'mode': 'off'},
        {'type': 'message'},
        {'type': 'memory_scope', 'mode': 'readonly'},
    ]))
    assert memory_scope.mode() == 'readonly'


def test_restore_without_event_keeps_default():
    memory_scope.restore_from_session(_FakeStore([{'type': 'message'}]))
    assert memory_scope.mode() == 'on'


def test_restore_ignores_unknown_mode():
    """旧会话/手改的事件不该把状态搞坏。"""
    memory_scope.restore_from_session(_FakeStore([
        {'type': 'memory_scope', 'mode': 'garbage'},
    ]))
    assert memory_scope.mode() == 'on'


def test_restore_survives_broken_store():
    class _Broken:
        def read_events(self):
            raise OSError('boom')

    memory_scope.restore_from_session(_Broken())
    assert memory_scope.mode() == 'on'


def test_restore_none_store():
    memory_scope.restore_from_session(None)
    assert memory_scope.mode() == 'on'


# ---------------------------------------------------------------- 展示

def test_describe_is_human_readable():
    for mode in memory_scope.MODES:
        memory_scope.set_mode(mode)
        text = memory_scope.describe()
        assert text and text != mode
