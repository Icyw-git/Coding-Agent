"""目录级写授权卡片：列出持久授权，每个目录一个「撤销」按钮。

授权本身只能由人给出（``/grant add`` 或 API），卡片只负责查看与收回。按钮
``value`` 带每次渲染的短 token；网关校验 token 属于本聊天且未过期，避免旧卡片
或别的聊天重放按钮。路径可能很长，所以每条单独一段并分页。
"""

PAGE_SIZE = 4


def _text(content: str) -> dict:
    return {'tag': 'div',
            'text': {'tag': 'plain_text', 'content': content}}


def _button(label: str, *, action: str, path: str, token: str) -> dict:
    return {'tag': 'button',
            'text': {'tag': 'plain_text', 'content': label},
            'type': 'danger',
            'value': {'grant_action': action, 'grant_path': path,
                      'grant_view': token}}


def _page_button(label: str, *, page: int, token: str) -> dict:
    return {'tag': 'button',
            'text': {'tag': 'plain_text', 'content': label},
            'type': 'default',
            'value': {'grant_action': 'page', 'page': page,
                      'grant_view': token}}


def _expiry_label(row: dict) -> str:
    expires = str(row.get('expires') or '')
    if not expires:
        return '永久'
    if row.get('active'):
        return f'有效至 {expires}'
    return f'已过期（{expires}）'


def _row_lines(row: dict) -> list[str]:
    lines = [str(row.get('path') or ''),
             f"{_expiry_label(row)} · scope={row.get('scope') or 'write'}"]
    by = str(row.get('created_by') or '')
    note = ' '.join(str(row.get('note') or '').split())
    meta = f'由 {by} 添加' if by else ''
    if note:
        meta = f'{meta} · {note}' if meta else note
    if meta:
        lines.append(meta)
    return lines


def grant_card(rows: list[dict], *, token: str = '', intro: str = '',
               page: int = 0) -> dict:
    """目录写授权卡片；过期记录仍列出，方便手动清掉。"""
    elements: list[dict] = []
    if intro:
        elements.append(_text(intro))
    if not rows:
        elements.append(_text(
            '（没有目录写授权）\n'
            '工作区外的写入仍会逐次询问。需要长期放行某个目录时用：\n'
            '/grant add "D:\\notes" [until 2026-12-31] [note 备注]'))
        return {
            'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text', 'content': '目录写授权'}},
            'elements': elements,
        }
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, int(page or 0)), pages - 1)
    elements.append(_text(
        f'共 {len(rows)} 条 · 第 {page + 1}/{pages} 页\n'
        '授权只放宽「工作区外写入」的审批；秘密文件与系统目录仍硬拒，'
        'read-only 档仍要审批。'))
    for row in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        path = str(row.get('path') or '')
        elements.append(_text(chr(10).join(_row_lines(row))))
        elements.append({'tag': 'action', 'actions': [
            _button('撤销授权', action='remove', path=path, token=token)]})
        elements.append({'tag': 'hr'})
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_page_button('← 上一页', page=page - 1, token=token))
        if page + 1 < pages:
            nav.append(_page_button('下一页 →', page=page + 1, token=token))
        if nav:
            elements.append({'tag': 'action', 'actions': nav})
            elements.append(_text(f'第 {page + 1}/{pages} 页'))
    return {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': '目录写授权'}},
        'elements': elements,
    }


def grant_text(rows: list[dict]) -> str:
    """没有卡片能力时的纯文本回退。"""
    if not rows:
        return '（没有目录写授权）'
    lines = ['目录写授权:']
    for row in rows:
        lines.append(f"  {row.get('path')}  [{_expiry_label(row)}]")
    return chr(10).join(lines)
