"""GET /v1/system/state 契约：鉴权与空闲快照透传。"""
import importlib.util
from contextlib import ExitStack

import pytest

from harness_api import create_app

fastapi_available = importlib.util.find_spec('fastapi') is not None

pytestmark = pytest.mark.skipif(not fastapi_available,
                                reason='FastAPI is an optional dependency')

HEADERS = {'Authorization': 'Bearer t'}


@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient

    with ExitStack() as stack:
        yield lambda app: stack.enter_context(TestClient(app))


def _runtime(tmp_path):
    from agent_runtime import AgentRuntime
    from session_store import SessionEventStore

    def run_turn(messages, session_store=None, **kwargs):
        return 'done'

    return AgentRuntime(run_turn=run_turn,
                        session_store_cls=SessionEventStore,
                        sessions_dir=tmp_path / 'sessions')


def _app(tmp_path):
    return create_app(_runtime(tmp_path), api_token='t',
                      task_root=tmp_path / 'tasks', workspace=tmp_path,
                      rate_limit=10000)


def test_system_state_exposes_idle_snapshot(tmp_path, monkeypatch, api_client):
    import idle

    snapshot = {
        'available': True,
        'idle_seconds': 120,
        'locked': False,
        'checked_at': '2026-10-26T04:00:00Z',
    }
    calls = []
    monkeypatch.setattr(idle, 'state',
                        lambda: calls.append(1) or dict(snapshot))

    client = api_client(_app(tmp_path))
    for _ in range(2):
        response = client.get('/v1/system/state', headers=HEADERS)
        assert response.status_code == 200
        assert response.json() == {'idle': snapshot}
    assert len(calls) == 2


def test_system_state_passes_through_probe_errors(tmp_path, monkeypatch,
                                                  api_client):
    import idle

    snapshot = {'available': False, 'idle_seconds': None, 'locked': None,
                'checked_at': '2026-10-26T04:00:00Z', 'error': 'timeout'}
    monkeypatch.setattr(idle, 'state', lambda: dict(snapshot))

    client = api_client(_app(tmp_path))
    response = client.get('/v1/system/state', headers=HEADERS)
    assert response.json() == {'idle': snapshot}


def test_system_state_requires_bearer_token(tmp_path, api_client):
    client = api_client(_app(tmp_path))
    assert client.get('/v1/system/state').status_code == 401
