import threading

import pytest

from feishu_gateway import FeishuGateway
from test_checkpoints import _loop_env
from test_context_manage import make_text_response, make_tool_response
from test_feishu_gateway import FakeTransport


def test_concurrent_turns_capture_files_in_their_own_sessions(tmp_path, monkeypatch):
    harness, workspace = _loop_env(tmp_path, monkeypatch)
    (workspace / 'a.txt').write_text('original a', encoding='utf-8')
    (workspace / 'b.txt').write_text('original b', encoding='utf-8')
    a_requested, b_requested, a_written = (threading.Event() for _ in range(3))
    original_write = harness.tool_registry['write']

    def write(path, content, **kwargs):
        result = original_write(path, content, **kwargs)
        if path == 'a.txt':
            a_written.set()
        return result

    def create(**kwargs):
        messages = kwargs['messages']
        if messages[-1]['role'] == 'tool':
            return make_text_response('done')
        name = next(m['content'] for m in messages if m['role'] == 'user')
        if name == 'a':
            a_requested.set()
            assert b_requested.wait(3)
        else:
            b_requested.set()
            assert a_written.wait(3)
        return make_tool_response([(f'write-{name}', 'write',
                                    f'{{"path":"{name}.txt","content":"new {name}"}}')])

    monkeypatch.setitem(harness.tool_registry, 'write', write)
    monkeypatch.setattr(harness.client.chat.completions, 'create', create)
    stores = {name: harness.SessionEventStore(harness.SESSIONS_DIR, session_id=name)
              for name in ('a', 'b')}
    failures = []

    def turn(name):
        try:
            if name == 'b':
                assert a_requested.wait(3)
            user = {'role': 'user', 'content': name}
            stores[name].append_message(user)
            assert harness.agent_loop([user], session_store=stores[name]) == 'done'
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=turn, args=(name,)) for name in ('a', 'b')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    harness.wait_for_housekeeping()
    assert not any(thread.is_alive() for thread in threads)
    assert not failures
    for name, store in stores.items():
        manager = harness.get_checkpoint_manager(store)
        changed = [row for row in manager.list() if row['files']]
        assert len(changed) == 1
        assert changed[0]['files'] == [f'{name}.txt']


@pytest.mark.parametrize('approval', ['y', 'n', 'Ｙ２', '/approve y', '／approve second n'])
def test_approval_unblocks_busy_single_worker(tmp_path, approval):
    started, resolved, release = (threading.Event() for _ in range(3))

    class API:
        def resolve_permission(self, request_id, allow):
            resolved.set()
            release.set()
            return {'status': 'approved' if allow else 'denied', 'tool': 'bash'}

    gateway = FeishuGateway(API(), transport=FakeTransport(),
                            bindings_path=tmp_path / 'bindings.json')
    gateway.bindings['chat'] = 'session'
    gateway._chat_pending['chat'] = ['first', 'second']

    def long_turn(*args):
        started.set()
        assert release.wait(3)

    gateway._send_agent_reply = long_turn
    gateway.start_dispatcher(workers=1)
    try:
        gateway.dispatch('chat', 'run a task')
        assert started.wait(1)
        gateway.dispatch('chat', approval)
        assert resolved.wait(1), 'approval is waiting behind the blocked agent turn'
    finally:
        release.set()
        gateway.stop()


@pytest.mark.parametrize('fail', [False, True])
def test_checkpoint_context_restored_after_turn(tmp_path, monkeypatch, fail):
    harness, _ = _loop_env(tmp_path, monkeypatch)
    outer = object()
    token = harness._checkpoint_manager.set(outer)
    store = harness.SessionEventStore(harness.SESSIONS_DIR)

    def create(**kwargs):
        assert harness.active_checkpoint_manager().session_id == store.session_id
        if fail:
            raise RuntimeError('model unavailable')
        return make_text_response('done')

    monkeypatch.setattr(harness.client.chat.completions, 'create', create)
    try:
        if fail:
            with pytest.raises(RuntimeError, match='model unavailable'):
                harness.agent_loop([], session_store=store)
        else:
            assert harness.agent_loop([], session_store=store) == 'done'
        assert harness.active_checkpoint_manager() is outer
        manager = harness.CheckpointManager(harness.CHECKPOINT_DIR, harness.WORKDIR,
                                             store.session_id)
        assert all(row['status'] == 'sealed' for row in manager.list())
    finally:
        harness._checkpoint_manager.reset(token)
        harness.wait_for_housekeeping()


def test_stale_approval_shortcut_never_starts_agent(tmp_path):
    transport = FakeTransport()
    gateway = FeishuGateway(object(), transport=transport,
                            bindings_path=tmp_path / 'bindings.json')
    gateway._chat_pending['chat'] = ['first']
    gateway.dispatch('chat', 'y')
    gateway._chat_pending.clear()
    gateway.start_dispatcher(workers=1)
    try:
        gateway._approval_events.join()
        assert '没有待审批请求' in transport.last()
    finally:
        gateway.stop()


def test_approval_worker_rejects_unauthorized_sender(tmp_path):
    transport = FakeTransport()
    gateway = FeishuGateway(object(), transport=transport, allowed_users=['owner'],
                            bindings_path=tmp_path / 'bindings.json')
    gateway._chat_pending['chat'] = ['first']
    calls = []
    gateway.on_message = lambda *args, **kwargs: calls.append(args)
    gateway.dispatch('chat', 'y', sender_id='stranger')
    gateway.dispatch('chat', '/approve first y', sender_id='stranger')
    gateway.start_dispatcher(workers=1)
    try:
        gateway._approval_events.join()
        assert calls == []
        assert transport.sent == []
        assert gateway._chat_pending['chat'] == ['first']
    finally:
        gateway.stop()


def test_shortcut_without_pending_is_normal_message(tmp_path):
    gateway = FeishuGateway(object(), transport=FakeTransport(),
                            bindings_path=tmp_path / 'bindings.json')
    received = []
    gateway.bindings['chat'] = 'session'
    gateway._send_agent_reply = lambda *args: received.append(args)
    gateway.dispatch('chat', 'y')
    gateway.start_dispatcher(workers=1)
    try:
        gateway._events.join()
        assert received == [('chat', 'session', 'y')]
    finally:
        gateway.stop()
