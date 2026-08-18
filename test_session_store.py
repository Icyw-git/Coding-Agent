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
