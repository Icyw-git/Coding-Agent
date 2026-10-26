"""Desktop input backend validation and PowerShell contract tests; never touch a real desktop."""
import json
import subprocess

import pytest

import desktop_control_windows as desktop


def _record(state=None, action='snapshot'):
    state = state or {
        'available': True, 'title': 'Notepad', 'process': 'notepad', 'pid': 123,
        'window_id': 'ABCD', 'class_name': 'Notepad', 'rect': [0, 0, 800, 600],
        'focused_name': 'Text area', 'focused_control_type': 'ControlType.Document',
        'focused_is_password': False, 'sensitive_target': False,
    }
    data = {'ok': True, 'state': state}
    if action != 'snapshot':
        data = {'ok': True, 'state_after': state, 'action': action}
    return subprocess.CompletedProcess([], 0, json.dumps(data), '')


def _runner(record=None, seen=None):
    record = record or _record()
    def run(script, env, timeout):
        if seen is not None:
            seen.update(script=script, env=env, timeout=timeout)
        return record
    return run


def test_snapshot_returns_foreground_window_state():
    state = desktop.snapshot(runner=_runner())
    assert state.title == 'Notepad'
    assert state.pid == 123
    assert state.window_id == 'ABCD'
    assert state.rect == (0, 0, 800, 600)


@pytest.mark.parametrize('args', [
    {'action': 'click', 'expected_pid': 123, 'expected_window_id': 'ABCD', 'x': 2, 'y': 3},
    {'action': 'move', 'expected_pid': 123, 'expected_window_id': 'ABCD', 'x': 2, 'y': 3},
    {'action': 'type', 'expected_pid': 123, 'expected_window_id': 'ABCD', 'text': '中文'},
    {'action': 'key', 'expected_pid': 123, 'expected_window_id': 'ABCD', 'key': 'enter'},
    {'action': 'scroll', 'expected_pid': 123, 'expected_window_id': 'ABCD', 'amount': -2},
])
def test_action_encodes_payload_and_returns_state(args):
    seen = {}
    result = desktop.action(**args, runner=_runner(
        record=_record(action=args['action']), seen=seen))
    payload = json.loads(__import__('base64').b64decode(
        seen['env']['HARNESS_DESKTOP_PAYLOAD']))
    assert payload['action'] == args['action']
    assert payload['expected_pid'] == 123
    assert payload['expected_window_id'] == 'ABCD'
    assert result.state.pid == 123


def test_action_requires_window_snapshot_binding():
    with pytest.raises(desktop.DesktopControlError, match='expected_pid'):
        desktop.validate_action('click', x=1, y=1)
    with pytest.raises(desktop.DesktopControlError, match='expected_window_id'):
        desktop.validate_action('click', expected_pid=1, x=1, y=1)


@pytest.mark.parametrize('kwargs', [
    {'action': 'click', 'expected_pid': 1, 'expected_window_id': 'A', 'x': 'x', 'y': 1},
    {'action': 'type', 'expected_pid': 1, 'expected_window_id': 'A', 'text': 'x' * 1001},
    {'action': 'key', 'expected_pid': 1, 'expected_window_id': 'A', 'key': 'not a key'},
    {'action': 'scroll', 'expected_pid': 1, 'expected_window_id': 'A', 'amount': 21},
    {'action': 'invoke', 'expected_pid': 1, 'expected_window_id': 'A'},
    {'action': 'move', 'expected_pid': 1, 'expected_window_id': 'ZZ', 'x': 0, 'y': 0},
])
def test_invalid_actions_rejected_before_runner(kwargs):
    called = []
    with pytest.raises(desktop.DesktopControlError):
        desktop.action(**kwargs, runner=lambda *args: called.append(args))
    assert called == []


def test_power_shell_contract_checks_window_identity_and_sensitive_focus():
    assert 'GetForegroundWindow' in desktop.CONTROL_SCRIPT
    assert 'expected_window_id' in desktop.CONTROL_SCRIPT
    assert 'focused_is_password' in desktop.CONTROL_SCRIPT
    assert 'sensitive_target' in desktop.CONTROL_SCRIPT
    assert 'SendInput' in desktop.CONTROL_SCRIPT


def test_powershell_timeout_is_reported():
    def timed_out(*args):
        raise subprocess.TimeoutExpired('powershell', 1)
    with pytest.raises(desktop.DesktopControlError, match='timed out'):
        desktop.snapshot(runner=timed_out)
