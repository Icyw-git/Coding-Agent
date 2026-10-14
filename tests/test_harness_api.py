import importlib.util
import time

import pytest

from harness_api import create_app


fastapi_available = importlib.util.find_spec("fastapi") is not None


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

    def send(self, session_id, content):
        if session_id not in self.sessions:
            raise FileNotFoundError(session_id)
        self.sessions[session_id].extend([
            {"role": "user", "content": content},
            {"role": "assistant", "content": "done"},
        ])
        return Result(session_id, "done")


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_routes_use_runtime(monkeypatch):
    from fastapi.testclient import TestClient

    runtime = FakeRuntime()
    client = TestClient(create_app(runtime, api_token="test-token"))

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
def test_api_maps_missing_session_to_404():
    from fastapi.testclient import TestClient

    client = TestClient(create_app(FakeRuntime(), api_token="test-token"))
    response = client.get("/v1/sessions/missing", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 404


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_maps_duplicate_session_to_409():
    from fastapi.testclient import TestClient

    runtime = FakeRuntime()
    client = TestClient(create_app(runtime, api_token="test-token"))
    headers = {"Authorization": "Bearer test-token"}
    assert client.post("/v1/sessions", json={"id": "same"}, headers=headers).status_code == 201
    response = client.post("/v1/sessions", json={"id": "same"}, headers=headers)
    assert response.status_code == 409


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_requires_token_and_limits_requests(tmp_path):
    from fastapi.testclient import TestClient

    runtime = FakeRuntime()
    client = TestClient(create_app(runtime, api_token="secret", rate_limit=1,
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
def test_api_lists_sessions():
    from fastapi.testclient import TestClient

    client = TestClient(create_app(FakeRuntime(), api_token="t"))
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
def test_stream_replays_session_events(tmp_path):
    """SSE 端点回放既有事件流（含非 message 事件）；once=1 回放即关闭。"""
    from fastapi.testclient import TestClient

    runtime = _stream_runtime(tmp_path)
    client = TestClient(create_app(runtime, api_token="t"))
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
def test_stream_follow_picks_up_new_events(tmp_path):
    """follow 阶段能增量拾取连接之后新写入的事件（实时监控的核心路径）。"""
    import threading

    from fastapi.testclient import TestClient

    runtime = _stream_runtime(tmp_path)
    client = TestClient(create_app(runtime, api_token="t"))
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
def test_stream_maps_missing_session_to_404(tmp_path):
    from fastapi.testclient import TestClient

    client = TestClient(create_app(_stream_runtime(tmp_path), api_token="t"))
    response = client.get("/v1/sessions/nope/stream",
                          headers={"Authorization": "Bearer t"})
    assert response.status_code == 404


# ---------- 任务端点 ----------

@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_task_end_to_end_flow(tmp_path):
    """网关核心链路：创建任务 → 轮询状态 → 拉取日志 → 列表。"""
    from fastapi.testclient import TestClient

    client = TestClient(create_app(FakeRuntime(), api_token="t", task_root=tmp_path / "tasks"))
    headers = {"Authorization": "Bearer t"}

    created = client.post("/v1/tasks",
                          json={"command": "python -c \"print('hello_world')\""},
                          headers=headers)
    assert created.status_code == 201
    task_id = created.json()["task"]["id"]

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
def test_task_stop_and_not_found(tmp_path):
    from fastapi.testclient import TestClient

    client = TestClient(create_app(FakeRuntime(), api_token="t", task_root=tmp_path / "tasks"))
    headers = {"Authorization": "Bearer t"}

    assert client.get("/v1/tasks/missing", headers=headers).status_code == 404
    assert client.post("/v1/tasks/missing/stop", headers=headers).status_code == 404

    created = client.post(
        "/v1/tasks",
        json={"command": 'python -c "import time; time.sleep(30)"'},
        headers=headers,
    ).json()["task"]
    task_id = created["id"]
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
def test_task_rejects_empty_command(tmp_path):
    from fastapi.testclient import TestClient

    client = TestClient(create_app(FakeRuntime(), api_token="t", task_root=tmp_path / "tasks"))
    response = client.post("/v1/tasks", json={"command": "   "},
                           headers={"Authorization": "Bearer t"})
    assert response.status_code == 400
