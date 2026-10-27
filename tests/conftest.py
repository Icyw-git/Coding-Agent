"""测试会话级隔离：跨测试还原全局权限门后端，避免 create_app 的状态泄漏。"""

import os

import pytest

import harness
import hooks
import sandbox
from hooks import GATE as permission_gate

_ENV_KEYS = ('HARNESS_MEMORY_RECALL', 'HARNESS_LOAD_MEMORIES',
             'HARNESS_APPROVAL_TIMEOUT', 'HARNESS_OS_SANDBOX',
             'HARNESS_WORKSPACE_ROOTS', 'HARNESS_WORKSPACE_FILE',
             # 执行档位：本机 .env 配了 read-only/never 时，不能把「默认档即重构前
             # 行为」的测试前提一起改掉。需要特定档位的用例自己 setenv。
             'HARNESS_SANDBOX_MODE', 'HARNESS_APPROVAL_POLICY',
             'HARNESS_APPROVAL_ALL',
             # 屏幕工具默认开；本机 .env 若配了 0，不能把默认档测试前提改掉。
             'HARNESS_SCREEN_TOOLS', 'HARNESS_CLIPBOARD_TOOLS',
             'HARNESS_DESKTOP_CONTROL',
             # LLM 审查官默认关：开着会让审批类测试偷偷打真实 LLM API
             # （test_approval_scope 曾因此慢 12s 且卡片断言落空）。专测文件自己
             # delenv 打开。
             'HARNESS_LLM_APPROVAL')
_TIER_KEYS = ('HARNESS_SANDBOX_MODE', 'HARNESS_APPROVAL_POLICY',
              'HARNESS_APPROVAL_ALL')


@pytest.fixture(autouse=True)
def _isolate_cron_durable_path(tmp_path, monkeypatch):
    """cron 是进程级单例：用例之间清空任务/队列，并把 crons.json 指到临时目录。"""
    import cron_scheduler

    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    with cron_scheduler.cron_lock:
        cron_scheduler.scheduled_jobs.clear()
        cron_scheduler.cron_queue.clear()
        cron_scheduler._last_fired.clear()
    try:
        yield
    finally:
        with cron_scheduler.cron_lock:
            cron_scheduler.scheduled_jobs.clear()
            cron_scheduler.cron_queue.clear()
            cron_scheduler._last_fired.clear()


@pytest.fixture(autouse=True)
def _block_unstubbed_llm_calls(monkeypatch):
    """测试默认禁止真实模型请求：没打桩就失败，而不是悄悄产生费用。

    项目根的 .env 会设置真实的 base_url/model；agent_loop 的非流式路径或
    多 provider 客户端会绕过 ``harness.client`` 的补丁。这里同时封住
    ``llm_config.get_client``，让漏打的测试立刻显形。
    """
    def blocked(*args, **kwargs):
        raise AssertionError(
            'test attempted a real LLM request; patch harness.client / '
            '_stream_completion / _run_llm_request instead')

    # 只封主客户端的请求入口：get_client 需要保持真实，供多 provider/缓存测试使用。
    monkeypatch.setattr(harness.client.chat.completions, 'create', blocked)


@pytest.fixture(autouse=True)
def _isolate_credentials(tmp_path, monkeypatch):
    """凭据存储按用例隔离：本机真实的 credentials.bin 会让 /status 断言串味。

    `/status` 里有一行「凭据加密：N 项」。不隔离的话，本机迁移过凭据的机器
    显示 3 项、没迁移的机器显示明文 .env——同一个测试在两台机器上结论不同。
    """
    import credentials

    monkeypatch.setattr(credentials, 'store_path',
                        lambda: tmp_path / 'credentials.bin')


@pytest.fixture(autouse=True)
def _shutdown_ps_workers():
    """常驻 PowerShell worker 是进程级单例：上一个用例注册的 worker 会留着。

    留着的代价是真实进程：`test_get_worker_is_singleton_per_name` 注册完不走
    shutdown 的话，后面每个用例都带着一个常驻 PowerShell，既拖慢又可能因为
    名字撞车拿到别人的 worker（状态串味）。
    """
    import ps_worker

    ps_worker.shutdown()
    try:
        yield
    finally:
        ps_worker.shutdown()


@pytest.fixture(autouse=True)
def _reset_turn_slot_state():
    """回合槽是模块级全局：上一个用例残留的 _DEPTH>0 会让下一个用例误判重入。

    `turn_coordinator.acquire` 对「同线程重入」只增加嵌套计数、不再申请 OS 锁；
    如果某个用例没 release（异常/提前返回），下一个用例会拿到一个空槽、回合根本
    没跑，于是随机失败。这里每个用例前后都清零，把起点拉回干净状态。
    """
    import turn_coordinator

    turn_coordinator.reset_for_tests()
    try:
        yield
    finally:
        turn_coordinator.reset_for_tests()


@pytest.fixture(autouse=True)
def _reset_turn_control_state():
    """回合取消位/活动 token 是进程级状态：用例之间必须清零。

    生产语义保留“启动前按下的 Esc 会取消即将开始的回合”，所以不在 begin_turn
    里无条件清位；测试需要自己保证不把上一个用例的取消位带进下一个。
    """
    import turn_control

    turn_control.reset()
    try:
        yield
    finally:
        turn_control.reset()


@pytest.fixture(autouse=True)
def _isolate_storage_dirs(tmp_path):
    """把会话/快照/转录/工具结果目录指向临时目录。

    agent_loop 类测试（test_memory、test_context_manage 等）不传
    session_store 时，harness 会按模块全局 SESSIONS_DIR / CHECKPOINT_DIR /
    TRANSCRIPT_DIR 写进真实的 .cache —— 回归一次就往真实会话列表灌几十个
    「1 分钟前」的测试会话。这里每个测试都重定向，结束后还原。

    TOOL_RESULTS_DIR 必须一起隔离：persist_large_output / tool_result_budget
    写入的是按 tool_call_id 命名的固定文件名，指向真实 .cache/tool_results
    时，同时跑的另一个 pytest 进程会写到同一个文件上、并在自己的用例末尾
    把它 unlink —— 于是偶发 PermissionError / FileNotFoundError
    （已在并发压测中复现）。重定向后跨进程不再共享文件名。

    手动 try/finally 管理（不依赖 monkeypatch），理由见下方
    _restore_permission_backend 的注释：teardown 顺序必须保证最后还原。
    """
    saved = (harness.SESSIONS_DIR, harness.CHECKPOINT_DIR,
             harness.TRANSCRIPT_DIR, harness.TOOL_RESULTS_DIR,
             harness.SCREENSHOT_DIR,
             harness.MEMORY_DIR, harness.MEMORY_INDEX)
    harness.SESSIONS_DIR = tmp_path / 'sessions'
    harness.CHECKPOINT_DIR = tmp_path / 'checkpoints'
    harness.TRANSCRIPT_DIR = tmp_path / 'transcripts'
    harness.TOOL_RESULTS_DIR = tmp_path / 'tool_results'
    harness.SCREENSHOT_DIR = tmp_path / 'screenshots'
    harness.MEMORY_DIR = tmp_path / 'memories'
    harness.MEMORY_INDEX = harness.MEMORY_DIR / 'MEMORY.md'
    try:
        yield
    finally:
        (harness.SESSIONS_DIR, harness.CHECKPOINT_DIR,
         harness.TRANSCRIPT_DIR, harness.TOOL_RESULTS_DIR,
         harness.SCREENSHOT_DIR,
         harness.MEMORY_DIR, harness.MEMORY_INDEX) = saved


@pytest.fixture(autouse=True)
def _isolate_grant_ledger(tmp_path, monkeypatch):
    """持久目录授权账本按用例隔离：本机真实 grants.json 不能影响策略断言。"""
    import grants

    monkeypatch.setattr(grants, 'DURABLE_PATH', tmp_path / 'grants.json')
    monkeypatch.setattr(grants, 'LOCK_PATH', tmp_path / 'locks' / 'grants.lock')


@pytest.fixture(autouse=True)
def _isolate_net_grant_ledger(tmp_path, monkeypatch):
    """域名账本按用例隔离：本机真实 net_grants.json 不能影响策略断言。"""
    import net_grants

    monkeypatch.setattr(net_grants, 'DURABLE_PATH', tmp_path / 'net_grants.json')
    monkeypatch.setattr(net_grants, 'LOCK_PATH',
                        tmp_path / 'locks' / 'net_grants.lock')


@pytest.fixture(autouse=True)
def _isolate_user_rules(tmp_path, monkeypatch):
    """用户规则文件按用例隔离：本机真实 rules.yaml 不能影响策略断言。"""
    import rules

    monkeypatch.setattr(rules, 'DURABLE_PATH', tmp_path / 'rules.yaml')


@pytest.fixture(autouse=True)
def _isolate_cron_jobs():
    """cron 表是进程级全局：用例之间泄漏会让稳定 ID / 触发断言随执行顺序变化。

    默认心跳按稳定 ID 注册（服务启动路径），如果它从某个用例泄漏到下一个
    「注册同 ID 应成功」的用例，失败会看起来像随机顺序问题。这里对每个用例
    保存并清空，结束后按原样还原，不改变任何被测逻辑。
    """
    import cron_scheduler

    with cron_scheduler.cron_lock:
        saved_jobs = dict(cron_scheduler.scheduled_jobs)
        saved_queue = list(cron_scheduler.cron_queue)
        cron_scheduler.scheduled_jobs.clear()
        cron_scheduler.cron_queue.clear()
    try:
        yield
    finally:
        with cron_scheduler.cron_lock:
            cron_scheduler.scheduled_jobs.clear()
            cron_scheduler.scheduled_jobs.update(saved_jobs)
            cron_scheduler.cron_queue[:] = saved_queue


@pytest.fixture(autouse=True)
def _restore_permission_backend(tmp_path):
    """create_app 会把后端切到 remote；无论测试以何种顺序运行，结束后还原，
    并清空审批历史（请求登记是全局的，泄漏会让后续测试拾取到脏 pending）。

    同时钉住：记忆召回策略（本机 .env 可能配了 local/off，测试默认预期是
    llm 旧行为，test_memory_recall 自己显式切换）、审批超时压到 3s（泄漏
    时 3 秒显形为失败，而不是按 .env 的 600s 挂死）、执行档位（清掉环境里的
    档位配置，让所有用例从默认档起步；要试别的档位自己 setenv）。

    ⚠ 刻意不依赖 monkeypatch fixture：依赖它会让 monkeypatch 成为 autouse
    的前置、从而在 autouse 之后才 teardown——其 undo() 会把 backend 覆盖
    回 create_app 切成的 'remote'，绕过本兜底（2026-10-15 全量挂死根因）。
    这里用 try/finally 手动管理，保证本恢复逻辑最后执行。
    """
    old_env = {key: os.environ.get(key) for key in _ENV_KEYS}
    old_external_tools = set(hooks.EXTERNAL_TOOLS)
    os.environ['HARNESS_MEMORY_RECALL'] = 'llm'
    os.environ.pop('HARNESS_LOAD_MEMORIES', None)
    os.environ['HARNESS_APPROVAL_TIMEOUT'] = '3'
    # 工作区授权与登记文件按用例隔离：本机 .env 里的额外根不能把测试目录
    # 之外的路径带进 create_app；登记表默认写进用例自己的 tmp_path，
    # 否则默认路径会落到共享的测试父目录（甚至真实 .cache）里跨用例泄漏。
    os.environ.pop('HARNESS_WORKSPACE_ROOTS', None)
    os.environ['HARNESS_WORKSPACE_FILE'] = str(tmp_path / 'workspaces.json')
    # OS 沙箱默认关闭：本机 .env 开 auto 时，用它跑 tmp_path 里的真实命令会
    # 给测试目录打 Low 标签、拖慢甚至改变行为。需要沙箱的用例自己打开。
    os.environ['HARNESS_OS_SANDBOX'] = 'off'
    sandbox.reset_cache()
    # LLM 审查官默认关（见 _ENV_KEYS 注释）：审批类测试不偷偷打真实 LLM。
    os.environ['HARNESS_LLM_APPROVAL'] = '0'
    for key in _TIER_KEYS:
        os.environ.pop(key, None)   # 用例默认跑默认档，需要别的档位自己 setenv
    os.environ.pop('HARNESS_SCREEN_TOOLS', None)
    os.environ.pop('HARNESS_CLIPBOARD_TOOLS', None)
    os.environ.pop('HARNESS_DESKTOP_CONTROL', None)
    previous = permission_gate.backend
    previous_timeout = permission_gate.timeout
    permission_gate.timeout = 3   # GATE 在导入时已读走 timeout，setenv 来不及
    try:
        yield
    finally:
        permission_gate.backend = previous
        permission_gate.clear()
        permission_gate.timeout = previous_timeout
        # 第三方工具名单按用例隔离：MCP 注册类用例会往里写，泄漏出去会让别的
        # 用例的某个工具莫名变成「需要审批」。进程启动时已有的名单原样还原。
        hooks.EXTERNAL_TOOLS.clear()
        hooks.EXTERNAL_TOOLS.update(old_external_tools)
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
