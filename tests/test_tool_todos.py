"""tool_todos：校验、会话隔离与事件流恢复。"""
import tool_todos


class _Store:
    session_id = 's1'

    def __init__(self):
        self.events = []

    def append(self, event_type, **payload):
        self.events.append({'type': event_type, **payload})

    def read_events(self):
        return list(self.events)


def _clear():
    with tool_todos._TODOS_LOCK:
        tool_todos._TODOS.clear()


def test_normalize_rejects_duplicates_and_multiple_active():
    _clear()
    duplicates = [{'content': 'a', 'status': 'pending'},
                  {'content': 'a', 'status': 'pending'}]
    assert 'duplicate' in tool_todos.normalize_todos(duplicates)[1]

    two_active = [{'content': 'a', 'status': 'in_progress'},
                  {'content': 'b', 'status': 'in_progress'}]
    assert 'only one' in tool_todos.normalize_todos(two_active)[1]

    too_long = [{'content': 'x' * (tool_todos.TODO_MAX_CHARS + 1),
                 'status': 'pending'}]
    assert 'too long' in tool_todos.normalize_todos(too_long)[1]


def test_write_persists_event_and_isolates_scopes():
    _clear()
    store = _Store()

    output = tool_todos.write_todos(
        [{'content': 'a', 'status': 'in_progress'}],
        scope='s1', session_store=store)

    assert 'Updated 1 tasks' in output
    assert store.events[-1]['type'] == 'todos'
    assert tool_todos.read_todos(scope='s1', session_store=store).startswith('0/1')
    # 每个会话有自己的事件流；空会话不会读到别的会话的清单。
    empty = _Store()
    empty.session_id = 's2'
    assert 'No tasks' in tool_todos.read_todos(scope='s2', session_store=empty)


def test_restore_recovers_from_last_event():
    _clear()
    store = _Store()
    store.append('todos', todos=[{'content': 'old', 'status': 'pending'}])
    store.append('todos', todos=[{'content': 'new', 'status': 'completed'}])

    restored = tool_todos.restore(store, 's1')

    assert restored == [{'content': 'new', 'status': 'completed'}]
    assert '1/1 completed' in tool_todos.read_todos(scope='s1',
                                                    session_store=store)


def test_read_without_store_or_state_is_empty():
    _clear()
    assert 'No tasks' in tool_todos.read_todos(scope='s1', session_store=None)
    assert tool_todos.restore(None) == []
