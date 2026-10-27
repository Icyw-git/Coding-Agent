"""desktop_windows / focus：列窗口、切窗口的身份校验与策略边界。"""
import pytest

import desktop_control_windows as desktop
from exec_policy import ASK, DENY, Policy


class _Reply:
    def __init__(self, payload):
        self.payload = payload
        self.returncode = 0
        self.stdout = ''
        self.stderr = ''


def _fake_run(monkeypatch, payload):
    monkeypatch.setattr(desktop, '_run', lambda *a, **kw: payload)


def test_list_windows_formats_entries(monkeypatch):
    _fake_run(monkeypatch, {
        'ok': True, 'total': 2,
        'windows': [
            {'title': 'Notepad', 'process': 'notepad', 'pid': 42,
             'window_id': 'AB12', 'foreground': True, 'minimized': False,
             'rect': [0, 0, 800, 600]},
            {'title': 'Explorer', 'process': 'explorer', 'pid': 7,
             'window_id': 'CD34', 'foreground': False, 'minimized': True,
             'rect': None},
        ]})
    result = desktop.list_windows(limit=10)
    assert result['total'] == 2
    first = result['windows'][0]
    assert first == {'title': 'Notepad', 'process': 'notepad', 'pid': 42,
                     'window_id': 'AB12', 'foreground': True,
                     'minimized': False, 'rect': (0, 0, 800, 600)}
    assert result['windows'][1]['rect'] is None


def test_list_windows_clamps_limit(monkeypatch):
    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return {'ok': True, 'total': 0, 'windows': []}

    monkeypatch.setattr(desktop, '_run', fake)
    desktop.list_windows(limit=10 ** 6)
    assert seen['limit'] == desktop.MAX_WINDOWS
    desktop.list_windows(limit=0)
    assert seen['limit'] == 1


def test_list_windows_rejects_bad_limit():
    with pytest.raises(desktop.DesktopControlError):
        desktop.list_windows(limit='many')


def test_focus_requires_both_identifiers():
    with pytest.raises(desktop.DesktopControlError, match='expected_pid'):
        desktop.focus_window(expected_window_id='AB12')
    with pytest.raises(desktop.DesktopControlError, match='expected_window_id'):
        desktop.focus_window(expected_pid=42)


def test_focus_passes_both_identifiers(monkeypatch):
    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return {'ok': True, 'state_after': {'available': True,
                                            'process': 'notepad',
                                            'pid': 42, 'window_id': 'AB12'}}

    monkeypatch.setattr(desktop, '_run', fake)
    state = desktop.focus_window(expected_pid=42, expected_window_id='ab12')
    assert seen == {'action': 'focus', 'expected_pid': 42,
                    'expected_window_id': 'AB12'}
    assert state.process == 'notepad'


def test_focus_rejects_invalid_window_id():
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_action('focus', expected_pid=42,
                                expected_window_id='not-hex')


# ---------- 策略边界 ----------

@pytest.mark.parametrize('tool', ['desktop_windows', 'desktop_snapshot',
                                  'desktop_action'])
def test_desktop_tools_are_off_by_default(monkeypatch, tool):
    monkeypatch.delenv('HARNESS_DESKTOP_CONTROL', raising=False)
    assert Policy().evaluate(tool, {}).action == DENY


def test_list_windows_still_needs_approval_when_enabled(monkeypatch):
    """列窗口虽然只读，但它暴露的是整台机器的窗口标题——仍要问。"""
    monkeypatch.setenv('HARNESS_DESKTOP_CONTROL', '1')
    decision = Policy().evaluate('desktop_windows', {})
    assert decision.action == ASK
    assert decision.code == 'desktop_control'


def test_list_windows_never_auto_allowed_by_tier(monkeypatch):
    monkeypatch.setenv('HARNESS_DESKTOP_CONTROL', '1')
    decision = Policy(sandbox_mode='danger-full-access',
                      approval_policy='never').evaluate('desktop_windows', {})
    assert decision.action == DENY
