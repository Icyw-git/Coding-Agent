"""Incremental history must equal fresh replay, with independent returned values."""
import json

import pytest

from session_store import SessionEventStore, SessionReplayError


def fresh(store, **kwargs):
    return SessionEventStore.open(store.path).rebuild_openai_history(**kwargs)


def test_append_projection_skips_replay_and_isolates_nested_values(tmp_path, monkeypatch):
    store = SessionEventStore(tmp_path, session_id='projection')
    store.append_message({'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]})
    result = store.rebuild_openai_history()
    result[0]['content'][0]['text'] = 'caller mutation'
    store.append_message({'role': 'assistant', 'content': 'answer'})
    store.append('usage', prompt=10)
    expected = fresh(store)

    def unexpected(**kwargs):
        pytest.fail('normal appends should not replay the entire log')

    monkeypatch.setattr(store, '_replay_openai_history', unexpected)
    actual = store.rebuild_openai_history()
    assert actual == expected
    assert actual[0]['content'][0]['text'] == 'hello'
    actual[-1]['content'] = 'another mutation'
    assert store.rebuild_openai_history() == expected


@pytest.mark.parametrize('streaming', [False, True])
def test_transform_events_invalidate_projection(tmp_path, monkeypatch, streaming):
    if streaming:
        monkeypatch.setenv('HARNESS_EVENT_CACHE_MAX_BYTES', '0')
    store = SessionEventStore(tmp_path, session_id='transforms')
    store.append_message({'role': 'user', 'content': 'goal'})
    store.rebuild_openai_history()
    store.append_compaction('summary', 'test', tail=[{'role': 'user', 'content': 'recent'}])
    assert store._history_cache is None
    assert store.rebuild_openai_history() == fresh(store)
    store.append_message({'role': 'assistant', 'content': 'answer'})
    store.append_message({'role': 'user', 'content': 'next'})
    assert store.rebuild_openai_history() == fresh(store)
    store.append_history_snip(1, 3, 2)
    assert store._history_cache is None
    assert store.rebuild_openai_history() == fresh(store)


def test_external_append_and_same_size_rewrite_invalidate(tmp_path):
    store = SessionEventStore(tmp_path, session_id='external')
    store.append_message({'role': 'user', 'content': 'first'})
    store.rebuild_openai_history()
    other = SessionEventStore.open(store.path)
    other.append_message({'role': 'assistant', 'content': 'second'})
    store.append_message({'role': 'user', 'content': 'third'})
    assert store.rebuild_openai_history() == fresh(store)
    data = store.path.read_text(encoding='utf-8').replace('first', 'FIRST')
    store.path.write_text(data, encoding='utf-8')
    assert store.rebuild_openai_history() == fresh(store)
    assert store.rebuild_openai_history()[0]['content'] == 'FIRST'


def test_tool_compaction_invalidates_cached_projection(tmp_path):
    store = SessionEventStore(tmp_path, session_id='placeholder')
    store.append_message({'role': 'user', 'content': 'goal'})
    store.append_message({'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'call', 'type': 'function',
         'function': {'name': 'read', 'arguments': '{}'}},
    ]})
    store.append_message({'role': 'tool', 'tool_call_id': 'call', 'content': 'raw'})
    store.rebuild_openai_history()
    store.append_tool_compacted('call', 'omitted')
    assert store._history_cache is None
    actual = store.rebuild_openai_history()
    assert actual == fresh(store)
    assert actual[-1]['content'] == 'omitted'


def test_cached_pending_tool_recovery_is_persisted_once(tmp_path):
    store = SessionEventStore(tmp_path, session_id='recovery')
    store.append_message({'role': 'user', 'content': 'goal'})
    store.rebuild_openai_history()
    store.append_message({'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'call', 'type': 'function',
         'function': {'name': 'read', 'arguments': '{}'}},
    ]})
    actual = store.rebuild_openai_history(recover_pending=True)
    assert actual == fresh(store)
    assert actual[-1]['role'] == 'tool'
    store.rebuild_openai_history(recover_pending=True)
    assert sum(e['type'] == 'tool_recovery' for e in store.read_events()) == 1


def test_rewind_keeps_snapshot_dependency_checks(tmp_path):
    snapshot = tmp_path / 'messages.jsonl'
    snapshot.write_text(json.dumps({'role': 'user', 'content': 'before'}) + '\n')
    store = SessionEventStore(tmp_path / 'logs', session_id='rewind')
    store.append_message({'role': 'user', 'content': 'after'})
    store.rebuild_openai_history()
    store.append_rewind('cp', snapshot_path=str(snapshot))
    assert store.rebuild_openai_history() == fresh(store)
    assert store._history_cache is None
    snapshot.unlink()
    with pytest.raises(SessionReplayError, match='snapshot missing'):
        store.rebuild_openai_history()


@pytest.mark.parametrize('streaming', [False, True])
def test_partial_tail_does_not_seed_projection(tmp_path, monkeypatch, streaming):
    if streaming:
        monkeypatch.setenv('HARNESS_EVENT_CACHE_MAX_BYTES', '0')
    store = SessionEventStore(tmp_path, session_id='partial')
    store.append_message({'role': 'user', 'content': 'kept'})
    with store.path.open('a', encoding='utf-8') as handle:
        handle.write('{"v":1')
    assert store.rebuild_openai_history() == [{'role': 'user', 'content': 'kept'}]
    assert store._history_cache is None


def test_projection_budget_and_pending_event_limit(tmp_path):
    store = SessionEventStore(tmp_path, session_id='bounded')
    store.append_message({'role': 'user', 'content': 'small'})
    store.rebuild_openai_history()
    for _ in range(257):
        store.append_message({'role': 'user', 'content': 'next'})
    assert store._history_cache is None
    store.rebuild_openai_history()
    store.append_message({'role': 'user', 'content': 'x' * (2 * 1024 * 1024)})
    assert store._history_cache is None
    assert store.rebuild_openai_history() == fresh(store)
    assert store._history_cache is None
