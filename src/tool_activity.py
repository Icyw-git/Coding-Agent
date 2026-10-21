"""CLI 里的工具活动行：一行展示「做了什么、耗时多少、是否失败」。

对标 Codex / Pi 的工具块，但保持单行：终端的滚动输出不打断流式回答，也让长回合
之后仍能看出每一步做了什么。纯格式化，没有 IO，便于测试。
"""
from __future__ import annotations

_PRIORITY = (
    'command', 'path', 'pattern', 'url', 'query', 'name', 'subject',
    'teammate', 'task', 'todos', 'patch', 'label', 'job_id', 'bg_id',
)


def summarize(name: str, arguments: dict | None) -> str:
    """从参数里挑最能说明意图的字段，压缩成一行。"""
    arguments = arguments or {}
    for key in _PRIORITY:
        value = arguments.get(key)
        if value in (None, '', [], {}):
            continue
        if key in ('todos', 'patch'):
            if isinstance(value, list):
                return f'{len(value)} item(s)'
            text = str(value).splitlines()[0] if str(value) else ''
            return text[:60] + ('…' if len(text) > 60 else '')
        if isinstance(value, (list, tuple)):
            return ', '.join(str(item) for item in value)[:60]
        if isinstance(value, dict):
            continue
        text = ' '.join(str(value).split())
        return text[:60] + ('…' if len(text) > 60 else '')
    cwd = arguments.get('cwd')
    return f'cwd={cwd}' if cwd else ''


def format_line(name: str, arguments: dict | None, elapsed: float,
                failed: bool = False, background: bool = False,
                hint: str = '') -> str:
    """`  • bash  pytest -q  3.40s ✓  → 42 行` 形式的单行活动。"""
    parts = [f'• {name}{" ↗后台" if background else ""}']
    detail = summarize(name, arguments)
    if detail:
        parts.append(detail)
    parts.append(f'{elapsed:.2f}s {"✗" if failed else "✓"}')
    if hint and not failed:
        parts.append(f'→ {hint}')
    return '  ' + '  '.join(parts)


def result_hint(output) -> str:
    """工具输出的体量提示：多行只报行数，不预览内容。"""
    text = str(output).strip()
    if not text:
        return '空'
    if text == 'No output':
        return '无输出'
    lines = text.count('\n') + 1
    return f'{lines} 行' if lines > 1 else ''


def is_failure(output) -> bool:
    """工具约定：错误以 `Error` 开头，拒绝以 `Permission denied` 开头。"""
    return str(output).lstrip().startswith(('Error', 'Permission denied'))


FOLD_THRESHOLD = 8


def preview_lines(output, head: int = 4, tail: int = 2,
                  threshold: int = FOLD_THRESHOLD) -> tuple[list[str], int]:
    """长输出折叠成「头 + … + 尾」；返回 (行列表, 被折叠的行数)。"""
    lines = str(output).splitlines()
    if len(lines) <= threshold:
        return lines, 0
    hidden = len(lines) - head - tail
    if hidden <= 0:
        return lines, 0
    return [*lines[:head], '…', *lines[-tail:]], hidden
