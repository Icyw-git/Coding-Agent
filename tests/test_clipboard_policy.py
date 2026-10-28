"""剪贴板工具的审批边界：逐次询问、后台/定时拒绝、可关闭。"""
import pytest

import exec_policy
import hooks


@pytest.fixture()
def config(tmp_path, monkeypatch):
    class Config:
        DENY_LIST = []
        DESTRUCTIVE = []

        @staticmethod
        def get_workdir():
            return tmp_path

        @staticmethod
        def active_checkpoint_manager():
            return None

    monkeypatch.setattr(hooks, '_h', lambda: Config)
    monkeypatch.delenv('HARNESS_CLIPBOARD_TOOLS', raising=False)
    hooks.GATE.clear()
    yield
    hooks.GATE.clear()


def test_clipboard_tools_always_ask(monkeypatch):
    monkeypatch.delenv('HARNESS_CLIPBOARD_TOOLS', raising=False)
    for tool in ('clipboard_read', 'clipboard_write'):
        for mode in ('read-only', 'workspace-write'):
            decision = exec_policy.Policy(mode, 'on-request').evaluate(tool, {})
            assert decision.action == exec_policy.ASK
            assert decision.code in ('clipboard_access', 'clipboard_write')


def test_clipboard_tools_never_policy_denies():
    assert exec_policy.Policy('workspace-write', 'never').evaluate(
        'clipboard_read', {}).action == exec_policy.DENY
    assert exec_policy.Policy('danger-full-access', 'never').evaluate(
        'clipboard_read', {}).action == exec_policy.ALLOW


def test_clipboard_tools_can_be_disabled(monkeypatch):
    monkeypatch.setenv('HARNESS_CLIPBOARD_TOOLS', '0')
    for tool in ('clipboard_read', 'clipboard_write'):
        decision = exec_policy.Policy().evaluate(tool, {})
        assert decision.action == exec_policy.DENY
        assert decision.code == 'clipboard_tools_disabled'
        assert 'HARNESS_CLIPBOARD_TOOLS=0' in decision.reason


def test_clipboard_tools_enabled_parsing():
    assert exec_policy.clipboard_tools_enabled({}) is True
    assert exec_policy.clipboard_tools_enabled({'HARNESS_CLIPBOARD_TOOLS': ''}) is True
    assert exec_policy.clipboard_tools_enabled({'HARNESS_CLIPBOARD_TOOLS': '1'}) is True
    for value in ('0', 'off', 'false', 'NO', 'disabled'):
        assert exec_policy.clipboard_tools_enabled(
            {'HARNESS_CLIPBOARD_TOOLS': value}) is False


def test_background_turn_denies_clipboard_tools(config):
    read = hooks.enforce('clipboard_read', {}, interactive=False)
    write = hooks.enforce('clipboard_write', {'text': 'x'}, interactive=False)

    assert 'requires an interactive turn' in read
    assert 'local clipboard' in read
    assert 'clipboard_write' in write


def test_clipboard_tools_ignore_the_session_whitelist(config, monkeypatch):
    monkeypatch.setattr(hooks.GATE, 'session_allows', lambda *a, **k: True)
    asked = []
    monkeypatch.setattr(
        hooks.GATE, 'decide',
        lambda tool, args, reason, **kw: asked.append(tool) or (True, 'ok'))

    assert hooks.enforce('clipboard_read', {}, interactive=True) is None
    assert hooks.enforce('clipboard_write', {'text': 'x'}, interactive=True) is None
    assert asked == ['clipboard_read', 'clipboard_write']


def test_disabled_clipboard_tools_are_denied_before_asking(config, monkeypatch):
    monkeypatch.setenv('HARNESS_CLIPBOARD_TOOLS', '0')
    asked = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda tool, args, reason, **kw: asked.append(tool) or (True, 'ok'))

    message = hooks.enforce('clipboard_read', {}, interactive=True)

    assert 'HARNESS_CLIPBOARD_TOOLS=0' in message
    assert asked == []


def test_scheduled_turn_forbids_clipboard_even_when_writable(config):
    with hooks.use_scheduled_turn(read_only=True):
        assert 'scheduled task is read-only' in hooks.enforce(
            'clipboard_read', {}, interactive=True)
        assert 'scheduled task is read-only' in hooks.enforce(
            'clipboard_write', {'text': 'x'}, interactive=False)

    with hooks.use_scheduled_turn(read_only=False):
        message = hooks.enforce('clipboard_write', {'text': 'x'},
                                interactive=False)
        assert 'requires an interactive turn' in message
