import importlib.util

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
    client = TestClient(create_app(runtime))

    created = client.post("/v1/sessions", json={"id": "api-test"})
    assert created.status_code == 201
    assert created.json()["id"] == "api-test"

    response = client.post(
        "/v1/sessions/api-test/messages",
        json={"content": "hello"},
    )
    assert response.status_code == 200
    assert response.json() == {"session_id": "api-test", "content": "done"}

    history = client.get("/v1/sessions/api-test")
    assert history.status_code == 200
    assert len(history.json()["history"]) == 2


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_maps_missing_session_to_404():
    from fastapi.testclient import TestClient

    client = TestClient(create_app(FakeRuntime()))
    response = client.get("/v1/sessions/missing")
    assert response.status_code == 404


@pytest.mark.skipif(not fastapi_available, reason="FastAPI is an optional dependency")
def test_api_maps_duplicate_session_to_409():
    from fastapi.testclient import TestClient

    runtime = FakeRuntime()
    client = TestClient(create_app(runtime))
    assert client.post("/v1/sessions", json={"id": "same"}).status_code == 201
    response = client.post("/v1/sessions", json={"id": "same"})
    assert response.status_code == 409
