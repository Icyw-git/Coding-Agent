"""teammate 事件：独立日志可回放，父会话只留引用；记录失败不影响执行。"""
import threading
from types import SimpleNamespace

import pytest

import agent_teams
import subagent_events
from session_store import SessionEventStore
from test_checkpoints import _loop_env
from workspace_context import use_workspace


def _response(content):
    message = SimpleNamespace(role='assistant', content=content, tool_calls=None)
    message.model_dump = lambda: {
        'role': 'assistant', 'content': content, 'tool_calls': None}
    return SimpleNamespace(choices=[SimpleNamespace(message=message,
                                                    finish_reason='stop')])


@pytest.fixture
def team_env(tmp_path, monkeypatch):
    """临时工作区 + 独立父会话；teammate 线程不继承 ContextVar。"""
    harness, workspace = _loop_env(tmp_path, monkeypatch)
    store = SessionEventStore(harness.SESSIONS_DIR, session_id='parent')
    token = harness._checkpoint_manager.set(
        SimpleNamespace(session_id='parent', session_store=store))
    sent = []
    threads = []
    original_thread = threading.Thread

    def thread_factory(*args, **kwargs):
        thread = original_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(agent_teams, 'threading', SimpleNamespace(Thread=thread_factory))
    monkeypatch.setattr(agent_teams, 'active_teammates', {})
    monkeypatch.setattr(agent_teams, 'BUS', SimpleNamespace(
        claim_inbox=lambda name: [], ack=lambda *args: None,
        send=lambda *args: sent.append(args)))
    monkeypatch.setattr(agent_teams, '_build_team_tools', lambda *args: ([], {}))
    try:
        yield SimpleNamespace(harness=harness, workspace=workspace, store=store,
                              sent=sent, threads=threads)
    finally:
        harness._checkpoint_manager.reset(token)
        for thread in threads:
            thread.join(3)


def _spawn(env, monkeypatch, content='done'):
    def create(**kwargs):
        return _response(content)

    monkeypatch.setattr(agent_teams, 'client', SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    with use_workspace(env.workspace):
        agent_teams.spawn_teammate_thread('worker-events', 'tester', 'inspect')
    env.threads[0].join(3)
    assert not env.threads[0].is_alive()


def test_teammate_writes_child_log_and_parent_reference(team_env, monkeypatch):
    monkeypatch.setattr(agent_teams, 'idle_poll', lambda *args: 'shutdown')

    _spawn(team_env, monkeypatch)

    parent_events = team_env.store.read_events()
    started = next(e for e in parent_events if e['type'] == 'teammate_started')
    finished = next(e for e in parent_events if e['type'] == 'teammate_finished')
    assert started['teammate'] == 'worker-events'
    assert started['role'] == 'tester'
    assert finished['run_id'] == started['run_id']
    assert finished['status'] == 'completed' and finished['turns'] == 1
    assert team_env.sent[-1] == ('worker-events', 'lead', 'done', 'result')
    assert not agent_teams.active_teammates

    child_path = team_env.store.path.parent / started['log']
    child = SessionEventStore.open(child_path)
    assert [m['role'] for m in child.rebuild_openai_history()] == [
        'system', 'user', 'assistant']
    child_events = child.read_events()
    assert [event['type'] for event in child_events][0] == 'teammate_started'
    assert child_events[-1]['type'] == 'teammate_finished'
    assert child_events[-1]['status'] == 'completed'


def test_teammate_logs_messages_injected_while_idle(team_env, monkeypatch):
    calls = []

    def fake_idle(name, messages, role, wt_ctx=None):
        if not calls:
            messages.append({'role': 'user',
                             'content': '<auto-claimed>Task task_1: inspect</auto-claimed>'})
            calls.append('work')
            return 'work'
        calls.append('shutdown')
        return 'shutdown'

    monkeypatch.setattr(agent_teams, 'idle_poll', fake_idle)

    _spawn(team_env, monkeypatch)

    started = next(e for e in team_env.store.read_events()
                   if e['type'] == 'teammate_started')
    child = SessionEventStore.open(team_env.store.path.parent / started['log'])
    contents = [m.get('content') for m in child.rebuild_openai_history()]
    assert any('<auto-claimed>' in str(content) for content in contents)
    assert calls == ['work', 'shutdown']


def test_teammate_renews_lease_each_turn(team_env, monkeypatch):
    renewed = []

    def fake_renew(task_id, owner='agent', **kwargs):
        renewed.append((task_id, owner))
        return f'Renewed lease for {task_id}'

    calls = []

    def fake_idle(name, messages, role, wt_ctx=None):
        if not calls:
            wt_ctx['task_id'] = 'task_long'
            calls.append('work')
            return 'work'
        calls.append('shutdown')
        return 'shutdown'

    monkeypatch.setattr(agent_teams, 'renew_lease', fake_renew)
    monkeypatch.setattr(agent_teams, 'idle_poll', fake_idle)

    _spawn(team_env, monkeypatch)

    # 第二轮循环开始续租；认领发生在第一轮 idle，之后每轮都要刷
    assert renewed == [('task_long', 'worker-events')]


def test_teammate_llm_failure_records_error_and_finishes(team_env, monkeypatch):
    def create(**kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr(agent_teams, 'client', SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))

    with use_workspace(team_env.workspace):
        agent_teams.spawn_teammate_thread('worker-events', 'tester', 'inspect')
    team_env.threads[0].join(3)

    assert not team_env.threads[0].is_alive()
    finished = [e for e in team_env.store.read_events()
                if e['type'] == 'teammate_finished']
    assert len(finished) == 1
    assert finished[0]['status'] == 'error'
    assert finished[0]['error_type'] == 'RuntimeError'
    assert team_env.sent[-1] == ('worker-events', 'lead', 'Done.', 'result')
    assert not agent_teams.active_teammates


def test_teammate_without_parent_session_still_runs(tmp_path, monkeypatch):
    harness, workspace = _loop_env(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_teams, 'active_teammates', {})
    monkeypatch.setattr(agent_teams, 'BUS', SimpleNamespace(
        claim_inbox=lambda name: [], ack=lambda *args: None,
        send=lambda *args: None))
    monkeypatch.setattr(agent_teams, '_build_team_tools', lambda *args: ([], {}))
    monkeypatch.setattr(agent_teams, 'idle_poll', lambda *args: 'shutdown')
    monkeypatch.setattr(agent_teams, 'client', SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kwargs: _response('done')))))

    with use_workspace(workspace):
        agent_teams.spawn_teammate_thread('worker-events', 'tester', 'inspect')
    for _ in range(100):
        if not agent_teams.active_teammates:
            break
        threading.Event().wait(0.01)
    assert not agent_teams.active_teammates
    assert not (harness.SESSIONS_DIR / 'teammates').exists()


def test_recorder_failure_does_not_break_teammate(team_env, monkeypatch):
    def broken_store(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(subagent_events, 'SessionEventStore', broken_store)
    monkeypatch.setattr(agent_teams, 'idle_poll', lambda *args: 'shutdown')

    _spawn(team_env, monkeypatch)

    assert team_env.sent[-1] == ('worker-events', 'lead', 'done', 'result')
    assert not (team_env.store.path.parent / 'teammates').exists()
