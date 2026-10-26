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


def test_sequence_output_lists_steps_and_audit_counts_them(monkeypatch):
    monkeypatch.setattr(desktop_control_windows, 'action', lambda *args, **kwargs:
                       SimpleNamespace(
                           action='sequence', duration_ms=9,
                           steps=({'action': 'click', 'ok': True},
                                  {'action': 'type', 'ok': True}),
                           state=desktop_control_windows.WindowState(
                               available=True, title='Notepad', process='notepad',
                               pid=42, window_id='ABCD')))
    events = []
    output = tool_desktop.action(
        'sequence', steps=[{'action': 'click', 'x': 1, 'y': 2},
                           {'action': 'type', 'text': 'private text'}],
        expected_pid=42, expected_window_id='ABCD',
        audit=lambda event, **payload: events.append((event, payload)))
    assert '1. click ok' in output
    assert '2. type ok' in output
    assert 'private text' not in repr(events)
    assert events[0][0] == 'desktop_action'
    assert events[0][1]['steps'] == 2
    assert events[0][1]['action'] == 'sequence'


def test_snapshot_unavailable_reports_error(monkeypatch):
    monkeypatch.setattr(desktop_control_windows, 'snapshot', lambda **kwargs:
                       desktop_control_windows.WindowState(
                           available=False, error='no_foreground_window'))
    events = []
    output = tool_desktop.snapshot(
        audit=lambda event, **payload: events.append((event, payload)))
    assert 'No foreground window available (no_foreground_window)' in output
    assert events[0][0] == 'desktop_snapshot'


def test_click_options_reach_the_capability_layer(monkeypatch):
    seen = {}

    def fake(action, **kwargs):
        seen.update(action=action, **kwargs)
        return SimpleNamespace(
            action=action, duration_ms=3,
            state=desktop_control_windows.WindowState(
                available=True, title='Notepad', process='notepad',
                pid=42, window_id='ABCD'))

    monkeypatch.setattr(desktop_control_windows, 'action', fake)
    tool_desktop.action('click', x=5, y=6, button='right', count=2,
                        expected_pid=42, expected_window_id='ABCD')
    assert seen['button'] == 'right'
    assert seen['count'] == 2
