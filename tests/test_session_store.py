import json
import threading
from pathlib import Path

import pytest

from session_store import SessionEventStore, SessionReplayError


def _tool_call(call_id="call_1", name="read"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": '{"path":"a.txt"}'},
    }


def test_rebuild_openai_history_preserves_tool_pair(tmp_path):
    store = SessionEventStore(tmp_path, session_id="normal")
    user = {"role": "user", "content": "read a.txt"}
    assistant = {"role": "assistant", "content": None, "tool_calls": [_tool_call()]}
    result = {"role": "tool", "tool_call_id": "call_1", "content": "contents"}

    store.append_message(user)
    store.append_message(assistant)
    store.append("tool_started", tool_call_id="call_1", name="read", arguments='{"path":"a.txt"}')
    store.append_message(result)

    assert store.rebuild_openai_history() == [user, assistant, result]
    assert store.pending_tool_calls() == []


def test_tail_events_reads_incrementally_and_holds_partial_line(tmp_path):
    """tail_events：增量读、暂停在半行上、半行补全后可继续读出。"""
    store = SessionEventStore(tmp_path, session_id="tail")
    store.append("session_created")
    store.append_message({"role": "user", "content": "hi"})

    events, offset = store.tail_events(0)
    assert [e["type"] for e in events] == ["session_created", "message"]

    again, offset = store.tail_events(offset)
    assert again == []

    # 写半行（模拟进程写到一半）→ tail 停在它之前
    with open(store.path, "a", encoding="utf-8") as handle:
        handle.write('{"v":1,"seq":3,"type":"turn')
    partial, held_offset = store.tail_events(offset)
    assert partial == [] and held_offset == offset

    # 半行补全 → 下次 tail 读出完整事件
    with open(store.path, "a", encoding="utf-8") as handle:
        handle.write('_completed"}\n')
    done, offset = store.tail_events(offset)
    assert [e["type"] for e in done] == ["turn_completed"]
    assert offset > held_offset


def test_pending_tool_call_produces_recovery_message(tmp_path):
    store = SessionEventStore(tmp_path, session_id="pending")
    store.append_message({"role": "user", "content": "inspect"})
    store.append_message({"role": "assistant", "content": None, "tool_calls": [_tool_call("call_9", "bash")]})

    pending = store.pending_tool_calls()
    assert pending[0]["id"] == "call_9"
    assert "call_9" in store.recovery_message()["content"]


def test_compaction_replaces_prior_history_for_runtime_replay(tmp_path):
    store = SessionEventStore(tmp_path, session_id="compacted")
    store.append_message({"role": "user", "content": "old request"})
    store.append_message({"role": "assistant", "content": "old response"})
    store.append_compaction("goal: continue safely", "auto")
    latest = {"role": "user", "content": "continue"}
    store.append_message(latest)

    assert store.rebuild_openai_history() == [
        {"role": "user", "content": "[Compacted]\n\ngoal: continue safely"},
        latest,
    ]


def test_partial_tail_is_ignored_but_non_tail_corruption_fails(tmp_path):
    store = SessionEventStore(tmp_path, session_id="partial")
    store.append_message({"role": "user", "content": "hello"})
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write('{"v":1')

    assert len(store.read_events()) == 1

    bad = Path(tmp_path) / "session_bad.jsonl"
    bad.write_text('{"v":1}\nnot-json\n{"v":1}\n', encoding="utf-8")
    with pytest.raises(SessionReplayError):
        SessionEventStore.open(bad).read_events()


def _snapshot(tmp_path, messages):
    path = tmp_path / "cp_x" / "messages.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(m) + "\n" for m in messages), encoding="utf-8")
    return path


def test_rewind_event_replaces_history_with_snapshot(tmp_path):
    snapshot = _snapshot(tmp_path, [
        {"role": "user", "content": "original request"},
        {"role": "assistant", "content": "original answer"},
    ])
    store = SessionEventStore(tmp_path / "sessions", session_id="rewound")
    store.append_message({"role": "user", "content": "original request"})
    store.append_message({"role": "assistant", "content": "original answer"})
    store.append_message({"role": "user", "content": "bad detour"})
    store.append_rewind("cp_x", snapshot_path=str(snapshot))
    store.append_message({"role": "user", "content": "continue"})

    history = store.rebuild_openai_history()
    assert [m["content"] for m in history[:2]] == ["original request", "original answer"]
    assert history[2]["content"].startswith("[Rewound to cp_x]")
    assert history[-1] == {"role": "user", "content": "continue"}


def test_rewind_without_snapshot_fails_replay(tmp_path):
    store = SessionEventStore(tmp_path / "sessions", session_id="missing")
    store.append_message({"role": "user", "content": "hello"})
    store.append_rewind("cp_gone", snapshot_path=str(tmp_path / "nope.jsonl"))

    with pytest.raises(SessionReplayError):
        store.rebuild_openai_history()


def test_checkpoint_event_does_not_change_replayed_history(tmp_path):
    store = SessionEventStore(tmp_path / "sessions", session_id="cpevent")
    store.append_message({"role": "user", "content": "hello"})
    store.append_checkpoint("cp_1", label="before refactor", trigger="manual")

    assert store.rebuild_openai_history() == [{"role": "user", "content": "hello"}]
    assert store.last_seq == 2
    assert store.read_events()[1]["label"] == "before refactor"


def test_concurrent_store_instances_allocate_unique_sequences(tmp_path):
    first = SessionEventStore(tmp_path, session_id="concurrent")
    second = SessionEventStore(tmp_path, session_id="concurrent")
    barrier = threading.Barrier(2)
    sequences = []

    def append_from(store, content):
        barrier.wait()
        sequences.append(store.append_message({"role": "user", "content": content})["seq"])

    threads = [
        threading.Thread(target=append_from, args=(first, "one")),
        threading.Thread(target=append_from, args=(second, "two")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = first.read_events()
    assert sorted(sequences) == [1, 2]
    assert [event["seq"] for event in events] == [1, 2]
    assert sorted(message["content"] for message in first.rebuild_openai_history()) == ["one", "two"]


def test_path_lock_shared_across_instances(tmp_path):
    """同路径多实例共享同一把锁（锁在初始化时解析一次）。"""
    s1 = SessionEventStore(tmp_path, session_id="locktest")
    s2 = SessionEventStore(tmp_path, session_id="locktest")
    assert s1._lock is s2._lock

    # 并发 append 不允许重号（共享锁保证互斥）
    import threading
    def worker(store, n):
        for _ in range(n):
            store.append_message({"role": "user", "content": "x"})

    threads = [threading.Thread(target=worker, args=(s, 20)) for s in (s1, s2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [e["seq"] for e in s1.read_events()]
    assert len(seqs) == len(set(seqs)) == 40


def test_large_session_disables_event_cache_and_streams_rebuild(tmp_path, monkeypatch):
    store = SessionEventStore(tmp_path, session_id='large')
    monkeypatch.setenv('HARNESS_EVENT_CACHE_MAX_BYTES', '512')
    for index in range(20):
        store.append_message({'role': 'user', 'content': f'message-{index}-' + 'x' * 100})

    events = store.read_events()
    assert len(events) == 20
    assert store._events_cache is None       # 超限不保留完整事件表
    assert [event['seq'] for event in store.iter_events()] == list(range(1, 21))
    assert [m['content'] for m in store.rebuild_openai_history()] == [
        f'message-{index}-' + 'x' * 100 for index in range(20)]

    summary = store.session_summary()
    assert summary['events'] == 20 and store._summary_cache is not None
    store.append_message({'role': 'assistant', 'content': 'done'})
    assert store._summary_cache is None
    assert store.session_summary()['events'] == 21


def test_streaming_rebuild_applies_compaction_and_placeholders(tmp_path, monkeypatch):
    monkeypatch.setenv('HARNESS_EVENT_CACHE_MAX_BYTES', '1')
    store = SessionEventStore(tmp_path, session_id='streaming')
    store.append_message({'role': 'user', 'content': 'start'})
    store.append_message({'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'call-1', 'type': 'function',
         'function': {'name': 'read', 'arguments': '{}'}},
    ]})
    store.append_message({'role': 'tool', 'tool_call_id': 'call-1',
                          'content': 'raw output'})
    store.append_tool_compacted('call-1', '<omitted tool output>')
    store.append_compaction('compressed', 'auto', tail=[
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call-1', 'type': 'function',
             'function': {'name': 'read', 'arguments': '{}'}},
        ]},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'raw output'},
        {'role': 'user', 'content': 'continue'},
    ])

    history = store.rebuild_openai_history()
    assert history[0] == {'role': 'user', 'content': '[Compacted]' + chr(10) * 2 + 'compressed'}
    assert history[1]['role'] == 'assistant'
    assert history[2] == {'role': 'tool', 'tool_call_id': 'call-1',
                          'content': '<omitted tool output>'}
    assert history[3] == {'role': 'user', 'content': 'continue'}
