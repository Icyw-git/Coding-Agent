import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent_runtime import AgentRuntime
from feishu_gateway import FeishuGateway
from gateway_sim import GatewayClient
from harness_api import create_app
from harness_sdk import HarnessClient
from session_store import SessionEventStore
from test_checkpoints import _loop_env
from test_context_manage import make_text_response, make_tool_response
from test_feishu_gateway import FakeTransport, _sync_asgi_transport
from workspace_context import current_workspace, use_workspace


def test_workspace_persists_and_invalid_changes_leave_session_unchanged(tmp_path):
    project = tmp_path / 'project with spaces'
    project.mkdir()
    runtime = AgentRuntime(lambda *a, **kw: 'done', SessionEventStore, tmp_path / 'sessions',
                           workspace=tmp_path)
    runtime.configure_workspace_root(tmp_path)
    sdk = HarnessClient(runtime)
    sid = sdk.sessions.create(workspace='"project with spaces"').id
    assert sdk.sessions.workspace(sid) == str(project)
    before = runtime.open_session(sid).path.read_bytes()
    for invalid in ('missing', '../..', str(tmp_path.parent), '   ', '""'):
        with pytest.raises(ValueError):
            sdk.sessions.change_workspace(sid, invalid)
        assert runtime.open_session(sid).path.read_bytes() == before
    restarted = AgentRuntime(lambda *a, **kw: 'done', SessionEventStore, tmp_path / 'sessions')
    restarted.configure_workspace_root(tmp_path)
    assert restarted.get_workspace(sid) == str(project)
    assert restarted.set_workspace(sid, '..') == str(tmp_path)


def test_missing_workspace_blocks_turn_until_explicitly_changed(tmp_path):
    project = tmp_path / 'removed'
    project.mkdir()
    calls = []
    runtime = AgentRuntime(lambda *a, **kw: calls.append(a), SessionEventStore,
                           tmp_path / 'sessions', workspace=tmp_path)
    sid = runtime.create_session(workspace=str(project))
    project.rmdir()
    before = runtime.open_session(sid).path.read_bytes()
    with pytest.raises(ValueError, match='does not exist'):
        runtime.send(sid, 'do not run in the wrong directory')
    assert calls == []
    assert runtime.open_session(sid).path.read_bytes() == before
    assert runtime.set_workspace(sid, str(tmp_path)) == str(tmp_path)


def test_workspace_is_restored_after_failed_turn(tmp_path):
    def fail(*args, **kwargs):
        assert current_workspace(None) == tmp_path
        raise RuntimeError('turn failed')

    runtime = AgentRuntime(fail, SessionEventStore, tmp_path / 'sessions', workspace=tmp_path)
    sid = runtime.create_session()
    with use_workspace(tmp_path.parent):
        with pytest.raises(RuntimeError, match='turn failed'):
            runtime.send(sid, 'go')
        assert current_workspace(None) == tmp_path.parent


def test_single_turn_gate_keeps_each_session_prompt_and_checkpoints(tmp_path, monkeypatch):
    """阶段 0 的全局单回合：并发发送被拒绝，串行回合仍各自使用自己的 workspace。

    旧用例同时跑两个真实回合来验证 workspace 隔离；全局单回合协调落地后这条路被
    有意关闭，这里改为「A 在模型调用中时 B 被 409 拒绝，随后 B 串行执行」，保留
    原用例对 prompt/工具/checkpoint 归属的断言。
    """
    import turn_coordinator

    harness, root = _loop_env(tmp_path, monkeypatch)
    roots = {name: root / name for name in ('a', 'b')}
    for directory in roots.values():
        directory.mkdir()
        (directory / 'shared.txt').write_text('original', encoding='utf-8')
    entered = threading.Event()
    release = threading.Event()
    command = 'cd' if sys.platform == 'win32' else 'pwd'

    def create(**kwargs):
        messages = kwargs['messages']
        name = next(m['content'] for m in messages if m['role'] == 'user')
        assert str(roots[name]) in messages[0]['content']
        if messages[-1]['role'] == 'tool':
            results = {m['tool_call_id']: m['content'] for m in messages if m['role'] == 'tool'}
            assert results['read'] == name
            assert 'shared.txt' in results['glob']
            assert results['bash'] == str(roots[name])
            return make_text_response('done')
        entered.set()
        assert release.wait(10), 'test did not release the first turn'
        return make_tool_response([
            ('write', 'write', json.dumps({'path': 'shared.txt', 'content': name})),
            ('read', 'read', '{"path":"shared.txt"}'),
            ('glob', 'glob', '{"pattern":"*.txt"}'),
            ('bash', 'bash', json.dumps({'command': command})),
        ])

    monkeypatch.setattr(harness.client.chat.completions, 'create', create)
    runtime = harness.create_runtime(initialize_tools=False)
    for name, directory in roots.items():
        runtime.create_session(name, workspace=str(directory))

    first = []
    thread = threading.Thread(target=lambda: first.append(runtime.send('a', 'a')))
    thread.start()
    try:
        assert entered.wait(5), 'first turn did not reach the model call'
        with pytest.raises(turn_coordinator.TurnBusy):
            runtime.send('b', 'b')
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert first and first[0].content == 'done'

    assert runtime.send('b', 'b').content == 'done'
    harness.wait_for_housekeeping()
    assert harness.WORKDIR == root and harness.get_workdir() == root
    for name, directory in roots.items():
        assert (directory / 'shared.txt').read_text(encoding='utf-8') == name
        assert runtime.get_workspace(name) == str(directory)
        manager = harness.CheckpointManager(harness.CHECKPOINT_DIR, directory, name)
        changed = next(row for row in manager.list() if row['files'])
        assert manager.get(changed['cp_id'])['workspace'] == str(directory)
        assert manager.get(changed['cp_id'])['files']['shared.txt']['exists'] is True


def test_background_tool_keeps_workspace_after_parent_turn_ends(tmp_path):
    import background_task

    release, finished = threading.Event(), threading.Event()
    seen = []

    def tool(**kwargs):
        assert release.wait(3)
        seen.append(current_workspace(None))
        finished.set()
        return 'done'

    call = SimpleNamespace(id='bg', function=SimpleNamespace(name='bash', arguments='{}'))
    with use_workspace(tmp_path):
        bg_id = background_task.start_background_task(call, {'bash': tool})
    try:
        release.set()
        assert finished.wait(3)
        assert seen == [tmp_path]
        for _ in range(100):
            with background_task.background_lock:
                status = background_task.background_tasks[bg_id]['status']
            if status == 'completed':
                break
            time.sleep(0.01)
        assert status == 'completed'
    finally:
        release.set()
        with background_task.background_lock:
            background_task.background_tasks.pop(bg_id, None)
            background_task.background_results.pop(bg_id, None)


def test_teammate_keeps_workspace_without_inheriting_parent_checkpoint(tmp_path, monkeypatch):
    import agent_teams
    import hooks

    harness, root = _loop_env(tmp_path, monkeypatch)
    release = threading.Event()
    seen, acked, threads = [], [], []
    original_thread = threading.Thread

    def thread_factory(*args, **kwargs):
        thread = original_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    def build_tools(*args):
        assert release.wait(3)
        # 会话要跟着进 teammate 线程（审批白名单按它分区），checkpoint manager 不跟：
        # teammate 不该把快照打在 lead 的会话上。
        seen.append((harness.get_workdir(), harness.active_checkpoint_manager(),
                     hooks.current_session_id()))
        return [], {}

    monkeypatch.setattr(agent_teams.threading, 'Thread', thread_factory)
    monkeypatch.setattr(agent_teams, '_build_team_tools', build_tools)
    monkeypatch.setattr(agent_teams, 'active_teammates', {})
    monkeypatch.setattr(agent_teams, 'BUS', SimpleNamespace(
        claim_inbox=lambda name: [{'id': 'm1'}], send=lambda *args: None,
        ack=lambda agent, ids: acked.append((agent, list(ids)))))
    monkeypatch.setattr(agent_teams, 'handle_inbox_message', lambda *args: 'shutdown')
    token = harness._checkpoint_manager.set(SimpleNamespace(session_id='parent'))
    try:
        with use_workspace(root):
            agent_teams.spawn_teammate_thread('test-worker', 'tester', 'inspect')
        release.set()
        threads[0].join(3)
        assert not threads[0].is_alive()
        assert seen == [(root, None, 'parent')]
        # shutdown 消息注入后立刻确认；没有 ack 的消息下次会重放
        assert acked == [('test-worker', ['m1'])]
        assert not agent_teams.active_teammates
    finally:
        release.set()
        harness._checkpoint_manager.reset(token)


def test_feishu_workspace_survives_new_resume_and_gateway_restart(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    seen = []
    runtime = AgentRuntime(lambda *a, **kw: seen.append(current_workspace(None)) or 'done',
                           SessionEventStore, tmp_path / 'sessions')
    api = GatewayClient('http://test', 't', transport=_sync_asgi_transport(
        create_app(runtime, api_token='t', workspace=tmp_path, task_root=tmp_path / 'tasks')))
    transport = FakeTransport()
    bindings = tmp_path / 'bindings.json'
    gw = FeishuGateway(api, transport=transport, bindings_path=bindings)
    try:
        gw.on_message('chat', '/cd "project"')
        original = gw.bindings['chat']
        assert api.get_workspace(original) == str(project)
        gw.on_message('chat', 'inspect project')
        assert seen == [project]
        gw.on_message('chat', '/new next')
        assert api.get_workspace('next') == str(project)
        gw.on_message('chat', '/cd ..')
        assert api.get_workspace('next') == str(tmp_path)
        gw.on_message('chat', f'/resume {original}')
        assert gw.bindings['chat'] == original
        assert f'工作区: {project}' in transport.last()
        restarted = FeishuGateway(api, transport=transport, bindings_path=bindings)
        restarted.on_message('chat', '/pwd')
        assert str(project) in transport.last()
        restarted.on_message('other', '/cd ..')
        assert 'other' not in restarted.bindings
        assert 'outside the allowed root' in transport.last()
    finally:
        api.close()


def test_workspace_api_auth_validation_and_busy_session(tmp_path):
    started, release = threading.Event(), threading.Event()

    def run(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return 'done'

    runtime = AgentRuntime(run, SessionEventStore, tmp_path / 'sessions')
    app = create_app(runtime, api_token='t', workspace=tmp_path, task_root=tmp_path / 'tasks')
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer t'}
        created = client.post('/v1/sessions', json={'id': 'busy'}, headers=headers)
        assert created.status_code == 201
        url = '/v1/sessions/busy/workspace'
        assert client.get(url).status_code == 401
        assert client.put(url, json={'path': '..'}, headers=headers).status_code == 400
        assert client.get('/v1/sessions/missing/workspace', headers=headers).status_code == 404
        thread = threading.Thread(target=lambda: runtime.send('busy', 'go'))
        thread.start()
        assert started.wait(2)
        try:
            response = client.put(url, json={'path': '.'}, headers=headers)
            assert response.status_code == 409
            assert runtime.get_workspace('busy') == str(tmp_path)
        finally:
            release.set()
            thread.join(5)
        assert client.put(url, json={'path': '.'}, headers=headers).status_code == 200


def test_workspace_registry_api_and_multiple_authorized_roots(tmp_path, monkeypatch):
    primary = tmp_path / 'primary'
    extra = tmp_path / 'extra'
    outside = tmp_path / 'outside'
    for directory in (primary, extra, outside):
        directory.mkdir()
    monkeypatch.setenv('HARNESS_WORKSPACE_ROOTS', str(extra))
    runtime = AgentRuntime(lambda *a, **kw: 'done', SessionEventStore,
                           tmp_path / 'sessions', workspace=primary)
    app = create_app(runtime, api_token='t', workspace=primary,
                     task_root=tmp_path / 'tasks')
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer t'}

        listing = client.get('/v1/workspaces', headers=headers).json()
        assert listing['roots'] == [str(primary), str(extra)]
        assert listing['current'] == str(primary)
        assert listing['workspaces'][0]['path'] == str(primary)

        added = client.post('/v1/workspaces',
                            json={'path': str(extra), 'name': 'Extra'},
                            headers=headers)
        assert added.status_code == 201
        entry = added.json()['workspace']
        assert entry['name'] == 'Extra'
        row = next(row for row in
                   client.get('/v1/workspaces', headers=headers).json()['workspaces']
                   if row['path'] == str(extra))
        assert row['saved'] is True and row['allowed'] is True

        created = client.post('/v1/sessions',
                              json={'id': 'in-extra', 'workspace': str(extra)},
                              headers=headers)
        assert created.status_code == 201
        assert runtime.get_workspace('in-extra') == str(extra)
        rejected = client.post('/v1/sessions',
                               json={'id': 'outside', 'workspace': str(outside)},
                               headers=headers)
        assert rejected.status_code == 400

        removed = client.delete(f"/v1/workspaces/{entry['id']}", headers=headers)
        assert removed.status_code == 200
        assert removed.json()['removed']['path'] == str(extra)
        assert client.delete('/v1/workspaces/missing',
                             headers=headers).status_code == 404
