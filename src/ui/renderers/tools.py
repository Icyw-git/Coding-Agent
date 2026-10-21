"""工具块渲染：调用/结果两段 + diff 着色 + 预览行数（对齐 Pi tool-execution）。"""
from __future__ import annotations

from rich.text import Text

TOOL_PREVIEW_LINES = 10
DIFF_MAX_LINES = 80

_STATE_COLORS = {
    'running': '#d7af5f',
    'completed': '#5fd75f',
    'complete': '#5fd75f',
    'failed': '#ff5f5f',
    'denied': '#ff5f5f',
    'queued': '#5fafff',
}


def tool_summary(name: str, args: dict) -> str:
    for key in ('command', 'path', 'pattern', 'url', 'query', 'name'):
        value = (args or {}).get(key)
        if value:
            text = ' '.join(str(value).split())
            return text if len(text) <= 72 else text[:69] + '...'
    return ''


def _diff_text(diff: str) -> Text:
    text = Text()
    lines = diff.splitlines()
    shown = lines[:DIFF_MAX_LINES]
    for line in shown:
        style = '#bcbcbc'
        if line.startswith(('+++', '---')):
            style = 'bold #a7a7a7'
        elif line.startswith('@@'):
            style = '#5fafff'
        elif line.startswith('+'):
            style = '#5fd75f'
        elif line.startswith('-'):
            style = '#ff5f5f'
        text.append(line + chr(10), style=style)
    if len(lines) > len(shown):
        text.append(f'… {len(lines) - len(shown)} more diff lines', style='#a7a7a7')
    return text


def tool_view(payload: dict) -> Text:
    """把工具块渲染成一段 Text（header + 可选 diff + 输出预览）。"""
    name = payload.get('name', 'tool')
    args = payload.get('args') or {}
    state = payload.get('state', 'running')
    duration = payload.get('duration_ms')
    duration_text = f' {duration / 1000:.1f}s' if duration else ''
    color = _STATE_COLORS.get(state, '#d7af5f')
    header = Text(f'▸ {name} {tool_summary(name, args)}{duration_text} [{state}]',
                  style=f'bold {color}')

    parts = [header]
    diff = str(payload.get('diff') or '')
    if diff:
        parts.append(_diff_text(diff))

    output = str(payload.get('output') or '')
    lines = output.splitlines()
    if not payload.get('expanded') and len(lines) > TOOL_PREVIEW_LINES:
        hidden = len(lines) - TOOL_PREVIEW_LINES
        lines = lines[:TOOL_PREVIEW_LINES] + [f'… {hidden} more lines']
    if lines:
        parts.append(Text('\n'.join(lines), style='#bcbcbc'))

    result = Text()
    for index, part in enumerate(parts):
        if index:
            result.append(chr(10))
        result.append_text(part)
    return result
