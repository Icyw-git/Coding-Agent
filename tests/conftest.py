"""测试会话级隔离：跨测试还原全局权限门后端，避免 create_app 的状态泄漏。"""

import os

import pytest

import harness
from hooks import GATE as permission_gate

_ENV_KEYS = ('HARNESS_MEMORY_RECALL', 'HARNESS_LOAD_MEMORIES',
             'HARNESS_APPROVAL_TIMEOUT')


@pytest.fixture(autouse=True)
def _isolate_storage_dirs(tmp_path):
    """把会话/快照/转录目录指向临时目录。

    agent_loop 类测试（test_memory、test_context_manage 等）不传
    session_store 时，harness 会按模块全局 SESSIONS_DIR / CHECKPOINT_DIR /
    TRANSCRIPT_DIR 写进真实的 .cache —— 回归一次就往真实会话列表灌几十个
    「1 分钟前」的测试会话。这里每个测试都重定向，结束后还原。

    手动 try/finally 管理（不依赖 monkeypatch），理由见下方
    _restore_permission_backend 的注释：teardown 顺序必须保证最后还原。
    """
    saved = (harness.SESSIONS_DIR, harness.CHECKPOINT_DIR, harness.TRANSCRIPT_DIR)
    harness.SESSIONS_DIR = tmp_path / 'sessions'
    harness.CHECKPOINT_DIR = tmp_path / 'checkpoints'
    harness.TRANSCRIPT_DIR = tmp_path / 'transcripts'
    try:
        yield
    finally:
        (harness.SESSIONS_DIR, harness.CHECKPOINT_DIR,
         harness.TRANSCRIPT_DIR) = saved


@pytest.fixture(autouse=True)
def _restore_permission_backend():
    """create_app 会把后端切到 remote；无论测试以何种顺序运行，结束后还原，
    并清空审批历史（请求登记是全局的，泄漏会让后续测试拾取到脏 pending）。

    同时钉住：记忆召回策略（本机 .env 可能配了 local/off，测试默认预期是
    llm 旧行为，test_memory_recall 自己显式切换）、审批超时压到 3s（泄漏
    时 3 秒显形为失败，而不是按 .env 的 600s 挂死）。

    ⚠ 刻意不依赖 monkeypatch fixture：依赖它会让 monkeypatch 成为 autouse
    的前置、从而在 autouse 之后才 teardown——其 undo() 会把 backend 覆盖
    回 create_app 切成的 'remote'，绕过本兜底（2026-10-15 全量挂死根因）。
    这里用 try/finally 手动管理，保证本恢复逻辑最后执行。
    """
    old_env = {key: os.environ.get(key) for key in _ENV_KEYS}
    os.environ['HARNESS_MEMORY_RECALL'] = 'llm'
    os.environ.pop('HARNESS_LOAD_MEMORIES', None)
    os.environ['HARNESS_APPROVAL_TIMEOUT'] = '3'
    previous = permission_gate.backend
    previous_timeout = permission_gate.timeout
    permission_gate.timeout = 3   # GATE 在导入时已读走 timeout，setenv 来不及
    try:
        yield
    finally:
        permission_gate.backend = previous
        permission_gate.clear()
        permission_gate.timeout = previous_timeout
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
