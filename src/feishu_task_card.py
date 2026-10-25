"""后台任务卡片：/status 的任务列表与 /logs 的输出，改成可点按钮的卡片。

为什么加：`/run` 起的后台任务以前只能用命令查、命令停；手机上要记 `/status`、
`/logs <id> 80`、`/stop <id>` 三套参数。这里把「刷新 / 看日志 / 停止 / 返回列表」
做成按钮，命令保留为兼容入口。

卡片只是展示层：任务状态、输出、停止都仍走既有 API，不缓存、不推断。
"""
from __future__ import annotations

MAX_TASKS = 8
MAX_LOG_LINES = 120
MAX_LOG_CHARS = 12_000


def _text(content: str) -> dict:
    return {'tag': 'div',
            'text': {'tag': 'plain_text', 'content': str(content)}}


def _code(content: str) -> dict:
    return {'tag': 'div',
            'text': {'tag': 'lark_md', 'content': f'```\n{content}\n```'}}


def _button(label: str, value: dict, *, kind: str = 'default') -> dict:
    return {'tag': 'button',
            'text': {'tag': 'plain_text', 'content': label},
            'type': kind,
            'value': value}


def _duration(task: dict) -> str:
    from turn_progress import format_duration

    for key in ('duration_s', 'elapsed_s'):
        value = task.get(key)
        if isinstance(value, (int, float)):
            return format_duration(value)
    if isinstance(task.get('duration_ms'), (int, float)):
        return format_duration(task['duration_ms'] / 1000)
    return '—'


def _task_line(task: dict) -> str:
    status = task.get('status') or 'unknown'
    exit_code = task.get('exit_code')
    tail = '' if exit_code is None else f' · exit={exit_code}'
    return f"{task.get('id')} · {status}{tail} · 用时 {_duration(task)}"


def tasks_card(tasks: list[dict], *, intro: str = '') -> dict:
    """后台任务列表：每个任务带「看日志 / 停止」，底部统一「刷新」。"""
    elements: list[dict] = []
    if intro:
        elements.append(_text(intro))
    if not tasks:
        elements.append(_text('（没有后台任务）\n'
                              '需要跑命令时直接说“在后台跑 xxx”，或点下面的「刷新」。'))
    for task in tasks[:MAX_TASKS]:
        task_id = str(task.get('id') or '')
        command = ' '.join(str(task.get('command') or task.get('name') or '').split())
        if len(command) > 80:
            command = command[:80] + '…'
        elements.append(_text(_task_line(task) + (f'\n{command}' if command else '')))
        buttons = [_button('📄 看日志', {'task_action': 'logs', 'task_id': task_id})]
        if (task.get('status') or '') in ('running', 'queued', 'pending'):
            buttons.append(_button('⏹ 停止', {'task_action': 'stop',
                                             'task_id': task_id}, kind='danger'))
        elements.append({'tag': 'action', 'actions': buttons})
        elements.append({'tag': 'hr'})
    rest = len(tasks) - MAX_TASKS
    if rest > 0:
        elements.append(_text(f'…另有 {rest} 个任务未显示（/status 查看全部）。'))
    elements.append({'tag': 'action', 'actions': [
        _button('🔄 刷新', {'task_action': 'refresh'}),
        _button('⚙️ 控制台', {'panel': 'menu'}),
    ]})
    return {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text',
                             'content': f'后台任务（{len(tasks)}）'}},
        'elements': elements,
    }


def logs_card(task: dict, logs: str, *, lines: int = 60) -> dict:
    """单个任务的输出：只保留末尾若干行，避免把卡片撑爆。"""
    task_id = str(task.get('id') or '')
    text = str(logs or '').strip() or '(no output)'
    rows = text.splitlines()
    if len(rows) > MAX_LOG_LINES:
        rows = rows[-MAX_LOG_LINES:]
    text = '\n'.join(rows)
    if len(text) > MAX_LOG_CHARS:
        text = '…' + text[-MAX_LOG_CHARS:]
    elements = [
        _text(_task_line(task)),
        _code(text),
        {'tag': 'action', 'actions': [
            _button('🔄 刷新', {'task_action': 'logs', 'task_id': task_id,
                                'lines': lines}),
            _button('📋 任务列表', {'task_action': 'refresh'}),
        ]},
    ]
    if (task.get('status') or '') in ('running', 'queued', 'pending'):
        elements.insert(2, {'tag': 'action', 'actions': [
            _button('⏹ 停止', {'task_action': 'stop', 'task_id': task_id},
                    kind='danger')]})
    return {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': f'任务日志 · {task_id}'}},
        'elements': elements,
    }


def tasks_text(tasks: list[dict]) -> str:
    if not tasks:
        return '(no tasks)'
    return '\n'.join(_task_line(task) for task in tasks)
