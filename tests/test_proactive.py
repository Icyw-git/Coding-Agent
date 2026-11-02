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
    assert '只读' in text
    assert 'NONE' in text
    text.encode('gbk')


def test_prompt_limits_count():
    assert '最多给 2 条' in proactive.prompt({'workspace': 'w'}, limit=2)


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


# ---------- B18 接线：可注入的只读建议回合 ----------

def test_read_only_schemas_match_the_whitelist():
    """喂给模型的工具面必须就是白名单本身——多一个都算越权。

    提示词挡不住工具，白名单才能。所以这里断言"schema 集合 == 白名单"，
    而不是"包含"：哪天有人往白名单里加写工具，这里会红。
    """
    import proactive

    names = {schema['function']['name'] for schema in proactive.read_only_schemas()}
    assert names == set(proactive.READ_ONLY_TOOLS)


def test_run_turn_feeds_state_and_parses_suggestions(tmp_path, monkeypatch):
    import proactive

    asked = {}

    def ask(text):
        asked['text'] = text
        return '1. 把未完成的迁移脚本跑通\n2. 清掉 .test-tmp 里的旧日志\n'

    run = proactive.run_turn(workdir=tmp_path, sessions_dir=tmp_path / 'sessions',
                             cache_dir=tmp_path / '.cache', ask=ask, limit=2)

    assert run.suggestions == ['把未完成的迁移脚本跑通', '清掉 .test-tmp 里的旧日志']
    assert str(tmp_path) in asked['text'], '提示里要带上工作区，否则模型无从谈起'
    assert 'NONE' in asked['text'], '没东西可说时要允许它什么都不建议'


def test_run_turn_records_the_run_for_the_auto_interval(tmp_path):
    import proactive

    cache = tmp_path / '.cache'
    proactive.run_turn(workdir=tmp_path, sessions_dir=tmp_path / 'sessions',
                       cache_dir=cache, ask=lambda text: '一条建议', limit=1)
    state = proactive._read_state(cache)
    assert state.get('last_run'), '手动跑过也要记，自动档的间隔靠它'
    assert state.get('last_count') == 1


def test_run_turn_reports_an_error_without_raising(tmp_path):
    """入口的回合可能抛（网络/取消）：命令层要能给出人话，而不是栈。"""
    import proactive

    def boom(text):
        raise RuntimeError('network down')

    run = proactive.run_turn(workdir=tmp_path, sessions_dir=tmp_path / 'sessions',
                             cache_dir=tmp_path / '.cache', ask=boom)
    assert run.suggestions == []
    assert 'network down' in run.error


def test_run_turn_treats_none_as_no_suggestions(tmp_path):
    import proactive

    run = proactive.run_turn(workdir=tmp_path, sessions_dir=tmp_path / 'sessions',
                             cache_dir=tmp_path / '.cache',
                             ask=lambda text: 'NONE', limit=3)
    assert run.suggestions == []
    assert run.error == ''


# ---------- /suggest 的接线（不启动 TUI，直接测混入的两段逻辑） ----------

def test_suggest_builds_a_read_only_turn(tmp_path, monkeypatch):
    """`/suggest` 排的是一个**只读**回合：工具面子集 + proactive 标记。

    只看"调了 start_suggestions"不够——要断言它交给回合的东西真的被裁过。
    """

    import proactive
    from ui.proactive_actions import ProactiveActionsMixin

    captured = {}

    class Fake(ProactiveActionsMixin):
        _busy = False

        def _notice(self, text, **kwargs):
            captured['notice'] = text

        def _begin_turn(self, request, *, echo_text=True):
            captured['request'] = request
            captured['echo'] = echo_text
            return True

    import harness
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')

    Fake().start_suggestions()

    request = captured['request']
    assert request.proactive is True
    assert {schema['function']['name'] for schema in request.tools} == set(
        proactive.READ_ONLY_TOOLS)
    assert captured['echo'] is False, '那一大段提示不该出现在聊天区'
    assert '只读' in captured['notice']


def test_suggest_refuses_while_a_turn_is_running():
    from ui.proactive_actions import ProactiveActionsMixin

    seen = {}

    class Fake(ProactiveActionsMixin):
        _busy = True

        def _notice(self, text, **kwargs):
            seen['notice'] = text

        def _begin_turn(self, request, *, echo_text=True):     # pragma: no cover
            raise AssertionError('忙的时候不能开新回合')

    Fake().start_suggestions()
    assert '等这一回合结束' in seen['notice']


def test_record_proactive_run_writes_the_interval_state(tmp_path, monkeypatch):
    import harness
    from ui.proactive_actions import ProactiveActionsMixin

    monkeypatch.setattr(harness, 'CACHE_DIR', tmp_path / 'cache')

    ProactiveActionsMixin().record_proactive_run('1. 甲\n2. 乙')

    import proactive
    state = proactive._read_state(tmp_path / 'cache')
    assert state.get('last_count') == 2


def test_slash_suggest_runs_one_read_only_turn(monkeypatch, tmp_path):
    """端到端：敲 `/suggest` → 一个只读回合，且钩子在那一轮里按白名单拒绝。

    这条测的是**接线**：命令分发 → TurnRequest → worker → agent_loop 的 tools
    与 hooks.proactive_scope。任一处没接上，这里都会红。
    """
    pytest.importorskip('textual')
    import asyncio

    import harness
    import hooks
    from ui.harness_app import HarnessApp

    seen = {}

    def fake_loop(messages, **kwargs):
        seen['tools'] = kwargs.get('tools')
        seen['proactive'] = hooks.proactive_turn()
        # 只读白名单在钩子里真的生效
        seen['write_blocked'] = hooks.permission_hook('write', {'path': 'x', 'content': 'y'})
        seen['read_allowed'] = hooks.permission_hook('read', {'path': 'x'})
        return '1. 一条建议'

    monkeypatch.setattr(harness, 'agent_loop', fake_loop)
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')
    monkeypatch.setattr(harness, 'CACHE_DIR', tmp_path / 'cache')

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(90, 26)) as pilot:
            app.submit_text('/suggest')
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if 'tools' in seen:
                    break
            app._exiting = True
            app._pending.put(None)

    asyncio.run(scenario())

    import proactive
    assert {schema['function']['name'] for schema in seen['tools']} == set(
        proactive.READ_ONLY_TOOLS)
    assert seen['proactive'] is True, '只读回合没有打开 proactive_scope'
    assert seen['write_blocked'], '白名单之外的 tool 必须被拒'
    assert seen['read_allowed'] is None, '读工具应当放行'


# ---------- P1-1：observe 档（评估但只记录） ----------

def test_mode_defaults_to_off_and_reads_three_values(monkeypatch):
    import proactive

    monkeypatch.delenv(proactive.MODE_ENV, raising=False)
    monkeypatch.delenv('HARNESS_PROACTIVE', raising=False)
    assert proactive.mode() == proactive.MODE_OFF
    assert proactive.enabled() is False

    for value in ('off', 'observe', 'auto', 'AUTO', ' observe '):
        monkeypatch.setenv(proactive.MODE_ENV, value)
        assert proactive.mode() == value.strip().casefold()
    assert proactive.enabled() is True


def test_legacy_switch_still_means_auto(monkeypatch):
    """老开关 `HARNESS_PROACTIVE=1` 的语义保持不变（等于 auto）。

    已有部署只设过这个变量；加模式时把它的意思悄悄改掉，等于把别人的开关换了功能。
    """
    import proactive

    monkeypatch.delenv(proactive.MODE_ENV, raising=False)
    monkeypatch.setenv('HARNESS_PROACTIVE', '1')
    assert proactive.mode() == proactive.MODE_AUTO


def test_unknown_mode_value_falls_back_to_off(monkeypatch):
    """写错了就退到最安全的那一档，并且不静默当成 auto。"""
    import proactive

    monkeypatch.delenv('HARNESS_PROACTIVE', raising=False)
    monkeypatch.setenv(proactive.MODE_ENV, 'always')
    assert proactive.mode() == proactive.MODE_OFF


def test_observation_ledger_keeps_what_it_would_have_said(tmp_path):
    """observe 档的价值在于**事后能看见它本来会说什么**。"""
    import json

    import proactive

    cache = tmp_path / 'cache'
    proactive.record_observation(cache, ['把迁移脚本跑通'],
                                 workspace=str(tmp_path), limit=3)

    lines = proactive.observation_path(cache).read_text(
        encoding='utf-8').splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row['suggestions'] == ['把迁移脚本跑通']
    assert row['workspace'] == str(tmp_path)
    assert row['at']


def test_observation_ledger_is_bounded(tmp_path):
    """只记录也不能无限长：超过上限就丢最老的。"""
    import proactive

    cache = tmp_path / 'cache'
    for index in range(proactive.OBSERVATION_LIMIT + 5):
        proactive.record_observation(cache, [f'第 {index} 条'], workspace='w')

    lines = proactive.observation_path(cache).read_text(
        encoding='utf-8').splitlines()
    assert len(lines) == proactive.OBSERVATION_LIMIT
    assert '第 0 条' not in lines[0]      # 最老的先走


def test_silent_turn_request_is_opt_in():
    """静默回合必须显式声明；默认值是"会显示"。"""
    from ui.widgets import TurnRequest

    assert TurnRequest(text='x').silent is False
    assert TurnRequest(text='x', silent=True).silent is True


def test_auto_suggest_does_nothing_when_off(tmp_path, monkeypatch):
    import harness
    import proactive
    from ui.proactive_actions import ProactiveActionsMixin

    monkeypatch.delenv(proactive.MODE_ENV, raising=False)
    monkeypatch.delenv('HARNESS_PROACTIVE', raising=False)
    monkeypatch.setattr(harness, 'CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')

    class Fake(ProactiveActionsMixin):
        _busy = False

        def _notice(self, text, **kwargs):
            raise AssertionError('off 档不该有任何提示')

        def _begin_turn(self, request, *, echo_text=True):
            raise AssertionError('off 档不该开回合')

    Fake().maybe_auto_suggest()


def test_auto_suggest_observes_silently(tmp_path, monkeypatch):
    """observe 档：跑一个静默回合，只记台账；不把建议甩到聊天区。"""
    import harness
    import proactive
    from ui.proactive_actions import ProactiveActionsMixin

    monkeypatch.setenv(proactive.MODE_ENV, proactive.MODE_OBSERVE)
    monkeypatch.setattr(harness, 'CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')
    seen = {}

    class Fake(ProactiveActionsMixin):
        _busy = False

        def _notice(self, text, **kwargs):
            seen['notice'] = text

        def _begin_turn(self, request, *, echo_text=True):
            seen['request'] = request
            return True

    Fake().maybe_auto_suggest()

    request = seen['request']
    assert request.proactive is True and request.silent is True
    assert {s['function']['name'] for s in request.tools} == set(
        proactive.READ_ONLY_TOOLS)
    assert '记录' in seen['notice']


def test_auto_suggest_shows_in_auto_mode(tmp_path, monkeypatch):
    import harness
    import proactive
    from ui.proactive_actions import ProactiveActionsMixin

    monkeypatch.setenv(proactive.MODE_ENV, proactive.MODE_AUTO)
    monkeypatch.setattr(harness, 'CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')
    called = {}

    class Fake(ProactiveActionsMixin):
        _busy = False

        def start_suggestions(self):
            called['yes'] = True

        def _begin_turn(self, request, *, echo_text=True):     # pragma: no cover
            raise AssertionError('auto 档应当走可见的 /suggest 路径')

    Fake().maybe_auto_suggest()
    assert called.get('yes') is True


def test_observe_ledger_is_readable_from_the_command(tmp_path, monkeypatch):
    """observe 档记录了东西，就必须有地方能看。

    否则"只记录不打扰"等于"记了没人知道"——用户看不到它本来会说什么，
    observe 和 off 就没区别了。`/suggest --last` 把最近几次记录打出来。
    """
    import harness
    import proactive
    from ui.proactive_actions import ProactiveActionsMixin

    monkeypatch.setattr(harness, 'CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')
    proactive.record_observation(tmp_path / 'cache', ['先跑测试', '再补文档'],
                                 workspace=str(tmp_path), limit=2)

    seen = {}

    class Fake(ProactiveActionsMixin):
        _busy = False

        def _notice(self, text, **kwargs):
            seen['notice'] = text

        def _begin_turn(self, request, *, echo_text=True):     # pragma: no cover
            raise AssertionError('--last 不该再跑一轮模型')

    Fake().start_suggestions('--last')
    assert 'notice' in seen, '--last 应当打出台账内容'
    assert '先跑测试' in seen['notice'] and '再补文档' in seen['notice']
    assert '2 条' in seen['notice']


def test_observe_ledger_empty_says_so(tmp_path, monkeypatch):
    import harness
    from ui.proactive_actions import ProactiveActionsMixin

    monkeypatch.setattr(harness, 'CACHE_DIR', tmp_path / 'cache')
    seen = {}

    class Fake(ProactiveActionsMixin):
        _busy = False

        def _notice(self, text, **kwargs):
            seen['notice'] = text

    Fake().start_suggestions('--last')
    assert '还没有' in seen['notice']
    assert 'HARNESS_PROACTIVE_MODE' in seen['notice']
