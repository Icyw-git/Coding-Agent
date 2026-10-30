"""审计卡片：把最近的安全相关事件按时间倒序列出来。

只展示 ``audit.AUDIT_EVENTS`` 白名单里的事件，字段在服务端就裁剪过——卡片不
重复实现一遍脱敏，避免两处口径分叉。

设计取舍：
- 一条事件一行摘要，不做多层嵌套，手机上扫一眼就能读完；
- 类型用固定图标，方便一眼区分「看过屏幕」与「授权变更」；
- 每页 8 条，底部按钮翻页（PATCH 同一张卡）。
"""
from __future__ import annotations

PAGE_SIZE = 8

_ICONS = {
    'approval_timeout': '⌛',
    'approval_auto': '🤖',
    'screen_captured': '🖥️',
    'screen_ocr': '🔎',
    'clipboard_read': '📋',
    'clipboard_written': '📋',
    'desktop_snapshot': '🪟',
    'desktop_inspect': '🔍',
    'desktop_inspect_failed': '⚠️',
    'desktop_displays': '🖥',
    'desktop_displays_failed': '⚠️',
    'desktop_wait': '⏳',
    'desktop_wait_failed': '⚠️',
    'desktop_list_windows': '🗂️',
    'desktop_snapshot_failed': '⚠️',
    'desktop_action': '🖱️',
    'desktop_action_failed': '⚠️',
    'grant_added': '🔓',
    'grant_removed': '🔒',
    'domain_added': '🌐',
    'domain_removed': '🌐',
    'policy_denied': '⛔',
    'permission_revoked': '♻️',
}


def _text(content: str) -> dict:
    return {'tag': 'div',
            'text': {'tag': 'plain_text', 'content': content}}


def _page_button(label: str, *, page: int, token: str) -> dict:
    return {'tag': 'button',
            'text': {'tag': 'plain_text', 'content': label},
            'type': 'default',
            'value': {'audit_action': 'page', 'page': page, 'audit_view': token}}


def _clock(ts) -> str:
    import time
    try:
        return time.strftime('%m-%d %H:%M:%S', time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return '时间未知'


def _scope_zh(value) -> str:
    """allow / deny / write 这些字面量要翻成人话。"""
    return {'allow': '允许', 'deny': '拒绝', 'write': '可写',
            'read': '只读'}.get(str(value or '').strip().lower(),
                                str(value or ''))


def event_line(event: dict) -> str:
    icon = _ICONS.get(str(event.get('type') or ''), '•')
    # 与终端版共用 audit.EVENT_LABELS：两处都必须给人话，不能一处中文一处英文。
    import audit
    kind = str(event.get('type') or '')
    parts = [f"{icon} {_clock(event.get('ts'))}  "
             f"{audit.EVENT_LABELS.get(kind, kind)}"]
    detail = _detail(event)
    if detail:
        parts.append(detail)
    if event.get('title'):
        parts.append(f"会话: {event['title']}")
    return '\n'.join(parts)


def _detail(event: dict) -> str:
    """按事件类型挑最关键的几个字段，不做通用 repr（避免把大对象糊上去）。"""
    kind = str(event.get('type') or '')
    if kind in ('grant_added', 'grant_removed'):
        return f"{event.get('path', '')} [{_scope_zh(event.get('scope'))}]"
    if kind in ('domain_added', 'domain_removed'):
        return f"{_scope_zh(event.get('scope'))} {event.get('host', '')}"
    if kind.startswith('screen_'):
        bits = [str(value) for value in (
            event.get('region'), event.get('lang'),
            f"{event.get('chars')} 字" if event.get('chars') is not None else '',
            f"{event.get('width')}x{event.get('height')}"
            if event.get('width') else '',
        ) if value]
        return ' · '.join(bits)
    if kind == 'desktop_list_windows':
        return f"{event.get('count', '?')} 个窗口（共 {event.get('total', '?')}）"
    if kind.startswith('desktop_'):
        bits = [str(value) for value in (
            event.get('action'), event.get('process'),
            f"pid={event.get('pid')}" if event.get('pid') else '',
            f"{event.get('steps')} 步" if event.get('steps') else '',
            event.get('reason'),
        ) if value]
        return ' · '.join(bits)
    if kind.startswith('clipboard_'):
        bits = [f"{event.get('chars')} 字" if event.get('chars') is not None else '',
                '已截断' if event.get('truncated') else '']
        return ' · '.join(bit for bit in bits if bit)
    if kind == 'approval_auto':
        bits = [str(value) for value in (
            event.get('by'), event.get('tool'), event.get('reason')) if value]
        return ' · '.join(bits)
    if kind == 'approval_timeout':
        return f"{event.get('tool', '')} · 等待 {event.get('timeout_s', '')}s"
    if kind == 'policy_denied':
        # 被挡住的调用：先说工具与策略码，再给一句原因。
        # blocked_by=background 不是策略拒绝，而是「这条路径没法问人」，
        # 两者用同一个事件型但必须区分，否则用户会以为自己的策略拦了它。
        tool = event.get('tool') or 'tool'
        code = event.get('code') or ''
        if event.get('blocked_by') == 'background':
            return f'{tool} · {code} · 后台/定时回合不可用'
        reason = str(event.get('reason') or '')
        reason = reason[6:] if reason.startswith('Error: ') else reason
        head = f'{tool} · {code}' if code else tool
        return f'{head} · {reason[:80]}' if reason else head
    return ''


def audit_card(events: list[dict], *, token: str = '', intro: str = '',
               page: int = 0, counts: dict | None = None,
               truncated: bool = False) -> dict:
    elements: list[dict] = []
    if intro:
        elements.append(_text(intro))
    if not events:
        elements.append(_text(
            '（最近没有安全相关事件）\n'
            '这里会记录：审批超时、截屏/OCR、剪贴板、桌面操作、目录与域名授权变更。'))
        return {
            'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text', 'content': '审计'}},
            'elements': elements,
        }
    pages = max(1, (len(events) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, int(page or 0)), pages - 1)
    summary = ' · '.join(f'{name} {count}'
                         for name, count in sorted((counts or {}).items()))
    head = f'共 {len(events)} 条 · 第 {page + 1}/{pages} 页'
    if truncated:
        # 只说「共 N 条」会让人以为这就是全部；服务端有上限时必须说清楚。
        head += '\n（已达显示上限，更早的事件请用 /audit <类型> 缩小范围）'
    elements.append(_text(head + (f'\n{summary}' if summary else '')))
    for event in events[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        elements.append(_text(event_line(event)))
        elements.append({'tag': 'hr'})
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_page_button('← 较新', page=page - 1, token=token))
        if page + 1 < pages:
            nav.append(_page_button('较早 →', page=page + 1, token=token))
        if nav:
            elements.append({'tag': 'action', 'actions': nav})
    return {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': '审计'}},
        'elements': elements,
    }


def audit_text(events: list[dict]) -> str:
    """没有卡片能力时的纯文本回退。"""
    if not events:
        return '（最近没有安全相关事件）'
    return '\n'.join(event_line(event) for event in events[:20])
