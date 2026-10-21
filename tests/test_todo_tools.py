"""todo_write / todo_read：输入校验、会话隔离、事件流持久化与恢复。"""
from types import SimpleNamespace

import pytest

import harness
import hooks
import tools
from session_store import SessionEventStore


@pytest.fixture(autouse=True)
def _clean_todos(monkeypatch):
    # 隔离跨用例的 checkpoint manager：否则 todo 事件会写进上一个用例的 store
    monkeypatch.setattr(harness, 'active_checkpoint_manager', lambda: None)
    with tools._TODOS_LOCK:
        tools._TODOS.clear()
    yield
    with tools._TODOS_LOCK:
        tools._TODOS.clear()


def test_write_normalizes_and_read_returns_list():
    result = tools.run_todo_write([
        {'content': ' 调研  ', 'status': 'completed'},
        {'content': '实现', 'status': 'in_progress'},
        {'content': '测试', 'status': 'pending'},
    ])
    assert '1/3 completed' in result
    assert '[x] 调研' in result and '[>] 实现' in result and '[ ] 测试' in result
    assert tools.run_todo_read().startswith('1/3 completed')


def test_write_replaces_previous_list():
    tools.run_todo_write([{'content': 'old', 'status': 'pending'}])
    tools.run_todo_write([{'content': 'new', 'status': 'pending'}])
    output = tools.run_todo_read()
    assert 'new' in output and 'old' not in output


@pytest.mark.parametrize('todos,fragment', [
    ('not json', 'must be a list'),
    ({'content': 'x'}, 'must be a list'),
    ([{'content': 'x'}], "missing 'content' or 'status'"),
    ([{'content': 'x', 'status': 'done'}], 'invalid status'),
    ([{'content': ' ', 'status': 'pending'}], 'must not be empty'),
    ([{'content': 'a', 'status': 'in_progress'},
      {'content': 'b', 'status': 'in_progress'}], 'only one todo may be in_progress'),
    ([{'content': 'a', 'status': 'pending'},
      {'content': 'a', 'status': 'completed'}], 'duplicate todo content'),
    ([{'content': 'x' * 501, 'status': 'pending'}], 'too long'),
    ([{'content': str(i), 'status': 'pending'} for i in range(101)], 'at most 100'),
])
def test_write_rejects_invalid_lists(todos, fragment):
    assert fragment in tools.run_todo_write(todos)
    assert tools.run_todo_read() == 'No tasks. Use todo_write to create a task list.'


def test_invalid_write_keeps_previous_plan():
    tools.run_todo_write([{'content': 'keep', 'status': 'pending'}])
    tools.run_todo_write([{'content': 'bad', 'status': 'in_progress'},
                          {'content': 'worse', 'status': 'in_progress'}])
    assert 'keep' in tools.run_todo_read()


def test_clear_list():
    tools.run_todo_write([{'content': 'x', 'status': 'pending'}])
    assert tools.run_todo_write([]) == 'Task list cleared.'
    assert 'No tasks' in tools.run_todo_read()


def test_todos_are_isolated_per_session():
    with hooks.use_session('A'):
        tools.run_todo_write([{'content': 'A plan', 'status': 'pending'}])
    with hooks.use_session('B'):
        assert 'No tasks' in tools.run_todo_read()
        tools.run_todo_write([{'content': 'B plan', 'status': 'pending'}])
    with hooks.use_session('A'):
        assert 'A plan' in tools.run_todo_read()


def test_write_persists_todos_event_and_restore(tmp_path, monkeypatch):
    store = SessionEventStore(tmp_path, session_id='sid')
    monkeypatch.setattr(
        harness, 'active_checkpoint_manager',
        lambda: SimpleNamespace(session_store=store, session_id='sid'))
    tools.run_todo_write([{'content': 'persisted', 'status': 'in_progress'}])

    event = store.read_events()[-1]
    assert event['type'] == 'todos'
    assert event['todos'] == [{'content': 'persisted', 'status': 'in_progress'}]

    with tools._TODOS_LOCK:
        tools._TODOS.clear()
    restored = tools.restore_todos(store)
    assert restored == [{'content': 'persisted', 'status': 'in_progress'}]
    assert 'persisted' in tools.run_todo_read()


def test_restore_uses_last_todos_event(tmp_path):
    store = SessionEventStore(tmp_path, session_id='sid')
    store.append('todos', todos=[{'content': 'first', 'status': 'pending'}])
    store.append('todos', todos=[])
    assert tools.restore_todos(store) == []
    assert 'No tasks' in tools.run_todo_read()
