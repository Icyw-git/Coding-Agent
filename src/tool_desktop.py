"""Desktop control tool layer: formatting and privacy-preserving audit."""
from __future__ import annotations

import hashlib

import desktop_control_windows


def _audit(audit, event: str, **payload) -> None:
    if audit is None:
        return
    try:
        audit(event, **payload)
    except Exception:
        # Audit failure must not turn an already completed input action into a
        # second failure, but the tool result remains intentionally minimal.
        pass


def _state_text(state) -> str:
    title = state.title or '(untitled)'
    process = state.process or 'unknown process'
    focused = state.focused_name or state.focused_control_type or 'unknown control'
    rect = ','.join(str(value) for value in state.rect) if state.rect else 'unknown'
    return (f'Foreground window: {title} ({process}, pid={state.pid}, '
            f'window_id={state.window_id}); rect={rect}; focused={focused}')


def snapshot(*, runner=None, audit=None) -> str:
    try:
        state = desktop_control_windows.snapshot(runner=runner)
    except desktop_control_windows.DesktopControlError as exc:
        _audit(audit, 'desktop_snapshot_failed', reason=str(exc)[:120])
        return f'Error: desktop snapshot failed: {exc}'
    _audit(audit, 'desktop_snapshot', process=state.process, pid=state.pid,
           window_id=state.window_id,
           title_sha1=hashlib.sha1(state.title.encode('utf-8')).hexdigest(),
           sensitive_target=state.sensitive_target)
    return _state_text(state)


def action(action_name: str, *, x=None, y=None, text=None, key=None,
           amount=None, expected_pid=None, expected_window_id=None,
           runner=None, audit=None) -> str:
    try:
        result = desktop_control_windows.action(
            action_name, x=x, y=y, text=text, key=key, amount=amount,
            expected_pid=expected_pid, expected_window_id=expected_window_id,
            runner=runner)
    except desktop_control_windows.DesktopControlError as exc:
        _audit(audit, 'desktop_action_failed', action=str(action_name)[:30],
               reason=str(exc)[:120])
        return f'Error: desktop action failed: {exc}'
    state = result.state
    _audit(audit, 'desktop_action', action=result.action, process=state.process,
           pid=state.pid, window_id=state.window_id,
           duration_ms=result.duration_ms,
           title_sha1=hashlib.sha1(state.title.encode('utf-8')).hexdigest(),
           sensitive_target=state.sensitive_target)
    return f'Completed desktop action {result.action}. {_state_text(state)}'
