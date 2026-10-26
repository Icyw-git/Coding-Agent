"""Classify interrupted tool calls for recovery guidance.

A session can stop after the assistant emitted tool calls but before their
results were persisted. Re-running those calls automatically is unsafe: some
only read state, some converge when repeated, and some can duplicate or
consume side effects. This module classifies missing calls so recovery text
can tell the model how carefully to proceed. It never executes a tool.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from policy_bash import is_readonly_command

READ_ONLY = 'read-only'
IDEMPOTENT = 'idempotent'
DESTRUCTIVE = 'destructive'

# Retrying these has no durable side effect. Keep in sync with tool_registry;
# unknown names (including MCP tools) fall back to DESTRUCTIVE.
READ_ONLY_TOOLS = frozenset({
    'read', 'view_image', 'read_document', 'render_document',
    'glob', 'grep', 'ls', 'web_fetch', 'load_skill',
    'todo_read', 'list_tasks', 'get_task', 'list_crons', 'background_output',
    'find_sessions', 'screen_ocr', 'clipboard_read',
})

# Repeating converges to the same state or is refused by a status guard.
IDEMPOTENT_TOOLS = frozenset({
    'write', 'todo_write', 'claim_task', 'complete_task',
    'cancel_cron', 'kill_background', 'keep_worktree',
    'rename_session', 'revoke_permissions', 'clipboard_write',
})

# Repeating may duplicate, consume, or apply twice; inspect state first.
# edit/apply_patch are here because old_text or a hunk can match again after a
# partial success. check_inbox consumes the mailbox. Unknown tools are treated
# the same way because their side effects are not statically knowable.
DESTRUCTIVE_TOOLS = frozenset({
    'edit', 'apply_patch', 'create_task', 'schedule_cron',
    'spawn_teammate', 'send_message', 'check_inbox', 'request_shutdown',
    'request_plan', 'review_plan', 'create_worktree', 'remove_worktree',
    'screen_capture',
})

# Category order is the order used in recovery text: least to most careful.
CATEGORIES = (READ_ONLY, IDEMPOTENT, DESTRUCTIVE)


@dataclass(frozen=True)
class PendingTool:
    """One tool call whose result never reached the session event stream."""

    call_id: str
    name: str
    category: str

    @property
    def label(self) -> str:
        return f'{self.name} ({self.call_id})'


def _parsed_arguments(call: dict) -> dict | None:
    function = call.get('function')
    if not isinstance(function, dict):
        return None
    raw = function.get('arguments')
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def classify(name: str, args: dict | None) -> str:
    """Return the recovery class for a named tool call."""
    if not name:
        return DESTRUCTIVE
    if name == 'bash':
        # Malformed arguments would be rejected before execution, so the retry
        # cannot be judged from the command text; treat it as unsafe.
        if args is None:
            return DESTRUCTIVE
        command = str(args.get('command') or '')
        return READ_ONLY if is_readonly_command(command) else DESTRUCTIVE
    if name in READ_ONLY_TOOLS:
        return READ_ONLY
    if name in IDEMPOTENT_TOOLS:
        return IDEMPOTENT
    return DESTRUCTIVE


def classify_tool_call(call: object) -> PendingTool:
    """Classify a raw OpenAI ``tool_calls`` entry without trusting its shape."""
    if not isinstance(call, dict):
        return PendingTool('', 'unknown', DESTRUCTIVE)
    call_id = str(call.get('id') or '')
    function = call.get('function')
    name = ''
    if isinstance(function, dict):
        name = str(function.get('name') or '').strip()
    return PendingTool(call_id, name or 'unknown', classify(name, _parsed_arguments(call)))


def close_message(pending: PendingTool) -> str:
    """Synthetic tool result for a call that never produced one."""
    if pending.category == READ_ONLY:
        advice = 'No durable state was changed; safe to repeat if still needed.'
    elif pending.category == IDEMPOTENT:
        advice = ('Verify the current state, then repeat if the operation did not '
                  'take effect.')
    else:
        advice = ('Inspect the current state first; do not repeat blindly. The call '
                  'may have completed, partially applied, or consumed a resource.')
    return ('Error: Tool execution was interrupted; no result was recorded. '
            'The outcome is unknown.\n'
            f'Recovery class: {pending.category} ({pending.name}). {advice}')


def resume_message(pending: list[PendingTool]) -> str:
    """Recovery note inserted once when a session resumes with pending calls."""
    grouped = {category: [] for category in CATEGORIES}
    for item in pending:
        grouped[item.category].append(item.label)
    lines = ['<recovery>Session resumed after interruption. Pending tool calls:']
    for category in CATEGORIES:
        if not grouped[category]:
            continue
        if category == READ_ONLY:
            advice = 'no durable state changed; safe to repeat if still needed'
        elif category == IDEMPOTENT:
            advice = 'verify state, then repeat if the operation did not take effect'
        else:
            advice = 'inspect state first; these may have completed or partially applied'
        lines.append(f'- {category}: ' + ', '.join(grouped[category]) + f' — {advice}.')
    lines.append('No tool was re-executed automatically.</recovery>')
    return '\n'.join(lines)
