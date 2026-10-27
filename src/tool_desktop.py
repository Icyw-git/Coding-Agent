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
    if not state.available:
        detail = f' ({state.error})' if state.error else ''
        return f'No foreground window available{detail}'
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
           amount=None, button=None, count=None, steps=None,
           expected_pid=None, expected_window_id=None,
           runner=None, audit=None) -> str:
    try:
        result = desktop_control_windows.action(
            action_name, x=x, y=y, text=text, key=key, amount=amount,
            button=button, count=count, steps=steps,
            expected_pid=expected_pid,
            expected_window_id=expected_window_id,
            runner=runner)
    except desktop_control_windows.DesktopControlError as exc:
        _audit(audit, 'desktop_action_failed', action=str(action_name)[:30],
               reason=str(exc)[:120])
        return f'Error: desktop action failed: {exc}'
    state = result.state
    step_records = getattr(result, 'steps', ()) or ()
    audit_payload = {
        'action': result.action, 'process': state.process, 'pid': state.pid,
        'window_id': state.window_id, 'duration_ms': result.duration_ms,
        'title_sha1': hashlib.sha1(state.title.encode('utf-8')).hexdigest(),
        'sensitive_target': state.sensitive_target,
    }
    if result.action == 'sequence':
        audit_payload['steps'] = len(step_records)
    _audit(audit, 'desktop_action', **audit_payload)
    if result.action == 'sequence':
        lines = [f'Completed {len(step_records)} sequence steps:']
        for index, step in enumerate(step_records, 1):
            lines.append(f'  {index}. {step.get("action", "?")} ok')
        lines.append(_state_text(state))
        return '\n'.join(lines)
    return f'Completed desktop action {result.action}. {_state_text(state)}'


def list_windows(*, limit: int = 20, runner=None, audit=None) -> str:
    """列出可见窗口。只读，不需要先知道前台是谁。"""
    try:
        result = desktop_control_windows.list_windows(limit=limit, runner=runner)
    except desktop_control_windows.DesktopControlError as exc:
        return f'Error: listing windows failed: {exc}'
    windows = result['windows']
    _audit(audit, 'desktop_list_windows', count=len(windows),
           total=result['total'])
    if not windows:
        return 'No visible top-level windows.'
    lines = [f"Visible windows ({len(windows)} of {result['total']}):"]
    for index, item in enumerate(windows, 1):
        marks = []
        if item['foreground']:
            marks.append('foreground')
        if item['minimized']:
            marks.append('minimized')
        suffix = f" [{', '.join(marks)}]" if marks else ''
        lines.append(f"  {index}. pid={item['pid']} window_id={item['window_id']} "
                     f"{item['process']}: {item['title']}{suffix}")
    lines.append('To act on one, call desktop_snapshot after focusing it, or use '
                 "desktop_action with action='focus' and that pid + window_id.")
    return chr(10).join(lines)


def focus(action_args: dict, *, runner=None, audit=None) -> str:
    """把窗口切到前台；仍要 pid + window_id 双因子。"""
    try:
        state = desktop_control_windows.focus_window(
            expected_pid=action_args.get('expected_pid'),
            expected_window_id=action_args.get('expected_window_id'),
            runner=runner)
    except desktop_control_windows.DesktopControlError as exc:
        _audit(audit, 'desktop_focus_failed', reason=str(exc)[:120])
        return f'Error: focusing that window failed: {exc}'
    _audit(audit, 'desktop_focus', process=state.process, pid=state.pid,
           window_id=state.window_id,
           title_sha1=hashlib.sha1(state.title.encode('utf-8')).hexdigest())
    return f'Focused that window. {_state_text(state)}'
