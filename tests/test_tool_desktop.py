"""Desktop tool formatting and audit-redaction tests."""
from types import SimpleNamespace

import desktop_control_windows
import tool_desktop


def test_snapshot_audit_does_not_store_window_title(monkeypatch):
    state = desktop_control_windows.WindowState(
        available=True, title='Private document title', process='notepad',
        pid=42, window_id='ABCD', rect=(0, 0, 640, 480),
        focused_name='Document',
    )
    monkeypatch.setattr(desktop_control_windows, 'snapshot', lambda **kwargs: state)
    events = []
    output = tool_desktop.snapshot(
        audit=lambda event, **payload: events.append((event, payload)))
    assert 'Private document title' not in str(events)
    assert 'Private document title' in output
    assert events[0][1]['title_sha1']


def test_action_audit_omits_typed_text(monkeypatch):
    monkeypatch.setattr(desktop_control_windows, 'action', lambda *args, **kwargs:
                       SimpleNamespace(
                           action='type', duration_ms=5,
                           state=desktop_control_windows.WindowState(
                               available=True, title='Notepad', process='notepad',
                               pid=42, window_id='ABCD')))
    events = []
    tool_desktop.action(
        'type', text='private text', expected_pid=42,
        expected_window_id='ABCD',
        audit=lambda event, **payload: events.append((event, payload)))
    assert 'private text' not in repr(events)
    assert events[0][0] == 'desktop_action'
    assert events[0][1]['action'] == 'type'
