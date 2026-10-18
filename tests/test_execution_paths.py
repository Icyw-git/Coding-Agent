"""执行路径接线测试：hook / 工具边界 / teammate / 后台 worker / HTTP 任务共用一层策略。

策略矩阵本身在 tests/test_exec_policy.py（纯函数，无需运行时）。这里只测「接线」：

    1. 审批只问一次 —— hook 的决议以凭据形式传到工具边界，边界不再二次询问
    2. 凭据是一次性的 —— 同一命令第二次调用要重新审批
    3. 无头路径（teammate、后台 worker、任务 runner）不抢 stdin，进远程队列，超时拒绝
    4. 会话白名单按会话分区，不跨会话泄漏
    5. 绕过 PreToolUse hook 的入口在边界上自己判一次（黑名单、工作区越界、档位）
    6. 第三方（MCP）工具登记后默认询问，never 档失败而不是静默执行
"""
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import agent_teams
import background_task
import harness
import hooks
import tools
from cli_commands import command_error
from exec_policy import load_policy, session_allow_key
from hooks import GATE

SPAWN = 'tools.subprocess.Popen'


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """假 harness 配置 + 远程审批后端 + 极短超时（未决议的用例不会挂住测试）。"""
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    class Config:
        DENY_LIST = ['sudo']
        DESTRUCTIVE = ['rm ', 'del ']
        session_id = ''          # 用例可改，模拟切会话

        @staticmethod
        def get_workdir():
            return workspace

        @staticmethod
        def active_checkpoint_manager():
            # 与真实 harness 同构：无活动会话 → None → 白名单退回进程级键
            return SimpleNamespace(session_id=Config.session_id) \
                if Config.session_id else None

    monkeypatch.setattr(hooks, '_h', lambda: Config)
    monkeypatch.setattr(GATE, 'backend', 'remote')
    monkeypatch.setattr(GATE, 'timeout', 0.2)
    GATE.clear()
    return Config


@pytest.fixture()
def asks(monkeypatch):
    """把交互式审批（GATE.decide）换成「一律批准」并记录问了什么，不读 stdin。"""
    recorded = []

    def decide(tool, args, reason):
        recorded.append(args.get('command', args))
        return True, 'approved'

    monkeypatch.setattr(GATE, 'decide', decide)
    return recorded


def _await_pending(timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if GATE.pending():
            return GATE.pending()[0]
        time.sleep(0.01)
    pytest.fail('无头调用应把审批登记到远程队列')


def _run_in_thread(fn):
    out = {}

    def worker():
        out['value'] = fn()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread, out


# ============================================================
# 1 + 2：凭据把 hook 的决议送到工具边界，且只消费一次
# ============================================================

def test_hook_approval_reaches_the_tool_boundary_once(env, asks):
    assert hooks.permission_hook('bash', {'command': 'rm build'}) is None
    assert asks == ['rm build']

    # 边界（run_bash 内的那次判定）取走凭据，没有二次询问
    assert hooks.enforce_headless('bash', {'command': 'rm build'}) is None
    assert asks == ['rm build'] and not GATE.pending()

    # 凭据一次性：同一条命令再调一次要重新审批（无头队列，超时拒绝）
    assert hooks.enforce_headless('bash', {'command': 'rm build'}) \
        == 'Permission denied by user'
    assert GATE.history()[0].status == 'timeout'


def test_denied_approval_leaves_no_grant(env, monkeypatch):
    monkeypatch.setattr(GATE, 'decide', lambda *a: (False, 'denied'))
    assert hooks.permission_hook('bash', {'command': 'del f'}) \
        == 'Permission denied by user'
    assert hooks.current_grants() == frozenset()


def test_session_allowlist_also_carries_the_grant(env):
    """白名单命中同样算「本次调用已批准」，否则边界会把它当成无凭据的新调用。"""
    env.session_id = 'A'
    GATE._session_allows.add(session_allow_key('bash', 'rm', 'A'))
    assert hooks.permission_hook('bash', {'command': 'rm x'}) is None
    assert hooks.enforce_headless('bash', {'command': 'rm x'}) is None
    assert not GATE.pending()


def test_approval_request_compacts_large_arguments(env):
    """write 的 content 不该整份进入控制台、API 响应和审批历史。"""
    req = GATE.request('write', {'path': 'notes.txt', 'content': 'x' * 5000},
                       'write file')
    assert req.args['path'] == 'notes.txt'
    assert len(req.args['content']) < 300
    assert '5000' in req.args['content']


# ============================================================
# 3：无头路径排队等远程决议，超时拒绝
# ============================================================

def test_headless_ask_waits_for_a_remote_decision(env):
    thread, out = _run_in_thread(
        lambda: hooks.enforce_headless('bash', {'command': 'del f'}))
    req = _await_pending()
    assert req.tool == 'bash' and 'del' in req.reason
    GATE.resolve(req.req_id, True, by='test')
    thread.join(timeout=2)
    assert out['value'] is None


def test_headless_ask_without_a_decision_times_out_to_deny(env, capsys):
    assert hooks.enforce_headless('bash', {'command': 'del f'}) \
        == 'Permission denied by user'
    assert GATE.history()[0].status == 'timeout'
    printed = capsys.readouterr().out
    assert '等待审批' in printed
    assert 'perm_' not in printed   # req_id 留在审批历史里，不刷进对话


def test_teammate_handler_is_gated_without_a_hook(env, monkeypatch):
    """teammate 跳过 PreToolUse：判定发生在 run_bash 边界，仍进同一个审批队列。"""
    spawn = Mock(side_effect=AssertionError('待审批命令不得启动进程'))
    monkeypatch.setattr(SPAWN, spawn)
    _tools, handlers = agent_teams._build_team_tools('worker')

    thread, out = _run_in_thread(lambda: handlers['bash']('del f'))
    GATE.resolve(_await_pending().req_id, False, by='test')
    thread.join(timeout=2)
    assert out['value'] == 'Permission denied by user'
    spawn.assert_not_called()


# ============================================================
# 4：会话白名单分区
# ============================================================

def test_session_allowlist_does_not_leak_across_sessions(env):
    env.session_id = 'A'
    GATE.resolve(GATE.request('bash', {'command': 'rm x'}, 'destructive').req_id,
                 True, by='test', scope='session')
    assert GATE.session_allows('bash', {'command': 'rm y'})
    env.session_id = 'B'
    assert not GATE.session_allows('bash', {'command': 'rm y'})


def test_allowlist_without_session_context_keeps_the_legacy_key(env):
    """取不到会话时退回进程级键（CLI 单会话与既有测试的前提）。"""
    assert env.session_id == ''
    GATE.resolve(GATE.request('bash', {'command': 'rm x'}, 'destructive').req_id,
                 True, by='test', scope='session')
    assert 'bash:rm' in GATE._session_allows
    assert GATE.session_allows('bash', {'command': 'rm y'})


# ============================================================
# 5：绕过 hook 的入口自己判
# ============================================================

def test_run_bash_denies_without_spawning(env, monkeypatch):
    spawn = Mock(side_effect=AssertionError('被拒命令不得启动进程'))
    monkeypatch.setattr(SPAWN, spawn)
    assert tools.run_bash('sudo whoami') \
        == 'Error: Dangerous command sudo, please do not execute'
    spawn.assert_not_called()


def test_run_bash_runs_an_already_approved_command(env, asks, monkeypatch):
    proc = Mock()
    proc.wait.return_value = 0
    spawned = []
    monkeypatch.setattr(SPAWN, lambda *a, **k: spawned.append(a) or proc)
    assert hooks.permission_hook('bash', {'command': 'rm build'}) is None
    assert tools.run_bash('rm build') == 'No output'
    assert len(spawned) == 1
    assert not GATE.pending()


def test_run_write_refuses_outside_the_workspace_without_asking(env, tmp_path):
    """旧实现先问一次「越界」再让 safe_path 报错——一个不起作用的按钮，现在硬拒。"""
    assert tools.run_write('../evil.txt', 'boom') \
        == 'Error: access outside workspace (../evil.txt)'
    assert not (tmp_path / 'evil.txt').exists()
    assert not GATE.history(), '硬拒不产生审批请求'


def test_danger_full_access_actually_opens_the_boundary(env, monkeypatch, tmp_path):
    """档位必须有真实区别：danger-full-access 下策略层与 safe_path 一起放开越界读写。"""
    monkeypatch.setenv('HARNESS_SANDBOX_MODE', 'danger-full-access')
    monkeypatch.setattr(tools, '_h', lambda: env)   # 路径检查也按假工作区解析
    assert 'Error' not in tools.run_write('../outside.txt', 'ok')
    assert (tmp_path / 'outside.txt').read_text(encoding='utf-8') == 'ok'
    assert not GATE.pending()


def test_read_only_tier_blocks_writes_at_the_boundary(env, monkeypatch, tmp_path):
    """read-only 档：文件写改从「免审」变成要审批，且拦在工具边界上。"""
    monkeypatch.setenv('HARNESS_SANDBOX_MODE', 'read-only')
    thread, out = _run_in_thread(lambda: tools.run_write('a.txt', 'x'))
    GATE.resolve(_await_pending().req_id, False, by='test')
    thread.join(timeout=2)
    assert out['value'] == 'Permission denied by user'
    assert not (tmp_path / 'ws' / 'a.txt').exists()


# ============================================================
# 后台 worker：审批凭据要显式带走（线程上下文不继承）
# ============================================================

@pytest.fixture()
def bg_clean():
    known = set(background_task.background_tasks)
    yield
    for bg_id in set(background_task.background_tasks) - known:
        background_task.background_tasks.pop(bg_id, None)
        background_task.background_results.pop(bg_id, None)


def _dispatch(command):
    """走真实的 start_background_task：worker 线程里执行注册的 bash 处理器。"""
    tool_call = SimpleNamespace(
        id='tc_1',
        function=SimpleNamespace(name='bash',
                                 arguments=json.dumps({'command': command})))

    def handler(command, cwd=None):   # 处理器边界 = run_bash 的同款判定
        return hooks.enforce_headless('bash', {'command': command}) or 'ran'

    bg_id = background_task.start_background_task(tool_call, {'bash': handler})
    for _ in range(400):
        if background_task.background_tasks[bg_id]['status'] != 'running':
            break
        time.sleep(0.01)
    return bg_id


def test_background_worker_carries_the_approval(env, asks, bg_clean):
    assert hooks.permission_hook('bash', {'command': 'rm build'}) is None
    assert hooks.current_grants()
    bg_id = _dispatch('rm build')
    assert hooks.current_grants() == frozenset()
    assert background_task.background_results[bg_id] == 'ran'
    assert not GATE.pending(), 'hook 批过的命令不该在 worker 里再问一遍'


def test_background_worker_without_approval_queues(env, monkeypatch, bg_clean):
    """没批过的命令在 worker 里排队等远程决议，而不是静默拒绝或抢占 stdin。"""
    monkeypatch.setattr(GATE, 'timeout', 5.0)   # 留出人工决议的时间窗口
    approved = []

    def resolver():
        req = _await_pending()
        GATE.resolve(req.req_id, True, by='test')
        approved.append(req.req_id)

    waiter = threading.Thread(target=resolver, daemon=True)
    waiter.start()
    bg_id = _dispatch('del f')
    waiter.join(timeout=3)
    assert approved, 'worker 的审批请求应可被远程决议'
    assert background_task.background_results[bg_id] == 'ran'


# ============================================================
# 6：第三方工具（MCP）—— 策略层不认识它们的副作用，默认要审批
# ============================================================

def test_unknown_tool_without_registration_is_still_allowed(env):
    """内置的任务板 / cron / team 工具就是「策略层不认识但自家实现」，
    不登记进第三方名单就不该被拦——否则每次注册顺序变化都会误伤。"""
    assert hooks.evaluate_call('mcp_echo', {'text': 'x'}).action == 'allow'


def test_registered_external_tool_asks_at_the_gate(env, asks):
    hooks.register_external_tools(['mcp_echo'])
    decision = hooks.evaluate_call('mcp_echo', {'text': 'x'})
    assert decision.action == 'ask' and decision.code == 'external_tool'

    assert hooks.permission_hook('mcp_echo', {'text': 'x'}) is None
    assert asks == [{'text': 'x'}]     # 问的就是这次调用的参数


def test_external_tool_allowlist_is_keyed_by_the_tool_name(env, asks):
    """免审的出路仍是既有的「本次会话允许同类」：第三方工具的同类键就是工具名。"""
    hooks.register_external_tools(['mcp_echo'])
    GATE._session_allows.add(session_allow_key('mcp_echo', 'mcp_echo', ''))
    assert hooks.permission_hook('mcp_echo', {'text': 'y'}) is None
    assert asks == [], '白名单命中不该再问'
    assert hooks.current_grants(), '白名单命中同样把凭据带到工具边界'


def test_external_tool_ask_under_never_fails_closed_without_asking(env, monkeypatch):
    """never 档不问人，但第三方工具也不该静默执行：判定直接失败。"""
    monkeypatch.setenv('HARNESS_APPROVAL_POLICY', 'never')
    hooks.register_external_tools(['mcp_echo'])
    denied = hooks.enforce('mcp_echo', {'text': 'z'}, interactive=False)
    assert denied.startswith('Error:') and 'third-party tool' in denied
    assert not GATE.pending(), 'fail-closed 不该产生审批请求'


# ============================================================
# 档位开关：/sandbox 与命令校验
# ============================================================

def test_sandbox_command_lists_tiers_and_the_limitation(capsys):
    harness.handle_command('/sandbox', [], None)
    out = capsys.readouterr().out
    for tier in ('read-only', 'workspace-write', 'danger-full-access',
                 'untrusted', 'on-request', 'never'):
        assert tier in out
    assert '*workspace-write' in out          # 默认档被标出
    assert '策略级判定' in out                 # 边界说明必须出现在菜单里
    assert 'HARNESS_OS_SANDBOX' in out        # OS 沙箱是独立开关，菜单里要看得到
    assert 'off' in out                       # conftest 把测试环境固定在 off 档


@pytest.mark.parametrize(('line', 'sandbox', 'approval'), [
    ('/sandbox read-only', 'read-only', 'on-request'),
    ('/sandbox danger-full-access', 'danger-full-access', 'on-request'),
    ('/sandbox approvals never', 'workspace-write', 'never'),
    ('/sandbox APPROVALS untrusted', 'workspace-write', 'untrusted'),
])
def test_sandbox_command_switches_tiers(line, sandbox, approval, capsys):
    harness.handle_command(line, [], None)
    policy = load_policy()          # 档位写进环境：下一条工具调用即生效
    assert (policy.sandbox_mode, policy.approval_policy) == (sandbox, approval)
    assert '已切换' in capsys.readouterr().out


def test_unknown_tier_is_rejected_not_silently_downgraded(capsys):
    before = load_policy()
    harness.handle_command('/sandbox read-onlyy', [], None)
    assert load_policy() == before, '打错档位名不能悄悄用默认档'
    assert 'workspace-write' in capsys.readouterr().out   # 用法里列出可选值


@pytest.mark.parametrize(('argument', 'invalid'), [
    ('read-only', False), ('workspace-write', False), ('danger-full-access', False),
    ('approvals never', False), ('approvals untrusted', False),
    ('approvals', True), ('approvals nope', True), ('nope', True),
    ('read-only extra', True), ('read-only approvals never', True),
])
def test_sandbox_argument_validation(argument, invalid):
    assert bool(command_error('/sandbox', argument)) is invalid
