"""`/watch`：后台任务的人类可读视图与「等到终态再取全文」。

后台 worker 只在任务结束时把输出写入 `background_results`，运行中拿不到增量
输出，所以这里不做假的实时 tail：跟踪期间只刷新状态与已用时，结束时打印全文。
"""
from __future__ import annotations

import time

from turn_progress import format_duration

DEFAULT_TIMEOUT = 600.0
INTERVAL = 1.0
MAX_OUTPUT = 50000
_ICONS = {
    'queued': '○', 'running': '●', 'completed': '✓',
    'failed': '✗', 'cancelled': '⊘',
}


def _store(store=None):
    if store is not None:
        return store
    import background_task
    return background_task


def _status_line(task: dict, now: float) -> str:
    icon = _ICONS.get(task.get('status'), '?')
    started = task.get('created_at')
    ended = task.get('ended_at')
    elapsed = (ended or now) - started if started else None
    elapsed_text = format_duration(elapsed) if elapsed is not None else '--:--'
    command = str(task.get('command') or '').replace('\n', ' ')[:60]
    return (f'  {icon} {task["id"]}  {task.get("status", "?")}  '
            f'{elapsed_text}  {command}')


def format_tasks(store=None) -> str:
    """/watch 不带参数：活跃任务在前，最近完成的在后。"""
    store = _store(store)
    tasks = store.list_background_tasks()
    if not tasks:
        return '  没有后台任务。长命令会自动转到后台，用 /watch <bg_id> 跟踪。'
    now = time.time()
    lines = ['  后台任务（活跃在前）：']
    lines.extend(_status_line(task, now) for task in tasks[:20])
    if len(tasks) > 20:
        lines.append(f'  … 另有 {len(tasks) - 20} 个已结束任务')
    lines.append('  用 /watch <bg_id> [--timeout 秒] 跟踪到结束。')
    return '\n'.join(lines)


def watch(bg_id: str, store=None, *, timeout: float = DEFAULT_TIMEOUT,
          interval: float = INTERVAL, emit=None, sleep=time.sleep,
          clock=time.monotonic) -> str:
    """等待任务进入终态并返回全文；超时或中断时返回当前状态提示。"""
    store = _store(store)
    emit = emit or (lambda text: None)
    deadline = clock() + max(0.0, timeout)
    announced = None
    try:
        while True:
            task = store.background_snapshot(bg_id)
            if task is None:
                return f'  未知后台任务：{bg_id}。用 /watch 查看列表。'
            status = task.get('status')
            if status not in ('running', 'queued'):
                _, output = store.read_background_result(bg_id)
                text = str(output if output is not None
                           else task.get('result', ''))
                if len(text) > MAX_OUTPUT:
                    text = text[:MAX_OUTPUT] + '\n... (truncated)'
                elapsed = ''
                if task.get('created_at') and task.get('ended_at'):
                    elapsed = (f' · 总用时 '
                               f'{format_duration(task["ended_at"] - task["created_at"])}')
                return (f'  [{bg_id}] {status}{elapsed}\n'
                        f'  Command: {task.get("command", "")}\n'
                        f'  Output:\n{text or "(no output)"}')
            line = _status_line(task, time.time())
            if line != announced:
                emit(line)
                announced = line
            remaining = deadline - clock()
            if remaining <= 0:
                return (f'  [{bg_id}] 仍在 {status}；已停止跟踪。'
                        '\n  用 /watch <bg_id> 继续，或 background_output 查看。')
            sleep(min(interval, remaining))
    except KeyboardInterrupt:
        return (f'  [{bg_id}] 已停止跟踪（任务继续在后台运行）。'
                '\n  用 /watch <bg_id> 继续。')


def _parse(argument: str):
    """解析 [bg_id|--list] [--timeout 秒]；bg_id 可省略。"""
    tokens = argument.split()
    if tokens and tokens[0] in ('--list', '-l'):
        return 'list', DEFAULT_TIMEOUT, None, None
    bg_id = None
    if tokens and not tokens[0].startswith('-'):
        bg_id = tokens.pop(0)
    timeout = DEFAULT_TIMEOUT
    while tokens:
        flag = tokens.pop(0)
        if flag != '--timeout':
            return 'error', None, None, '  用法：/watch [bg_id|--list] [--timeout 秒]'
        if not tokens:
            return 'error', None, None, '  --timeout 缺少秒数。'
        try:
            timeout = float(tokens.pop(0))
        except ValueError:
            return 'error', None, None, '  --timeout 需要一个秒数。'
        if timeout <= 0:
            return 'error', None, None, '  --timeout 必须大于 0。'
    return 'watch', timeout, bg_id, None


def watch_command(argument: str, store=None) -> str:
    """/watch 默认跟踪最新的后台任务，不需要记住编号。"""
    store = _store(store)
    action, timeout, bg_id, error = _parse(argument)
    if error:
        return error
    if action == 'list':
        return format_tasks(store)
    if bg_id is None:
        tasks = store.list_background_tasks()
        if not tasks:
            return format_tasks(store)
        bg_id = tasks[0]['id']
        running = sum(1 for task in tasks
                      if task.get('status') in ('running', 'queued'))
        if running > 1:
            emit_extra = (f'  另有 {running - 1} 个任务在运行；'
                          '用 /watch --list 查看。')
            print(emit_extra)
    return watch(bg_id, store, timeout=timeout, emit=print)
