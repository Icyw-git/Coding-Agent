"""todo 清单：校验、会话分区、事件流持久化与格式化输出。

内存 `_TODOS` 只是缓存，事件流才是持久源（进程重启后 `restore` 从最后一个
`todos` 事件恢复）。会话 id 与事件流由 tools 在调用时注入，模块不读取入口状态。
"""
from __future__ import annotations

import ast
import json
import threading

TODO_MAX_ITEMS = 100
TODO_MAX_CHARS = 500
_TODOS: dict[str, list[dict]] = {}
_TODOS_LOCK = threading.Lock()


def normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    if len(todos) > TODO_MAX_ITEMS:
        return None, f"Error: at most {TODO_MAX_ITEMS} todos are supported"
    normalized = []
    seen = set()
    active = []
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
        content = str(t["content"]).strip()
        if not content:
            return None, f"Error: todos[{i}] content must not be empty"
        if len(content) > TODO_MAX_CHARS:
            return None, (f"Error: todos[{i}] content is too long "
                          f"(max {TODO_MAX_CHARS} chars)")
        if content in seen:
            return None, f"Error: duplicate todo content: {content}"
        seen.add(content)
        if t["status"] == "in_progress":
            active.append(i)
        normalized.append({"content": content, "status": t["status"]})
    if len(active) > 1:
        return None, (f"Error: only one todo may be in_progress (got indexes "
                      f"{', '.join(str(i) for i in active)})")
    return normalized, None


def stats(todos: list[dict]) -> str:
    counts = {"completed": 0, "in_progress": 0, "pending": 0}
    for t in todos:
        counts[t["status"]] += 1
    return (f"{counts['completed']}/{len(todos)} completed · "
            f"{counts['in_progress']} in progress · {counts['pending']} pending")


def format_todos(todos: list[dict]) -> str:
    icons = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
    return '\n'.join(f'{icons[t["status"]]} {t["content"]}' for t in todos)


def print_todos(todos: list[dict]) -> None:
    print("\n\033[33m## Current Tasks\033[0m")
    icons = {"pending": " ", "in_progress": "\033[36m▸\033[0m",
             "completed": "\033[32m✓\033[0m"}
    for t in todos:
        print(f"  [{icons[t['status']]}] {t['content']}")
    print(f"  \033[90m{stats(todos)}\033[0m")


def restore(session_store, scope: str | None = None) -> list[dict]:
    """从事件流恢复最后一次 todo_write 的状态（会话重启后仍可读）。"""
    if session_store is None:
        return []
    target = scope or session_store.session_id
    try:
        events = session_store.read_events()
    except Exception:
        return []
    last = next((event for event in reversed(events)
                 if event.get('type') == 'todos'
                 and isinstance(event.get('todos'), list)), None)
    if last is None:
        return []
    normalized, error = normalize_todos(last['todos'])
    if error:
        return []
    with _TODOS_LOCK:
        _TODOS[target] = normalized
    return normalized


def write_todos(todos: list, *, scope: str, session_store) -> str:
    normalized, error = normalize_todos(todos)
    if error:
        return error
    with _TODOS_LOCK:
        _TODOS[scope] = normalized
    if session_store is not None:
        try:
            session_store.append('todos', todos=normalized)
        except Exception:
            pass   # 事件落盘失败不回滚内存里的计划
    print_todos(normalized)
    if not normalized:
        return "Task list cleared."
    return (f"Updated {len(normalized)} tasks ({stats(normalized)}).\n"
            + format_todos(normalized))


def read_todos(*, scope: str, session_store) -> str:
    with _TODOS_LOCK:
        todos = list(_TODOS.get(scope, []))
    if not todos:
        todos = restore(session_store, scope)
    if not todos:
        return "No tasks. Use todo_write to create a task list."
    return f"{stats(todos)}.\n{format_todos(todos)}"
