
import pytest

import workspaces
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


def _workspace_runtime(tmp_path, run_turn=lambda *_a, **_kw: 'unused'):
    return AgentRuntime(
        run_turn=run_turn,
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path / 'sessions',
        workspace=tmp_path / 'primary',
        workspace_file=tmp_path / 'workspaces.json',
    )


def test_runtime_authorizes_configured_roots_only(tmp_path):
    primary = tmp_path / 'primary'
    extra = tmp_path / 'extra'
    outside = tmp_path / 'outside'
    for directory in (primary, extra, outside):
        directory.mkdir()
    runtime = _workspace_runtime(tmp_path)
    runtime.configure_workspace_roots([primary, extra])

    session_id = runtime.create_session('in-extra', workspace=str(extra))
    assert runtime.get_workspace(session_id) == str(extra)
    with pytest.raises(ValueError, match='outside the allowed root'):
        runtime.create_session('outside', workspace=str(outside))

    assert [entry['path'] for entry in
            workspaces.list_workspaces(file=runtime.workspace_file)] \
        == [str(extra)]


def test_runtime_lists_current_registered_and_recent_workspaces(tmp_path):
    primary = tmp_path / 'primary'
    extra = tmp_path / 'extra'
    for directory in (primary, extra):
        directory.mkdir()
    runtime = _workspace_runtime(tmp_path)
    runtime.configure_workspace_roots([primary, extra])
    runtime.create_session('at-extra', workspace=str(extra))

    listing = runtime.list_workspaces()

    assert listing['current'] == str(primary)
    assert listing['roots'] == [str(primary), str(extra)]
    assert listing['workspaces'][0]['path'] == str(primary)
    assert listing['workspaces'][0]['current'] is True
    extra_row = next(row for row in listing['workspaces']
                     if row['path'] == str(extra))
    assert extra_row['sessions'] == 1
    assert extra_row['allowed'] is True
    assert extra_row['saved'] is True


def test_runtime_add_and_remove_workspace_respects_roots(tmp_path):
    primary = tmp_path / 'primary'
    extra = tmp_path / 'extra'
    outside = tmp_path / 'outside'
    for directory in (primary, extra, outside):
        directory.mkdir()
    runtime = _workspace_runtime(tmp_path)
    runtime.configure_workspace_roots([primary, extra])

    entry = runtime.add_workspace(str(extra), name='Extra Project')
    assert entry['name'] == 'Extra Project'
    row = next(row for row in runtime.list_workspaces()['workspaces']
               if row['path'] == str(extra))
    assert row['name'] == 'Extra Project'
    assert row['saved'] is True and row['allowed'] is True

    assert runtime.remove_workspace(entry['id'])['path'] == str(extra)
    assert all(row['path'] != str(extra)
               for row in runtime.list_workspaces()['workspaces'])
    with pytest.raises(ValueError, match='outside the allowed root'):
        runtime.add_workspace(str(outside))


def test_registered_workspace_does_not_widen_authorization(tmp_path):
    primary = tmp_path / 'primary'
    outside = tmp_path / 'outside'
    for directory in (primary, outside):
        directory.mkdir()
    runtime = _workspace_runtime(tmp_path)
    runtime.configure_workspace_roots([primary])
    workspaces.add_workspace(outside, name='Escaped',
                             file=runtime.workspace_file)

    row = next(row for row in runtime.list_workspaces()['workspaces']
               if row['path'] == str(outside))
    assert row['saved'] is True
    assert row['allowed'] is False
    with pytest.raises(ValueError, match='outside the allowed root'):
        runtime.create_session(workspace=str(outside))
