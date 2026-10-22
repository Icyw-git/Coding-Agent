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


def test_console_prompt_shows_the_whole_command_and_escapes_control_chars(monkeypatch, capsys):
    """审批提示是人做决定的地方：命令尾部切不得，也不能让命令自己改写终端。"""
    gate = PermissionGate()
    monkeypatch.setattr('builtins.input', lambda prompt='': 'n')
    command = 'echo ' + 'a' * 1500 + ' | tee ~/.bashrc\x1b[2K'
    gate.decide('bash', {'command': command}, 'destructive')
    out = capsys.readouterr().out
    assert 'tee ~/.bashrc' in out      # 旧版 200 字符摘要正好把它切没
    assert '\\x1b[2K' in out          # 命令里的 ESC 换成可见转义，不被终端解析


def test_console_hides_the_session_option_for_classless_commands(monkeypatch, capsys):
    """解释器 / ssh / docker 没有「同类」可记，CLI 不能摆一个不起作用的 a。"""
    gate = PermissionGate()
    prompts = []
    monkeypatch.setattr('builtins.input',
                        lambda prompt='': prompts.append(prompt) or 'a')
    allowed, _ = gate.decide('bash', {'command': 'python -c "import os"'}, 'opaque')
    assert allowed
    assert 'a=本会话允许同类' not in prompts[0]
    assert '不记「同类」' in capsys.readouterr().out
    assert not gate._session_allows


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


def test_approval_timeout_config_is_parsed_not_crashing(monkeypatch, capsys):
    """审批门在 import 时构造：笔误既不该让进程起不来，也不该静默回落。"""
    import hooks

    for value in ('5m', '0', '-3'):
        monkeypatch.setenv('HARNESS_APPROVAL_TIMEOUT', value)
        assert hooks.approval_timeout() == hooks.DEFAULT_APPROVAL_TIMEOUT
    printed = capsys.readouterr().err
    assert printed.count('HARNESS_APPROVAL_TIMEOUT') == 3

    monkeypatch.setenv('HARNESS_APPROVAL_TIMEOUT', '12.5')
    assert hooks.approval_timeout() == 12.5
    assert PermissionGate().timeout == 12.5
    assert PermissionGate(timeout=0.5).timeout == 0.5   # 显式参数优先于环境


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


class _FakeUIGate:
    use_ui = True

    def __init__(self, allow):
        self.allow = allow
        self.calls = []

    def request(self, tool, args, title=''):
        self.calls.append((tool, args, title))
        return self.allow


def test_ui_mode_routes_approval_to_modal_without_stdin(monkeypatch, capsys):
    import harness

    fake = _FakeUIGate(allow=True)
    monkeypatch.setattr(harness, 'permission_gate', fake)
    monkeypatch.setattr('builtins.input', lambda prompt='': (_ for _ in ()).throw(
        AssertionError('UI mode must not read stdin')))
    gate = PermissionGate()

    allowed, message = gate.decide('bash', {'command': 'rm x'}, 'destructive')

    assert allowed and message.startswith('approved')
    assert fake.calls == [('bash', {'command': 'rm x'}, 'destructive')]
    assert capsys.readouterr().out == ''   # 不向 TUI 的控制台打印


def test_ui_mode_denies_when_modal_says_no(monkeypatch):
    import harness

    monkeypatch.setattr(harness, 'permission_gate', _FakeUIGate(allow=False))
    gate = PermissionGate()

    allowed, message = gate.decide('bash', {'command': 'rm x'}, 'destructive')

    assert not allowed and message.startswith('denied')


class _ScopedUIGate:
    use_ui = True

    def __init__(self, allowed=True, scope=None):
        self.allowed = allowed
        self.scope = scope
        self.calls = []

    def request_scoped(self, tool, args, title='', *, allow_session=True):
        self.calls.append((tool, args, title, allow_session))
        return self.allowed, self.scope


def test_ui_mode_session_scope_reaches_the_session_allowlist(monkeypatch):
    """Textual 的 a 必须与 CLI 的 a 等价：只放行同类，不扩大范围。"""
    import harness

    fake = _ScopedUIGate(scope='session')
    monkeypatch.setattr(harness, 'permission_gate', fake)
    gate = PermissionGate()

    allowed, _ = gate.decide('bash', {'command': 'del x'}, 'destructive')

    assert allowed
    assert fake.calls == [('bash', {'command': 'del x'}, 'destructive', True)]
    assert gate.session_allows('bash', {'command': 'del y'})
    assert not gate.session_allows('bash', {'command': 'rm y'})


def test_ui_mode_never_grants_scope_for_classless_commands(monkeypatch):
    """解释器没有同类键；渲染层即使带回 scope，审批门也必须丢弃。"""
    import harness

    fake = _ScopedUIGate(scope='session')
    monkeypatch.setattr(harness, 'permission_gate', fake)
    gate = PermissionGate()

    allowed, _ = gate.decide('bash', {'command': 'python -c "import os"'}, 'opaque')

    assert allowed
    assert fake.calls[0][-1] is False
    assert not gate._session_allows
