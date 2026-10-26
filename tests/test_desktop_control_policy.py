"""Desktop-control policy: opt-in, per-action human approval, no background bypass."""
import hooks
from exec_policy import ASK, DENY, Policy


def test_desktop_control_is_off_by_default(monkeypatch):
    monkeypatch.delenv('HARNESS_DESKTOP_CONTROL', raising=False)
    for tool in ('desktop_snapshot', 'desktop_action'):
        decision = Policy().evaluate(tool, {'action': 'click'})
        assert decision.action == DENY
        assert decision.code == 'desktop_control_disabled'


def test_enabled_desktop_control_always_asks_and_never_auto_allows(monkeypatch):
    monkeypatch.setenv('HARNESS_DESKTOP_CONTROL', '1')
    for mode in ('read-only', 'workspace-write', 'danger-full-access'):
        decision = Policy(sandbox_mode=mode, approval_policy='on-request').evaluate(
            'desktop_action', {'action': 'type', 'text': 'hello'})
        assert decision.action == ASK
        assert decision.code == 'desktop_control'
    # Even full access cannot suppress explicit consent for local input.
    decision = Policy(sandbox_mode='danger-full-access',
                      approval_policy='never').evaluate('desktop_action', {})
    assert decision.action == DENY
    assert decision.code == 'desktop_control_approval_required'


def test_desktop_control_denies_without_interactive_turn(monkeypatch):
    monkeypatch.setenv('HARNESS_DESKTOP_CONTROL', '1')
    message = hooks.enforce('desktop_action', {'action': 'click'}, interactive=False)
    assert 'requires an interactive turn' in message


def test_desktop_control_never_uses_session_allow(monkeypatch):
    monkeypatch.setenv('HARNESS_DESKTOP_CONTROL', '1')
    monkeypatch.setattr(hooks.GATE, 'session_allows', lambda *args, **kwargs: True)
    asked = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda tool, args, reason: asked.append(tool) or (True, 'ok'))
    assert hooks.enforce('desktop_action', {'action': 'click'}, interactive=True) is None
    assert asked == ['desktop_action']


def test_desktop_control_is_rejected_in_scheduled_turn(monkeypatch):
    monkeypatch.setenv('HARNESS_DESKTOP_CONTROL', '1')
    with hooks.use_scheduled_turn():
        message = hooks.enforce('desktop_action', {'action': 'click'}, interactive=True)
    assert 'scheduled task is read-only' in message
