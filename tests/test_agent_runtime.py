from pathlib import Path

import pytest

from agent_runtime import AgentRuntime, SessionAlreadyExistsError
from session_store import SessionEventStore


def _runtime(tmp_path, run_turn):
    return AgentRuntime(
        run_turn=run_turn,
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path,
    )


def test_runtime_creates_session_and_runs_turn(tmp_path):
    calls = []

    def run_turn(messages, session_store):
        calls.append((messages[:], session_store.session_id))
        assistant = {"role": "assistant", "content": "done"}
        messages.append(assistant)
        session_store.append_message(assistant)
        return "done"

    runtime = _runtime(tmp_path, run_turn)
    session_id = runtime.create_session("session-test")

    result = runtime.send(session_id, "inspect the project")

    assert result.session_id == "session-test"
    assert result.content == "done"
    assert calls[0][0] == [{"role": "user", "content": "inspect the project"}]
    assert runtime.history(session_id)[-1] == {"role": "assistant", "content": "done"}


def test_runtime_rejects_empty_messages(tmp_path):
    runtime = _runtime(tmp_path, lambda *_args, **_kwargs: "unused")
    session_id = runtime.create_session()

    with pytest.raises(ValueError, match="non-empty"):
        runtime.send(session_id, "  ")


def test_runtime_rejects_invalid_explicit_session_id(tmp_path):
    runtime = _runtime(tmp_path, lambda *_args, **_kwargs: "unused")

    with pytest.raises(ValueError, match="Invalid session ID"):
        runtime.create_session("..\\outside")


def test_runtime_rejects_duplicate_session_id(tmp_path):
    runtime = _runtime(tmp_path, lambda *_args, **_kwargs: "unused")
    runtime.create_session("same")

    with pytest.raises(SessionAlreadyExistsError, match="already exists"):
        runtime.create_session("same")


def test_runtime_uses_resolver_for_session_ids(tmp_path):
    store = SessionEventStore(tmp_path, session_id="known")
    seen = []

    def resolve(session_id):
        seen.append(session_id)
        return store.path

    runtime = AgentRuntime(
        run_turn=lambda messages, session_store: "ok",
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path,
        resolve_session_path=resolve,
    )

    assert runtime.history("known") == []
    assert seen == ["known"]


def test_runtime_forwards_stream_delta_callback(tmp_path):
    received = []

    def run_turn(messages, session_store, on_delta=None):
        on_delta('hello')
        assistant = {'role': 'assistant', 'content': 'hello'}
        messages.append(assistant)
        session_store.append_message(assistant)
        return 'hello'

    runtime = _runtime(tmp_path, run_turn)
    session_id = runtime.create_session('stream')
    result = runtime.send(session_id, 'go', on_delta=received.append)

    assert result.content == 'hello'
    assert received == ['hello']
