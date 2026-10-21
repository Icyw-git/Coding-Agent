"""工具块渲染：调用/结果两段 + diff 着色 + 预览行数（对齐 Pi tool-execution）。"""
from __future__ import annotations

import re

from rich.text import Text

_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')


def _strip_ansi(text: str) -> str:
    """工具输出里的 ANSI 色码在 Textual 里会变成乱码/串色，展示前统一去掉。"""
    return _ANSI_RE.sub('', text)


TOOL_PREVIEW_LINES = 10
TOOL_MORE_LINES = 40
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

    output = _strip_ansi(str(payload.get('output') or ''))
    lines = output.splitlines()
    hint = ''
    if not payload.get('expanded') and len(lines) > TOOL_PREVIEW_LINES:
        limit = int(payload.get('preview_lines') or TOOL_PREVIEW_LINES)
        limit = max(TOOL_PREVIEW_LINES, min(limit, TOOL_MORE_LINES))
        hidden = len(lines) - limit
        if hidden > 0:
            action = 'show less' if limit > TOOL_PREVIEW_LINES else 'show more'
            lines = lines[:limit]
            hint = f'… {hidden} more lines · 点击 {action}'
        elif limit > TOOL_PREVIEW_LINES:
            lines = lines[:limit]
            hint = '… 点击 show less'
    if lines or hint:
        body = Text(chr(10).join(lines), style='#bcbcbc')
        if hint:
            if lines:
                body.append(chr(10))
            body.append(hint, style='bold #5fafff')
        parts.append(body)

    result = Text()
    for index, part in enumerate(parts):
        if index:
            result.append(chr(10))
        result.append_text(part)
    return result
