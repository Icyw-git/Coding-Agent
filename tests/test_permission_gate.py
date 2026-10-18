"""PermissionGate 单元测试：双后端决议、超时熔断、决议历史。"""

import threading
import time

import pytest

from hooks import PermissionGate


def test_console_backend_approve_and_deny(monkeypatch, capsys):
    gate = PermissionGate()
    monkeypatch.setattr('builtins.input', lambda prompt='': 'y')
    allowed, message = gate.decide('bash', {'command': 'del x'}, 'destructive')
    assert allowed and 'approved' in message

    monkeypatch.setattr('builtins.input', lambda prompt='': 'n')
    allowed, _ = gate.decide('bash', {'command': 'rd x'}, 'destructive')
    assert not allowed
    assert 'perm_' not in capsys.readouterr().out


def test_console_eof_denies_instead_of_crashing(monkeypatch):
    gate = PermissionGate()

    def raise_eof(prompt=''):
        raise EOFError

    monkeypatch.setattr('builtins.input', raise_eof)
    allowed, _ = gate.decide('bash', {'command': 'x'}, 'destructive')
    assert not allowed
    assert gate.history(1)[0].resolved_by == 'console(EOF)'


def test_console_a_differs_from_y_only_by_the_session_allowlist(monkeypatch):
    """CLI 的 a = 允许这一次 + 本会话同类免审（与审批卡片三个按钮对齐）。

    第三方工具默认每次询问，没有卡片的 CLI 必须有这条免审出路；同类键仍由
    服务端按被审批的内容推出，不是用户给的标签。
    """
    gate = PermissionGate()
    monkeypatch.setattr('builtins.input', lambda prompt='': 'y')
    allowed, _ = gate.decide('bash', {'command': 'del x'}, 'destructive')
    assert allowed and not gate.session_allows('bash', {'command': 'del y'})

    monkeypatch.setattr('builtins.input', lambda prompt='': 'a')
    allowed, _ = gate.decide('bash', {'command': 'del a'}, 'destructive')
    assert allowed and gate.session_allows('bash', {'command': 'del b'})
    assert not gate.session_allows('bash', {'command': 'rm b'})


def test_remote_resolve_unblocks_waiter():
    gate = PermissionGate()
    gate.backend = 'remote'
    results = {}

    def waiter():
        results['allowed'], results['message'] = gate.decide(
            'bash', {'command': 'del x'}, 'destructive')

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    for _ in range(50):
        if gate.pending():
            break
        time.sleep(0.02)

    pending = gate.pending()
    assert len(pending) == 1 and pending[0].tool == 'bash'
    req_id = pending[0].req_id

    record = gate.resolve(req_id, allow=True, by='remote')
    assert record.status == 'approved'
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert results['allowed'] is True


def test_remote_timeout_denies_by_default():
    gate = PermissionGate(timeout=0.05)
    gate.backend = 'remote'
    allowed, message = gate.decide('bash', {'command': 'x'}, 'destructive')
    assert not allowed
    assert 'timeout' in message
    assert gate.history(1)[0].status == 'timeout'


def test_resolve_unknown_id_and_double_resolve():
    gate = PermissionGate()
    with pytest.raises(KeyError):
        gate.resolve('perm_missing', allow=True)

    gate.backend = 'remote'
    gate.request('bash', {}, 'reason')
    req_id = gate.pending()[0].req_id
    gate.resolve(req_id, allow=True)
    with pytest.raises(RuntimeError):
        gate.resolve(req_id, allow=False)


def test_to_dict_hides_event_and_history_orders_newest_first():
    gate = PermissionGate(timeout=0.01)
    gate.backend = 'remote'
    gate.decide('bash', {}, 'first')
    time.sleep(0.01)
    gate.decide('bash', {}, 'second')
    history = gate.history()
    assert len(history) == 2
    assert history[0].created_at >= history[1].created_at
    dumped = history[0].to_dict()
    assert 'req_id' in dumped and '_event' not in dumped
