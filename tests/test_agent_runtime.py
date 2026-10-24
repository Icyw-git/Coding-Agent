
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


def test_runtime_persists_attachments_and_forwards_turn_options(tmp_path):
    seen = {}
    events = []

    def run_turn(messages, session_store, on_delta=None, on_event=None,
                 housekeeping=True):
        seen['messages'] = list(messages)
        seen['housekeeping'] = housekeeping
        on_event('tool.started', {'name': 'view_image'})
        assistant = {'role': 'assistant', 'content': 'looked'}
        messages.append(assistant)
        session_store.append_message(assistant)
        return 'looked'

    runtime = _runtime(tmp_path, run_turn)
    session_id = runtime.create_session('attach')
    attachment = {'type': 'harness_image', 'path': 'cached.png',
                  'mime': 'image/png', 'name': 'pixel.png'}

    result = runtime.send(session_id, 'what is this?',
                          attachments=[attachment],
                          on_event=lambda kind, payload: events.append((kind, payload)),
                          housekeeping=False)

    assert result.content == 'looked'
    assert seen['housekeeping'] is False
    assert events == [('tool.started', {'name': 'view_image'})]
    attached = seen['messages'][-1]
    assert attached['role'] == 'user'
    assert attached['content'][0]['type'] == 'text'
    assert attached['content'][1] == attachment
    history = runtime.history(session_id)
    assert history[-2]['content'][1]['type'] == 'harness_image'
    assert history[-1] == {'role': 'assistant', 'content': 'looked'}


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


def test_steer_falls_back_to_a_new_turn_when_idle(tmp_path):
    calls = []

    def run_turn(messages, session_store, **kwargs):
        calls.append(list(messages))
        return 'ok'

    runtime = _runtime(tmp_path, run_turn)
    session_id = runtime.create_session()

    result = runtime.steer(session_id, 'later please')

    assert result['status'] == 'sent'
    assert calls and calls[0][-1]['content'] == 'later please'


def test_steer_injects_into_the_active_turn(tmp_path):
    import threading
    import time

    import turn_control

    started = threading.Event()
    received = []

    def run_turn(messages, session_store, steer_provider=None, **kwargs):
        started.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            items = steer_provider() if steer_provider else []
            if items:
                received.extend(items)
                return 'ok'
            time.sleep(0.01)
        raise AssertionError('steer never arrived')

    runtime = _runtime(tmp_path, run_turn)
    session_id = runtime.create_session()

    result = []
    thread = threading.Thread(target=lambda: result.append(runtime.send(session_id, 'go')))
    thread.start()
    try:
        assert started.wait(5), 'turn did not start'
        status = runtime.steer(session_id, 'also check docs')
        assert status['status'] == 'queued'
        assert status['turn_id']
    finally:
        thread.join(10)
    assert not thread.is_alive()
    assert received == ['also check docs']
    assert result and result[0].content == 'ok'
    assert turn_control.first_token(session_id) is None


def test_interrupt_cancels_the_active_turn(tmp_path):
    import threading
    import time

    import turn_control
    from agent_runtime import TurnCancelled

    started = threading.Event()

    def run_turn(messages, session_store, **kwargs):
        started.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if turn_control.cancel_requested():
                raise KeyboardInterrupt
            time.sleep(0.01)
        raise AssertionError('interrupt never arrived')

    runtime = _runtime(tmp_path, run_turn)
    session_id = runtime.create_session()

    errors = []
    thread = threading.Thread(target=lambda: errors.append(_send(runtime, session_id)))
    thread.start()
    try:
        assert started.wait(5), 'turn did not start'
        status = runtime.interrupt(session_id)
        assert status['status'] == 'cancelling'
        assert status['turn_id']
    finally:
        thread.join(10)
    assert not thread.is_alive()
    assert errors and isinstance(errors[0], TurnCancelled)
    assert runtime.interrupt(session_id)['status'] == 'idle'


def _send(runtime, session_id):
    try:
        runtime.send(session_id, 'long turn')
    except BaseException as exc:
        return exc
    raise AssertionError('send should have been cancelled')


def test_steer_request_id_is_idempotent(tmp_path):
    import threading
    import time

    started = threading.Event()
    received = []

    def run_turn(messages, session_store, steer_provider=None, **kwargs):
        started.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            items = steer_provider() if steer_provider else []
            if items:
                received.extend(items)
                return 'ok'
            time.sleep(0.01)
        raise AssertionError('steer never arrived')

    runtime = _runtime(tmp_path, run_turn)
    session_id = runtime.create_session()
    result = []
    thread = threading.Thread(target=lambda: result.append(runtime.send(session_id, 'go')))
    thread.start()
    steered = []

    def _steer_twice():
        steered.append(runtime.steer(session_id, 'one', request_id='req-1'))
        steered.append(runtime.steer(session_id, 'one', request_id='req-1'))

    try:
        assert started.wait(5), 'first turn did not start'
        steer_thread = threading.Thread(target=_steer_twice)
        steer_thread.start()
        steer_thread.join(10)
        assert not steer_thread.is_alive(), 'steer 死锁（锁不可重入）'
    finally:
        thread.join(10)
    assert steered[0]['status'] == 'queued'
    assert steered[1]['status'] == 'queued' and steered[1]['duplicate'] is True
    assert received == ['one']


def test_steer_falls_back_to_send_when_turn_is_ending(tmp_path):
    import turn_control

    calls = []

    def run_turn(messages, session_store, **kwargs):
        calls.append(list(messages))
        return 'ok'

    runtime = _runtime(tmp_path, run_turn)
    session_id = runtime.create_session()

    # 模拟“回合已有 token、注入队列已移除”的收尾窗口。
    token = turn_control.begin_turn(session_id=session_id, source='tui')
    try:
        result = runtime.steer(session_id, 'late message', request_id='late-1')
    finally:
        turn_control.end_turn(token)

    assert result['status'] == 'sent'
    assert result.get('reason') == 'turn_ending'
    assert calls and calls[-1][-1]['content'] == 'late message'
    assert runtime.steer(session_id, 'late message',
                         request_id='late-1')['duplicate'] is True
