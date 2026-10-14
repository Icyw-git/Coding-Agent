"""测试会话级隔离：跨测试还原全局权限门后端，避免 create_app 的状态泄漏。"""

import pytest

from hooks import GATE as permission_gate


@pytest.fixture(autouse=True)
def _restore_permission_backend():
    """create_app 会把后端切到 remote；无论测试以何种顺序运行，结束后还原。"""
    previous = permission_gate.backend
    yield
    permission_gate.backend = previous
