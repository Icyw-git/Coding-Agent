"""CLI 里的工具活动行：一行展示「做了什么、耗时多少、是否失败」。

对标 Codex / Pi 的工具块，但保持单行：终端的滚动输出不打断流式回答，也让长回合
之后仍能看出每一步做了什么。纯格式化，没有 IO，便于测试。
"""
from __future__ import annotations

_PRIORITY = (
    'command', 'path', 'pattern', 'url', 'query', 'name', 'subject',
    'teammate', 'task', 'todos', 'patch', 'label', 'job_id', 'bg_id',
    # 新增工具的参数：不给的话工具行上只有裸名字
    'moves', 'text', 'when', 'batch_id',
)


#: 桌面动作的单行摘要。这一行每次调用都会打印，必须短到能塞进一行，
#: 同时说清"点了什么/打了什么"——只给 `操作键鼠` 等于没写。
_DESKTOP_ACTIONS = {
    'move': '移动', 'click': '点击', 'type': '输入', 'key': '按键',
    'scroll': '滚动', 'sequence': '组合', 'focus': '切窗口',
    'element': '控件操作',
}



def _desktop_summary(name: str, arguments: dict) -> str:
    if name in ('desktop_inspect', 'desktop_displays', 'desktop_snapshot',
                'desktop_windows'):
        return ''
    if name == 'desktop_wait':
        needle = ' '.join(str(arguments.get('text') or '').split())[:24]
        absent = str(arguments.get('state') or '') == 'absent'
        return f'等「{needle}」{"消失" if absent else "出现"}' if needle else ''
    action = str(arguments.get('action') or '').strip().casefold()
    if action == 'element':
        control = arguments.get('element')
        name_hint = ' '.join(str(arguments.get('expect') or '').split())[:20]
        import approval_view

        verb = approval_view.ELEMENT_OPERATION_LABELS.get(
            str(arguments.get('operation') or 'invoke').casefold(), '操作')
        text = f'#{control if control is not None else "?"}'
        return f'控件 {text}' + (f'「{name_hint}」' if name_hint else '') + f' {verb}'
    if action == 'sequence':
        steps = arguments.get('steps')
        return f'组合 {len(steps)} 步' if isinstance(steps, list) else '组合动作'
    if action in ('click', 'move'):
        return (f'{_DESKTOP_ACTIONS[action]} '
                f'({arguments.get("x")},{arguments.get("y")})')
    if action == 'type':
        text = arguments.get('text')
        return f'输入 {len(text)} 字' if isinstance(text, str) else '输入'
    if action == 'key':
        return f'按键 {arguments.get("key")}'
    if action == 'scroll':
        # amount 是可选参数（默认 3 格）：没给时别把 None 端到用户面前。
        amount = arguments.get('amount')
        return f'滚动 {amount} 格' if amount not in (None, '') else '滚动'
    if action == 'focus':
        return '切到前台'
    return _DESKTOP_ACTIONS.get(action, '')


def summarize(name: str, arguments: dict | None) -> str:
    """从参数里挑最能说明意图的字段，压缩成一行。"""
    arguments = arguments or {}
    if str(name or '').startswith('desktop_'):
        return _desktop_summary(str(name), arguments)
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
            # 元素是 dict 时不能 str()：会打出 {'from': 'a', 'to': 'b'} 这种
            # 原始容器。移动这类工具补成「3 项」。
            if any(isinstance(item, dict) for item in value):
                return f'{len(value)} 项'
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
    # 符号必须 GBK 安全：这一行每次工具调用都打印，`•` `✓` `✗` 在中文 Windows
    # 控制台上会变成 • ✓ 这种转义串（harden_stdio 只保证不崩）。
    import approval_view
    label = approval_view.tool_name_zh(name)
    parts = [f'· {label}{" ↗后台" if background else ""}']
    detail = summarize(name, arguments)
    if detail:
        parts.append(detail)
    parts.append(f'{elapsed:.2f}s {"×" if failed else "√"}')
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
