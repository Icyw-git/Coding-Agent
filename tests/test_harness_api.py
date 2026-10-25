import importlib.util
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

from harness_api import create_app

fastapi_available = importlib.util.find_spec("fastapi") is not None


@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient

    with ExitStack() as stack:
        yield lambda app: stack.enter_context(TestClient(app))


def _approve_task_command(client, headers, task_id):
    for _ in range(100):
        pending = client.get('/v1/permissions', headers=headers).json()['pending']
        if pending:
            request = pending[0]
            response = client.post(
                f"/v1/permissions/{request['req_id']}/resolve",
                json={'allow': True}, headers=headers)
            assert response.status_code == 200
            return
        time.sleep(0.01)
    raise AssertionError(f'task {task_id} did not request approval')


class Result:
    def __init__(self, session_id, content):
        self.session_id = session_id
        self.content = content


class FakeRuntime:
    def __init__(self):
        self.sessions = {}

    def create_session(self, session_id=None):
        session_id = session_id or "api-session"
        if session_id in self.sessions:
            from agent_runtime import SessionAlreadyExistsError
            raise SessionAlreadyExistsError(session_id)
        self.sessions[session_id] = []
        return session_id

    def history(self, session_id):
        if session_id not in self.sessions:
            raise FileNotFoundError(session_id)
        return list(self.sessions[session_id])

    def list_sessions(self, limit=20):
        return [{'id': sid, 'title': '(untitled session)', 'events': 0,
                 'updated': 0.0} for sid in list(self.sessions)[:limit]]

    def send(self, session_id, content, on_delta=None):
        if session_id not in self.sessions:
            raise FileNotFoundError(session_id)
        self.sessions[session_id].extend([
            {"role": "user", "content": content},
            {"role": "assistant", "content": "done"},
        ])
        if on_delta:
            on_delta('done')
        return Result(session_id, "done")


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_streams_message_deltas(api_client):
    runtime = FakeRuntime()
    client = api_client(create_app(runtime, api_token='t'))
    headers = {'Authorization': 'Bearer t'}
    assert client.post('/v1/sessions', json={'id': 'stream-api'}, headers=headers).status_code == 201
    response = client.post('/v1/sessions/stream-api/messages/stream',
                           json={'content': 'hello'}, headers=headers)
    assert response.status_code == 200
    assert 'event: delta' in response.text
    assert 'event: done' in response.text


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_routes_use_runtime(monkeypatch, api_client):

    runtime = FakeRuntime()
    client = api_client(create_app(runtime, api_token="test-token"))

    headers = {"Authorization": "Bearer test-token"}
    created = client.post("/v1/sessions", json={"id": "api-test"}, headers=headers)
    assert created.status_code == 201
    assert created.json()["id"] == "api-test"

    response = client.post(
        "/v1/sessions/api-test/messages",
        json={"content": "hello"}, headers=headers,
    )
    assert response.status_code == 200
    assert response.json() == {"session_id": "api-test", "content": "done"}

    history = client.get("/v1/sessions/api-test", headers=headers)
    assert history.status_code == 200
    assert len(history.json()["history"]) == 2


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_maps_missing_session_to_404(api_client):

    client = api_client(create_app(FakeRuntime(), api_token="test-token"))
    response = client.get("/v1/sessions/missing", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 404


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_maps_duplicate_session_to_409(api_client):

    runtime = FakeRuntime()
    client = api_client(create_app(runtime, api_token="test-token"))
    headers = {"Authorization": "Bearer test-token"}
    assert client.post("/v1/sessions", json={"id": "same"}, headers=headers).status_code == 201
    response = client.post("/v1/sessions", json={"id": "same"}, headers=headers)
    assert response.status_code == 409


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_requires_token_and_limits_requests(tmp_path, api_client):

    runtime = FakeRuntime()
    client = api_client(create_app(runtime, api_token="secret", rate_limit=1,
                                   access_log_path=str(tmp_path / "api.log")))
    assert client.get("/v1/sessions/missing").status_code == 401
    headers = {"Authorization": "Bearer secret"}
    assert client.get("/v1/sessions/missing", headers=headers).status_code == 404
    assert client.get("/v1/sessions/missing", headers=headers).status_code == 429
    log = (tmp_path / "api.log").read_text(encoding="utf-8")
    assert "path=/v1/sessions/missing" in log
    assert "secret" not in log


# ---------- 会话列表 / SSE 实时流 ----------

@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_lists_sessions(api_client):

    client = api_client(create_app(FakeRuntime(), api_token="t"))
    headers = {"Authorization": "Bearer t"}
    client.post("/v1/sessions", json={"id": "api-test"}, headers=headers)
    response = client.get("/v1/sessions", headers=headers)
    assert response.status_code == 200
    assert any(s["id"] == "api-test" for s in response.json()["sessions"])


def _stream_runtime(tmp_path):
    from agent_runtime import AgentRuntime
    from session_store import SessionEventStore

    return AgentRuntime(
        run_turn=lambda messages, **kwargs: "ok",
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path / "sessions",
    )


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_stream_replays_session_events(tmp_path, api_client):
    """SSE 端点回放既有事件流（含非 message 事件）；once=1 回放即关闭。"""

    runtime = _stream_runtime(tmp_path)
    client = api_client(create_app(runtime, api_token="t"))
    headers = {"Authorization": "Bearer t"}
    created = client.post("/v1/sessions", json={"id": "live-1"}, headers=headers)
    assert created.status_code == 201

    with client.stream("GET", "/v1/sessions/live-1/stream",
                       params={"poll": 0.05, "once": 1},
                       headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        seen = []
        for line in response.iter_lines():
            if line.startswith("event:"):
                seen.append(line[6:].strip())
        # 自然结束（done 帧），不中途断开
    assert "session_created" in seen
    assert seen[-1] == "done"


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_stream_follow_picks_up_new_events(tmp_path, api_client):
    """follow 阶段能增量拾取连接之后新写入的事件（实时监控的核心路径）。"""
    import threading


    runtime = _stream_runtime(tmp_path)
    client = api_client(create_app(runtime, api_token="t"))
    headers = {"Authorization": "Bearer t"}
    client.post("/v1/sessions", json={"id": "live-2"}, headers=headers)
    store = runtime.open_session("live-2")

    def late_append():
        time.sleep(0.4)
        store.append_message({"role": "user", "content": "late arrival"})

    threading.Thread(target=late_append, daemon=True).start()
    with client.stream("GET", "/v1/sessions/live-2/stream",
                       params={"poll": 0.05, "follow_seconds": 1.5},
                       headers=headers) as response:
        body = "".join(response.iter_lines())
    assert "late arrival" in body


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_stream_maps_missing_session_to_404(tmp_path, api_client):

    client = api_client(create_app(_stream_runtime(tmp_path), api_token="t"))
    response = client.get("/v1/sessions/nope/stream",
                          headers={"Authorization": "Bearer t"})
    assert response.status_code == 404


# ---------- 任务端点 ----------

@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_task_end_to_end_flow(tmp_path, api_client):
    """网关核心链路：创建任务 → 轮询状态 → 拉取日志 → 列表。"""

    client = api_client(create_app(FakeRuntime(), api_token="t", task_root=tmp_path / "tasks",
                                   rate_limit=10000))
    headers = {"Authorization": "Bearer t"}

    created = client.post("/v1/tasks",
                          json={"command": "python -c \"print('hello_world')\""},
                          headers=headers)
    assert created.status_code == 201
    task_id = created.json()["task"]["id"]
    _approve_task_command(client, headers, task_id)

    record = created.json()["task"]
    for _ in range(100):
        record = client.get(f"/v1/tasks/{task_id}", headers=headers).json()["task"]
        if record["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert record["status"] == "succeeded"
    assert record["exit_code"] == 0

    logs = client.get(f"/v1/tasks/{task_id}/logs", headers=headers).text
    assert "hello_world" in logs

    listing = client.get("/v1/tasks", headers=headers).json()["tasks"]
    assert any(t["id"] == task_id for t in listing)


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_task_stop_and_not_found(tmp_path, api_client):

    client = api_client(create_app(FakeRuntime(), api_token="t", task_root=tmp_path / "tasks",
                                   rate_limit=10000))
    headers = {"Authorization": "Bearer t"}

    assert client.get("/v1/tasks/missing", headers=headers).status_code == 404
    assert client.post("/v1/tasks/missing/stop", headers=headers).status_code == 404

    created = client.post(
        "/v1/tasks",
        json={"command": 'python -c "import time; time.sleep(30)"'},
        headers=headers,
    ).json()["task"]
    task_id = created["id"]
    _approve_task_command(client, headers, task_id)
    for _ in range(100):
        if client.get(f"/v1/tasks/{task_id}", headers=headers).json()["task"]["status"] == "running":
            break
        time.sleep(0.05)

    stopped = client.post(f"/v1/tasks/{task_id}/stop", headers=headers)
    assert stopped.status_code == 200
    assert stopped.json()["task"]["status"] == "cancelled"
    # 已结束的任务再 stop → 409
    assert client.post(f"/v1/tasks/{task_id}/stop", headers=headers).status_code == 409


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_task_rejects_empty_command(tmp_path, api_client):

    client = api_client(create_app(FakeRuntime(), api_token="t", task_root=tmp_path / "tasks"))
    response = client.post("/v1/tasks", json={"command": "   "},
                           headers={"Authorization": "Bearer t"})
    assert response.status_code == 400


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_stream_does_not_skip_event_at_replay_boundary(tmp_path, monkeypatch, api_client):
    runtime = _stream_runtime(tmp_path)
    runtime.create_session('boundary')
    store = runtime.open_session('boundary')
    tail = store.tail_events
    def snapshot_then_append(offset=0, **kwargs):
        snapshot = tail(offset, **kwargs)
        if kwargs.get('strict'):
            store.append_message({'role': 'user', 'content': 'boundary-marker'})
        return snapshot
    monkeypatch.setattr(store, 'tail_events', snapshot_then_append)
    monkeypatch.setattr(runtime, 'open_session', lambda sid: store)
    client = api_client(create_app(runtime, api_token='t', task_root=tmp_path / 'tasks'))
    response = client.get('/v1/sessions/boundary/stream',
                          params={'poll': 0.05, 'follow_seconds': 0.1},
                          headers={'Authorization': 'Bearer t'})
    assert response.text.count('boundary-marker') == 1


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_task_storage_root_does_not_change_workspace(tmp_path, monkeypatch, api_client):
    monkeypatch.setenv('HARNESS_TASK_ROOT', str(tmp_path / 'records'))
    workspace = tmp_path / 'project'
    workspace.mkdir()
    client = api_client(create_app(FakeRuntime(), api_token='t', workspace=workspace,
                                   rate_limit=10000))
    headers = {'Authorization': 'Bearer t'}
    response = client.post('/v1/tasks', json={'command': 'echo workspace'}, headers=headers)
    assert response.status_code == 201
    record = response.json()['task']
    assert record['cwd'] == str(workspace.resolve())
    assert Path(record['log_path']).parent == tmp_path / 'records'
    outside = client.post('/v1/tasks', json={'command': 'echo no', 'cwd': str(tmp_path)},
                          headers=headers)
    assert outside.status_code == 400
    deadline = time.time() + 15   # 时间窗轮询：负载下 echo 子进程可能偶尔 >3s
    while time.time() < deadline:
        state = client.get(f"/v1/tasks/{record['id']}", headers=headers).json()['task']
        if state['status'] in ('succeeded', 'failed'):
            break
        time.sleep(0.05)
    assert state['status'] == 'succeeded'


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_shutdown_stops_tasks_and_releases_log(tmp_path):
    import json

    from fastapi.testclient import TestClient

    log = tmp_path / 'access.log'
    app = create_app(FakeRuntime(), api_token='t', task_root=tmp_path / 'tasks',
                     access_log_path=str(log))
    with TestClient(app) as client:
        record = client.post('/v1/tasks',
                             json={'command': 'python -c "import time; time.sleep(30)"'},
                             headers={'Authorization': 'Bearer t'}).json()['task']
        _approve_task_command(client, {'Authorization': 'Bearer t'}, record['id'])
        for _ in range(100):
            data = json.loads((tmp_path / 'tasks' / f"{record['id']}.json").read_text())
            if data['status'] == 'running':
                break
            time.sleep(0.01)
        assert data['status'] == 'running'
    saved = json.loads((tmp_path / 'tasks' / f"{record['id']}.json").read_text())
    assert saved['status'] == 'cancelled'
    assert saved['exit_code'] is not None
    # Windows refuses this rename if a FileHandler still owns the file.
    log.rename(tmp_path / 'released.log')


# ---------- 执行档位端点（/sandbox 的服务端一侧） ----------

@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_policy_endpoint_reads_switches_and_rejects_bad_tiers(tmp_path, api_client):
    from exec_policy import load_policy

    client = api_client(create_app(FakeRuntime(), api_token='t',
                                   task_root=tmp_path / 'tasks', workspace=tmp_path,
                                   rate_limit=10000))
    headers = {'Authorization': 'Bearer t'}
    default = {'sandbox_mode': 'workspace-write', 'approval_policy': 'on-request'}
    assert client.get('/v1/policy', headers=headers).json() == {'policy': default}

    switched = client.post('/v1/policy', json={'sandbox_mode': 'read-only'},
                           headers=headers)
    assert switched.status_code == 200
    assert switched.json()['policy'] == {'sandbox_mode': 'read-only',
                                         'approval_policy': 'on-request'}
    assert load_policy().sandbox_mode == 'read-only', '服务端进程需立即生效'

    bad = client.post('/v1/policy', json={'approval_policy': 'ask-me'}, headers=headers)
    assert bad.status_code == 400
    assert 'on-request' in bad.json()['detail']      # 400 里带可选值清单
    assert load_policy().approval_policy == 'on-request'


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_http_task_is_gated_by_the_policy_tier(tmp_path, api_client, monkeypatch):
    """POST /v1/tasks 不经过 PreToolUse hook：runner 在 Popen 前自己判（第四条路径）。"""
    monkeypatch.setattr('hooks.GATE.timeout', 0.2)   # 无人决议 → 超时拒绝

    client = api_client(create_app(FakeRuntime(), api_token='t',
                                   task_root=tmp_path / 'tasks', workspace=tmp_path,
                                   rate_limit=10000))
    headers = {'Authorization': 'Bearer t'}
    client.post('/v1/policy', json={'sandbox_mode': 'read-only'}, headers=headers)

    created = client.post('/v1/tasks',
                          json={'command': 'python -c "print(1)"'}, headers=headers)
    assert created.status_code == 201
    record = created.json()['task']
    assert record['status'] == 'queued', '创建请求不应等待审批'

    for _ in range(100):
        record = client.get(f"/v1/tasks/{record['id']}", headers=headers).json()['task']
        if record['status'] not in ('queued', 'running'):
            break
        time.sleep(0.05)
    assert record['status'] == 'cancelled'
    # 超时不是「用户拒绝」：任务详情要保留原因，和审批历史里的 timeout 对齐
    assert record['detail'] == \
        'Permission denied: approval timed out after 0.2s with no response'
    assert client.get(f"/v1/tasks/{record['id']}/logs", headers=headers).text == ''


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_message_returns_409_when_turn_is_busy(api_client):
    from turn_coordinator import TurnBusy

    class BusyRuntime(FakeRuntime):
        def send(self, session_id, content, on_delta=None):
            raise TurnBusy('另一个入口正在执行回合')

    runtime = BusyRuntime()
    client = api_client(create_app(runtime, api_token='t'))
    headers = {'Authorization': 'Bearer t'}
    assert client.post('/v1/sessions', json={'id': 'busy'},
                       headers=headers).status_code == 201
    response = client.post('/v1/sessions/busy/messages',
                           json={'content': 'hello'}, headers=headers)
    assert response.status_code == 409
    assert '执行回合' in response.json()['detail']


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_stream_reports_turn_busy_as_error_event(api_client):
    from turn_coordinator import TurnBusy

    class BusyRuntime(FakeRuntime):
        def send(self, session_id, content, on_delta=None):
            raise TurnBusy('另一个入口正在执行回合')

    runtime = BusyRuntime()
    client = api_client(create_app(runtime, api_token='t'))
    headers = {'Authorization': 'Bearer t'}
    assert client.post('/v1/sessions', json={'id': 'busy-stream'},
                       headers=headers).status_code == 201
    response = client.post('/v1/sessions/busy-stream/messages/stream',
                           json={'content': 'hello'}, headers=headers)
    assert response.status_code == 200
    assert 'event: error' in response.text
    assert 'turn_busy' in response.text


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_lifespan_starts_and_stops_scheduler(monkeypatch, tmp_path):
    import harness as harness_module
    import harness_api

    events = []

    class FakeService:
        def __init__(self, run_turn):
            self.run_turn = run_turn

        def start(self):
            events.append('start')
            return True

        def stop(self, timeout=None):
            events.append('stop')
            return True

    monkeypatch.setattr(harness_api, 'SchedulerService', FakeService)
    monkeypatch.setattr(harness_module, 'initialize_mcp_tools', lambda: None)
    monkeypatch.setattr(harness_module, 'shutdown_mcp', lambda: None)
    monkeypatch.setattr(harness_module.client, 'warmup', lambda: None)
    monkeypatch.setenv('HARNESS_SCHEDULER', '1')
    from fastapi.testclient import TestClient

    app = create_app(api_token='t', task_root=tmp_path / 'tasks')
    with TestClient(app):
        assert events == ['start']
    assert events == ['start', 'stop']


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_lifespan_skips_scheduler_when_disabled(monkeypatch, tmp_path):
    import harness as harness_module
    import harness_api

    events = []

    class FakeService:
        def __init__(self, run_turn):
            events.append('init')

        def start(self):
            events.append('start')

        def stop(self, timeout=None):
            events.append('stop')

    monkeypatch.setattr(harness_api, 'SchedulerService', FakeService)
    monkeypatch.setattr(harness_module, 'initialize_mcp_tools', lambda: None)
    monkeypatch.setattr(harness_module, 'shutdown_mcp', lambda: None)
    monkeypatch.setattr(harness_module.client, 'warmup', lambda: None)
    monkeypatch.setenv('HARNESS_SCHEDULER', '0')
    from fastapi.testclient import TestClient

    app = create_app(api_token='t', task_root=tmp_path / 'tasks')
    with TestClient(app):
        pass
    assert events == []


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_injected_runtime_does_not_start_scheduler(monkeypatch, tmp_path):
    import harness_api

    events = []

    class FakeService:
        def __init__(self, run_turn):
            events.append('init')

        def start(self):
            events.append('start')

    monkeypatch.setattr(harness_api, 'SchedulerService', FakeService)
    from fastapi.testclient import TestClient

    app = create_app(FakeRuntime(), api_token='t', task_root=tmp_path / 'tasks')
    with TestClient(app):
        pass
    assert events == []


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_rename_and_fork_return_409_when_session_is_busy(api_client):
    from session_locks import SessionBusy

    class BusyRuntime(FakeRuntime):
        def rename_session(self, session_id, title):
            raise SessionBusy('会话正在执行任务')

        def fork_session(self, session_id, upto_seq=None):
            raise SessionBusy('会话正在执行任务')

    runtime = BusyRuntime()
    client = api_client(create_app(runtime, api_token='t'))
    headers = {'Authorization': 'Bearer t'}
    assert client.post('/v1/sessions', json={'id': 'busy-meta'},
                       headers=headers).status_code == 201
    response = client.post('/v1/sessions/busy-meta/title',
                           json={'title': 'x'}, headers=headers)
    assert response.status_code == 409
    response = client.post('/v1/sessions/busy-meta/fork',
                           json={}, headers=headers)
    assert response.status_code == 409


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_steer_and_interrupt_routes(api_client):
    class TurnRuntime(FakeRuntime):
        def steer(self, session_id, content, *, request_id=None):
            if session_id not in self.sessions:
                raise FileNotFoundError(session_id)
            return {'status': 'queued', 'session_id': session_id,
                    'turn_id': 't-1', 'source': 'api'}

        def interrupt(self, session_id):
            return ({'status': 'cancelling', 'session_id': session_id,
                     'turn_id': 't-1'} if session_id == 'busy-turn'
                    else {'status': 'idle', 'session_id': session_id})

    runtime = TurnRuntime()
    client = api_client(create_app(runtime, api_token='t'))
    headers = {'Authorization': 'Bearer t'}
    assert client.post('/v1/sessions', json={'id': 'busy-turn'},
                       headers=headers).status_code == 201

    response = client.post('/v1/sessions/busy-turn/steer',
                           json={'content': 'also check docs'}, headers=headers)
    assert response.status_code == 200
    assert response.json()['status'] == 'queued'
    assert response.json()['turn_id'] == 't-1'

    response = client.post('/v1/sessions/busy-turn/interrupt', headers=headers)
    assert response.status_code == 200
    assert response.json()['status'] == 'cancelling'

    response = client.post('/v1/sessions/busy-turn/interrupt',
                           headers={'Authorization': 'Bearer t'})
    assert response.status_code == 200

    missing = client.post('/v1/sessions/missing/steer',
                          json={'content': 'x'}, headers=headers)
    assert missing.status_code == 404


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_steer_returns_409_when_turn_cannot_be_steered(api_client):
    class Unsteerable(FakeRuntime):
        def steer(self, session_id, content, *, request_id=None):
            raise RuntimeError('当前回合不支持 steer（由其他入口执行）')

    runtime = Unsteerable()
    client = api_client(create_app(runtime, api_token='t'))
    headers = {'Authorization': 'Bearer t'}
    assert client.post('/v1/sessions', json={'id': 's1'},
                       headers=headers).status_code == 201
    response = client.post('/v1/sessions/s1/steer',
                           json={'content': 'x'}, headers=headers)
    assert response.status_code == 409


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_stream_reports_cancelled_turn(api_client):
    from agent_runtime import TurnCancelled

    class CancelRuntime(FakeRuntime):
        def send(self, session_id, content, on_delta=None):
            raise TurnCancelled('回合已取消')

    runtime = CancelRuntime()
    client = api_client(create_app(runtime, api_token='t'))
    headers = {'Authorization': 'Bearer t'}
    assert client.post('/v1/sessions', json={'id': 'c1'},
                       headers=headers).status_code == 201
    response = client.post('/v1/sessions/c1/messages/stream',
                           json={'content': 'x'}, headers=headers)
    assert response.status_code == 200
    assert 'event: cancelled' in response.text


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_cron_crud(api_client, tmp_path, monkeypatch):
    import cron_scheduler

    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    client = api_client(create_app(FakeRuntime(), api_token='t',
                                   task_root=tmp_path / 'tasks'))
    headers = {'Authorization': 'Bearer t'}

    created = client.post('/v1/crons', json={
        'cron': '0 9 * * *', 'prompt': 'daily report', 'id': 'heartbeat'},
        headers=headers)
    assert created.status_code == 201
    job = created.json()
    assert job['id'] == 'heartbeat' and job['recurring'] is True

    duplicate = client.post('/v1/crons', json={
        'cron': '0 10 * * *', 'prompt': 'x', 'id': 'heartbeat'},
        headers=headers)
    assert duplicate.status_code == 409

    invalid = client.post('/v1/crons', json={'cron': '* * *', 'prompt': 'x'},
                          headers=headers)
    assert invalid.status_code == 400

    listed = client.get('/v1/crons', headers=headers).json()['jobs']
    assert [item['id'] for item in listed] == ['heartbeat']

    assert client.delete('/v1/crons/heartbeat', headers=headers).status_code == 200
    assert client.delete('/v1/crons/heartbeat', headers=headers).status_code == 404
    assert client.get('/v1/crons', headers=headers).json()['jobs'] == []
