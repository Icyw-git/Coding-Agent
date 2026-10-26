"""Pending tool recovery classification: guidance only, never re-execution."""
import json

import pytest

import tool_recovery
from session_store import SessionEventStore


def call(name, args=None, call_id='call_1'):
    return {
        'id': call_id,
        'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(args or {})},
    }


def category(name, args=None):
    return tool_recovery.classify_tool_call(call(name, args)).category


@pytest.mark.parametrize('name,args', [
    ('read', {'path': 'a.txt'}),
    ('view_image', {'path': 'a.png'}),
    ('read_document', {'path': 'a.pdf'}),
    ('render_document', {'path': 'a.pdf'}),
    ('glob', {'pattern': '*.py'}),
    ('grep', {'pattern': 'needle'}),
    ('ls', {}),
    ('web_fetch', {'url': 'https://example.com'}),
    ('load_skill', {'name': 'demo'}),
    ('todo_read', {}),
    ('list_tasks', {}),
    ('get_task', {'task_id': 'task_1'}),
    ('list_crons', {}),
    ('background_output', {'bg_id': 'bg_1'}),
    ('bash', {'command': 'ls -la'}),
    ('bash', {'command': 'git status'}),
    ('find_sessions', {'query': 'x'}),
    ('screen_ocr', {}),
    ('clipboard_read', {}),
])
def test_read_only_tools(name, args):
    assert category(name, args) == tool_recovery.READ_ONLY


@pytest.mark.parametrize('name,args', [
    ('write', {'path': 'a.txt', 'content': 'x'}),
    ('todo_write', {'todos': []}),
    ('claim_task', {'task_id': 'task_1'}),
    ('complete_task', {'task_id': 'task_1'}),
    ('cancel_cron', {'job_id': 'cron_1'}),
    ('kill_background', {'bg_id': 'bg_1'}),
    ('keep_worktree', {'name': 'wt'}),
    ('clipboard_write', {'text': 'x'}),
])
def test_idempotent_tools(name, args):
    assert category(name, args) == tool_recovery.IDEMPOTENT


@pytest.mark.parametrize('name,args', [
    ('edit', {'path': 'a.txt', 'old_text': 'a', 'new_text': 'b'}),
    ('apply_patch', {'patch': '*** Begin Patch'}),
    ('create_task', {'subject': 'new'}),
    ('schedule_cron', {'cron': '* * * * *', 'prompt': 'go'}),
    ('spawn_teammate', {'name': 'x', 'role': 'r', 'prompt': 'p'}),
    ('send_message', {'to': 'x', 'content': 'hi'}),
    ('check_inbox', {}),
    ('request_shutdown', {'teammate': 'x'}),
    ('request_plan', {'teammate': 'x', 'task': 't'}),
    ('review_plan', {'request_id': 'r', 'approve': True}),
    ('create_worktree', {'name': 'wt'}),
    ('remove_worktree', {'name': 'wt'}),
    ('mcp_server_search', {'query': 'x'}),
    ('bash', {'command': 'rm -rf build'}),
    ('bash', {'command': 'python script.py'}),
    ('bash', {'command': 'echo hi > file.txt'}),
])
def test_destructive_or_unknown_tools(name, args):
    assert category(name, args) == tool_recovery.DESTRUCTIVE


@pytest.mark.parametrize('raw', ['{"command":', '[]', 'null', ''])
def test_malformed_arguments_are_treated_as_destructive(raw):
    entry = {'id': 'bad', 'type': 'function',
             'function': {'name': 'bash', 'arguments': raw}}
    assert tool_recovery.classify_tool_call(entry).category == tool_recovery.DESTRUCTIVE


def test_malformed_call_shape_does_not_crash():
    pending = tool_recovery.classify_tool_call({'id': 'x'})
    assert pending.name == 'unknown'
    assert pending.category == tool_recovery.DESTRUCTIVE
    assert tool_recovery.classify_tool_call(None).category == tool_recovery.DESTRUCTIVE


def test_close_message_preserves_uncertainty_and_category_advice():
    read = tool_recovery.classify_tool_call(call('read', {'path': 'a.txt'}, 'r'))
    edit = tool_recovery.classify_tool_call(call('edit', {}, 'e'))
    write = tool_recovery.classify_tool_call(call('write', {}, 'w'))

    for pending in (read, edit, write):
        text = tool_recovery.close_message(pending)
        assert 'outcome is unknown' in text
        assert f'Recovery class: {pending.category}' in text

    assert 'safe to repeat' in tool_recovery.close_message(read)
    assert 'repeat if the operation did not take effect' in tool_recovery.close_message(write)
    assert 'do not repeat blindly' in tool_recovery.close_message(edit)


def test_resume_message_groups_calls_least_to_most_careful():
    pending = [
        tool_recovery.classify_tool_call(call('edit', {}, 'e')),
        tool_recovery.classify_tool_call(call('write', {}, 'w')),
        tool_recovery.classify_tool_call(call('read', {}, 'r')),
    ]
    text = tool_recovery.resume_message(pending)
    assert text.startswith('<recovery>Session resumed after interruption.')
    assert text.index('- read-only:') < text.index('- idempotent:') < text.index('- destructive:')
    assert '- read-only: read (r)' in text
    assert '- idempotent: write (w)' in text
    assert '- destructive: edit (e)' in text
    assert 'No tool was re-executed automatically.' in text


def test_rebuild_recovers_each_pending_call_with_its_own_class(tmp_path):
    store = SessionEventStore(tmp_path, session_id='classes')
    store.append_message({'role': 'assistant', 'content': None, 'tool_calls': [
        call('read', {'path': 'a.txt'}, 'r'),
        call('write', {'path': 'a.txt', 'content': 'x'}, 'w'),
        call('edit', {'path': 'a.txt', 'old_text': 'a', 'new_text': 'b'}, 'e'),
    ]})
    messages = store.rebuild_openai_history(recover_pending=True)
    results = {message['tool_call_id']: message['content']
               for message in messages if message['role'] == 'tool'}
    assert 'read-only' in results['r']
    assert 'idempotent' in results['w']
    assert 'destructive' in results['e']
    assert 'outcome is unknown' in results['e']


def test_recovery_message_uses_classified_calls(tmp_path):
    store = SessionEventStore(tmp_path, session_id='pending')
    store.append_message({'role': 'assistant', 'content': None, 'tool_calls': [
        call('read', {'path': 'a.txt'}, 'r'),
        call('bash', {'command': 'rm -rf build'}, 'b'),
    ]})
    content = store.recovery_message()['content']
    assert '- read-only: read (r)' in content
    assert '- destructive: bash (b)' in content


def test_resume_collects_recovery_before_closing_pending_calls(tmp_path, monkeypatch):
    import harness

    store = SessionEventStore(tmp_path / 'sessions', session_id='resume')
    store.append_message({'role': 'user', 'content': 'go'})
    store.append_message({'role': 'assistant', 'content': None, 'tool_calls': [
        call('read', {'path': 'a.txt'}, 'r'),
        call('bash', {'command': 'rm -rf build'}, 'b'),
    ]})
    captured = {}

    def fake_agent_loop(messages, session_store=None):
        captured['messages'] = messages
        captured['store'] = session_store
        return 'resumed'

    monkeypatch.setattr(harness, 'agent_loop', fake_agent_loop)
    assert harness.resume_agent_loop(store.path) == 'resumed'
    messages = captured['messages']
    assert [message['role'] for message in messages] == [
        'user', 'assistant', 'tool', 'tool', 'user']
    results = {message['tool_call_id']: message['content']
               for message in messages if message['role'] == 'tool'}
    assert 'read-only' in results['r']
    assert 'destructive' in results['b']
    recovery = messages[-1]['content']
    assert '- read-only: read (r)' in recovery
    assert '- destructive: bash (b)' in recovery
    assert store.read_events()[-1]['type'] == 'message'
