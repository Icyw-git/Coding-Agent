"""测试会话级隔离：跨测试还原全局权限门后端，避免 create_app 的状态泄漏。"""

import os

import pytest

import harness
import hooks
import sandbox
from hooks import GATE as permission_gate

_ENV_KEYS = ('HARNESS_MEMORY_RECALL', 'HARNESS_LOAD_MEMORIES',
             'HARNESS_APPROVAL_TIMEOUT', 'HARNESS_OS_SANDBOX',
             # 执行档位：本机 .env 配了 read-only/never 时，不能把「默认档即重构前
             # 行为」的测试前提一起改掉。需要特定档位的用例自己 setenv。
             'HARNESS_SANDBOX_MODE', 'HARNESS_APPROVAL_POLICY',
             'HARNESS_APPROVAL_ALL')
_TIER_KEYS = ('HARNESS_SANDBOX_MODE', 'HARNESS_APPROVAL_POLICY',
              'HARNESS_APPROVAL_ALL')


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
             harness.TRANSCRIPT_DIR, harness.TOOL_RESULTS_DIR)
    harness.SESSIONS_DIR = tmp_path / 'sessions'
    harness.CHECKPOINT_DIR = tmp_path / 'checkpoints'
    harness.TRANSCRIPT_DIR = tmp_path / 'transcripts'
    harness.TOOL_RESULTS_DIR = tmp_path / 'tool_results'
    try:
        yield
    finally:
        (harness.SESSIONS_DIR, harness.CHECKPOINT_DIR,
         harness.TRANSCRIPT_DIR, harness.TOOL_RESULTS_DIR) = saved


@pytest.fixture(autouse=True)
def _restore_permission_backend():
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
    # OS 沙箱默认关闭：本机 .env 开 auto 时，用它跑 tmp_path 里的真实命令会
    # 给测试目录打 Low 标签、拖慢甚至改变行为。需要沙箱的用例自己打开。
    os.environ['HARNESS_OS_SANDBOX'] = 'off'
    sandbox.reset_cache()
    for key in _TIER_KEYS:
        os.environ.pop(key, None)   # 用例默认跑默认档，需要别的档位自己 setenv
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
