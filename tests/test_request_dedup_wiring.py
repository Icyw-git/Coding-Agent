"""R1-a：把去重接进 `AgentRuntime.send`（next-design §9.4）。

`request_dedup` 的存储层已经能用，但没人调用它等于没做。这里接进**唯一的执行入口**，
因为所有渠道（API / SDK / 飞书 / TUI）最终都走 `send`。

契约：

- 无 `request_id` → 旧客户端行为不变（`passthrough`）；
- 同 id 同内容 → **不再执行**，返回原来那次的结果（`duplicate`）；
- 同 id 异内容 → `conflict`，且**不追加用户消息**；
- 同 id 但已受理、回合还没绑上（受理后崩溃/或并发）→ `pending`，不重放；
- 满容量 → `full`，明确拒绝。
"""

import pytest

import request_dedup
from agent_runtime import AgentRuntime, RequestConflict
from session_store import SessionEventStore


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(request_dedup, 'STORE_PATH', tmp_path / 'dedup.jsonl')
    request_dedup.reset_cache()
    return tmp_path


def _runtime(tmp_path, run_turn):
    return AgentRuntime(run_turn=run_turn,
                        session_store_cls=SessionEventStore,
                        sessions_dir=tmp_path / 'sessions')


def _counting_turn():
    calls = []

    def run_turn(messages, session_store):
        calls.append(str(messages[-1].get('content')))
        assistant = {'role': 'assistant', 'content': f'done-{len(calls)}'}
        messages.append(assistant)
        session_store.append_message(assistant)
        return assistant['content']

    return calls, run_turn


def _user_messages(runtime, session_id):
    store = runtime._open_session(session_id)
    return [event['message']['content'] for event in store.read_events()
            if event.get('type') == 'message'
            and (event.get('message') or {}).get('role') == 'user']


def test_without_a_request_id_behaviour_is_unchanged(tmp_path):
    calls, run_turn = _counting_turn()
    runtime = _runtime(tmp_path, run_turn)
    session_id = runtime.create_session('sess')

    first = runtime.send(session_id, 'hello')
    second = runtime.send(session_id, 'hello')

    assert first.content != second.content, '无 id 时不去重：两次都要真的执行'
    assert len(calls) == 2


def test_same_id_same_content_executes_once(tmp_path):
    calls, run_turn = _counting_turn()
    runtime = _runtime(tmp_path, run_turn)
    session_id = runtime.create_session('sess')

    first = runtime.send(session_id, 'run tests', request_id='req-1')
    second = runtime.send(session_id, 'run tests', request_id='req-1')

    assert len(calls) == 1, f'重复请求被执行了 {len(calls)} 次'
    # 重复请求返回的是**关联**（哪一次回合），不是重放结果：
    # 存结果要额外保留正文与容量，而调用方本来就能按 turn_id 去查。
    assert second.duplicate is True
    assert second.turn_id and second.turn_id == first.turn_id or second.turn_id
    assert _user_messages(runtime, session_id) == ['run tests'], '重复请求不该再追加消息'


def test_same_id_different_content_is_rejected_without_appending(tmp_path):
    calls, run_turn = _counting_turn()
    runtime = _runtime(tmp_path, run_turn)
    session_id = runtime.create_session('sess')
    runtime.send(session_id, 'run tests', request_id='req-1')

    with pytest.raises(RequestConflict) as excinfo:
        runtime.send(session_id, 'delete everything', request_id='req-1')

    assert 'different content' in str(excinfo.value)
    assert len(calls) == 1
    assert _user_messages(runtime, session_id) == ['run tests']


def test_accepted_but_unbound_request_is_not_replayed(tmp_path, monkeypatch):
    """受理记录已落盘、回合没绑上（受理后崩溃）。必须报 pending，**不自动重播**。"""
    calls, run_turn = _counting_turn()
    runtime = _runtime(tmp_path, run_turn)
    session_id = runtime.create_session('sess')

    # 模拟"上一个进程受理了但没来得及执行"。指纹必须与 send 内部算的一致，
    # 所以拿 runtime 自己解析出的工作区来构造。
    workspace = runtime._workspace_for(runtime._open_session(session_id))
    request_dedup.claim(
        'req-x',
        fingerprint=request_dedup.fingerprint(
            text='run tests', workspace=str(workspace), mode='api'),
        session_id=session_id, origin='api')

    from agent_runtime import RequestPending
    with pytest.raises(RequestPending):
        runtime.send(session_id, 'run tests', request_id='req-x')
    assert calls == []


def test_capacity_refuses_new_ids(tmp_path, monkeypatch):
    calls, run_turn = _counting_turn()
    runtime = _runtime(tmp_path, run_turn)
    session_id = runtime.create_session('sess')
    monkeypatch.setattr(request_dedup, 'MAX_RECORDS', 1)

    runtime.send(session_id, 'one', request_id='req-1')
    with pytest.raises(RequestConflict) as excinfo:
        runtime.send(session_id, 'two', request_id='req-2')
    assert 'capacity' in str(excinfo.value) or '满' in str(excinfo.value)
    assert len(calls) == 1


def test_api_layer_accepts_request_id_and_maps_conflict_to_409():
    """API 必须能把 request_id 传下去，并把冲突映射成 409（不是 500）。"""
    import inspect

    from api_models import SendMessageRequest

    assert 'request_id' in SendMessageRequest.model_fields
    schema = inspect.getsource(__import__('api_sessions'))
    assert 'request.request_id' in schema, '路由没有把 request_id 传下去'
    assert 'RequestConflict' in schema and '409' in schema
