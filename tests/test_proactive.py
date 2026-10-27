"""只读主动建议（M2）——默认关闭。

红线有三条：工具面靠**白名单**而不是提示词、建议不是行动、默认关。
主动性最容易做成品味灾难，所以「不打扰」和「不越权」同样重要。
"""
from __future__ import annotations

import json
import time

import pytest

import proactive


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv('HARNESS_PROACTIVE', raising=False)
    monkeypatch.delenv('HARNESS_PROACTIVE_MAX', raising=False)


# ---------------------------------------------------------------- 默认关闭

def test_disabled_by_default():
    """默认必须关：先有真实使用数据再决定开不开。"""
    assert not proactive.enabled()


def test_should_run_blocked_when_disabled(tmp_path):
    ok, reason = proactive.should_run(tmp_path)
    assert not ok and reason == 'disabled'


def test_enabled_by_switch(monkeypatch):
    monkeypatch.setenv('HARNESS_PROACTIVE', '1')
    assert proactive.enabled()


# ---------------------------------------------------------------- 节奏

def test_runs_when_never_run(tmp_path, monkeypatch):
    monkeypatch.setenv('HARNESS_PROACTIVE', '1')
    ok, _reason = proactive.should_run(tmp_path)
    assert ok


def test_does_not_run_twice_in_a_day(tmp_path, monkeypatch):
    monkeypatch.setenv('HARNESS_PROACTIVE', '1')
    now = time.time()
    proactive.record_run(tmp_path, 2, now=now)
    ok, _reason = proactive.should_run(tmp_path, now=now + 3600)
    assert not ok


def test_runs_again_next_day(tmp_path, monkeypatch):
    monkeypatch.setenv('HARNESS_PROACTIVE', '1')
    now = time.time()
    proactive.record_run(tmp_path, 2, now=now)
    ok, _reason = proactive.should_run(tmp_path, now=now + 86400 + 60)
    assert ok


def test_ignored_streak_slows_it_down(tmp_path, monkeypatch):
    """连续忽略 3 次后降到每周一次——自动降频，不该让用户手动关。"""
    monkeypatch.setenv('HARNESS_PROACTIVE', '1')
    now = time.time()
    for _ in range(proactive.IGNORE_LIMIT):
        proactive.record_ignored(tmp_path)
    proactive.record_run(tmp_path, 1, now=now)

    ok, _reason = proactive.should_run(tmp_path, now=now + 2 * 86400)
    assert not ok, 'should be weekly now'
    ok, _reason = proactive.should_run(tmp_path, now=now + 7 * 86400 + 60)
    assert ok


def test_acceptance_resets_the_streak(tmp_path, monkeypatch):
    monkeypatch.setenv('HARNESS_PROACTIVE', '1')
    now = time.time()
    for _ in range(proactive.IGNORE_LIMIT):
        proactive.record_ignored(tmp_path)
    proactive.record_accepted(tmp_path)
    proactive.record_run(tmp_path, 1, now=now)
    ok, _reason = proactive.should_run(tmp_path, now=now + 86400 + 60)
    assert ok, '采纳后应回到每日节奏'


def test_broken_state_file_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv('HARNESS_PROACTIVE', '1')
    (tmp_path / proactive.STATE_NAME).write_text('{bad json', encoding='utf-8')
    ok, _reason = proactive.should_run(tmp_path)
    assert ok, '状态文件坏了应该当作从未运行'


def test_max_suggestions_is_clamped(monkeypatch):
    monkeypatch.setenv('HARNESS_PROACTIVE_MAX', '99')
    assert proactive.max_suggestions() == 5
    monkeypatch.setenv('HARNESS_PROACTIVE_MAX', '0')
    assert proactive.max_suggestions() == 1
    monkeypatch.setenv('HARNESS_PROACTIVE_MAX', 'oops')
    assert proactive.max_suggestions() == 3


# ---------------------------------------------------------------- 只读白名单

def test_whitelist_excludes_every_write_path():
    """这是 M2 的核心安全边界：不是靠提示词，是靠工具面。"""
    for tool in ('write', 'edit', 'apply_patch', 'delete_paths',
                 'bash', 'web_fetch', 'remember', 'forget',
                 'schedule_cron', 'cancel_cron', 'spawn_teammate',
                 'desktop_action', 'clipboard_write', 'screen_capture'):
        assert tool not in proactive.READ_ONLY_TOOLS, tool


def test_whitelist_includes_read_tools():
    for tool in ('read', 'grep', 'glob', 'ls', 'recall', 'list_tasks'):
        assert tool in proactive.READ_ONLY_TOOLS, tool


def test_enforce_blocks_writes_in_proactive_scope():
    import hooks
    with hooks.proactive_scope():
        assert hooks.proactive_turn()
        out = hooks.enforce('write', {'path': 'a.txt', 'content': 'x'},
                            interactive=True)
        assert out and 'read-only turn' in out
        out = hooks.enforce('bash', {'command': 'ls'}, interactive=True)
        assert out and 'read-only turn' in out


def test_scope_resets_after_exit():
    import hooks
    with hooks.proactive_scope():
        pass
    assert not hooks.proactive_turn()


def test_proactive_scope_off_by_default():
    import hooks
    assert not hooks.proactive_turn()


# ---------------------------------------------------------------- 素材收集

def test_scan_sources_reports_workspace(tmp_path):
    material = proactive.scan_sources(workdir=tmp_path, sessions_dir=tmp_path)
    assert material['workspace'] == str(tmp_path)
    assert 'todos' in material
    assert isinstance(material['recent_sessions'], list)


def test_scan_sources_survives_missing_dirs(tmp_path):
    material = proactive.scan_sources(
        workdir=tmp_path / 'missing', sessions_dir=tmp_path / 'nope')
    assert material['recent_sessions'] == []


# ---------------------------------------------------------------- 提示与解析

def test_prompt_says_read_only():
    text = proactive.prompt({'workspace': 'w', 'todos': 2,
                             'recent_sessions': []})
    assert 'READ-ONLY' in text
    assert 'NONE' in text


def test_prompt_limits_count():
    assert 'at most 2' in proactive.prompt({'workspace': 'w'}, limit=2)


def test_parse_plain_lines():
    assert proactive.parse_suggestions('fix the flaky test\nupdate docs') == [
        'fix the flaky test', 'update docs']


def test_parse_strips_bullets_and_numbers():
    text = '- first\n* second\n1. third\n2) fourth'
    assert proactive.parse_suggestions(text) == ['first', 'second', 'third']


def test_parse_none_returns_empty():
    assert proactive.parse_suggestions('NONE') == []
    assert proactive.parse_suggestions('none') == []
    assert proactive.parse_suggestions('') == []


def test_parse_respects_limit():
    text = '\n'.join(f'item {i}' for i in range(10))
    assert len(proactive.parse_suggestions(text, limit=2)) == 2


def test_parse_ignores_blank_lines():
    assert proactive.parse_suggestions('\n\na\n\n\nb\n\n') == ['a', 'b']


# ---------------------------------------------------------------- 状态落盘

def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv('HARNESS_PROACTIVE', '1')
    proactive.record_run(tmp_path, 3, now=1000.0)
    data = json.loads((tmp_path / proactive.STATE_NAME).read_text(encoding='utf-8'))
    assert data['last_run'] == 1000.0
    assert data['last_count'] == 3
