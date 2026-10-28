"""域名策略卡片：列出 allow / deny 名单，每条一个「撤销」按钮。

与目录授权卡片的差别是安全含义：域名规则放宽的是**外发边界**，所以卡片上
显式区分 allow/deny，并在 deny 行提示它连 `danger-full-access` 也绕不过。
按钮 value 带每次渲染的短 token；网关校验 token 属于本聊天且未过期。
"""
from __future__ import annotations

PAGE_SIZE = 6


def _text(content: str) -> dict:
    return {'tag': 'div',
            'text': {'tag': 'plain_text', 'content': content}}


def _button(label: str, *, host: str, token: str) -> dict:
    return {'tag': 'button',
            'text': {'tag': 'plain_text', 'content': label},
            'type': 'danger',
            'value': {'domain_action': 'remove', 'domain_host': host,
                      'domain_view': token}}


def _page_button(label: str, *, page: int, token: str) -> dict:
    return {'tag': 'button',
            'text': {'tag': 'plain_text', 'content': label},
            'type': 'default',
            'value': {'domain_action': 'page', 'page': page,
                      'domain_view': token}}


def _expiry_label(row: dict) -> str:
    # 唯一实现在 net_grants：自己按日期算，不看调用方是否传了 active。
    import net_grants
    return net_grants.expiry_label(row)


def _row_lines(row: dict) -> list[str]:
    host = str(row.get('host') or '')
    scope = str(row.get('scope') or 'allow')
    marker = '⛔ 拒绝' if scope == 'deny' else '✅ 允许'
    lines = [f'{marker}  {host}', _expiry_label(row)]
    by = str(row.get('created_by') or '')
    note = ' '.join(str(row.get('note') or '').split())
    meta = f'由 {by} 添加' if by else ''
    if note:
        meta = f'{meta} · {note}' if meta else note
    if meta:
        lines.append(meta)
    return lines


def domain_card(rows: list[dict], *, token: str = '', intro: str = '',
                page: int = 0) -> dict:
    elements: list[dict] = []
    if intro:
        elements.append(_text(intro))
    if not rows:
        elements.append(_text(
            '（没有域名规则）\n'
            '联网请求仍会逐次询问。需要长期放行某个站点时用：\n'
            '/domains allow "*.example.com" [until 2026-12-31] [note 备注]\n'
            '/domains deny ads.example.com'))
        return {
            'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text', 'content': '域名策略'}},
            'elements': elements,
        }
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, int(page or 0)), pages - 1)
    allow = sum(1 for row in rows if row.get('scope') != 'deny')
    deny = len(rows) - allow
    elements.append(_text(
        f'共 {len(rows)} 条（允许 {allow} · 拒绝 {deny}）· 第 {page + 1}/{pages} 页\n'
        '「允许」只免除出口审批，仍受私网拦截与 URL 校验；'
        '「拒绝」是硬拒，任何档位都绕不过。\n'
        '通配只匹配子域：*.example.com 不含 example.com 本身。'))
    for row in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        elements.append(_text(chr(10).join(_row_lines(row))))
        elements.append({'tag': 'action', 'actions': [
            _button('撤销规则', host=str(row.get('host') or ''), token=token)]})
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
        'header': {'title': {'tag': 'plain_text', 'content': '域名策略'}},
        'elements': elements,
    }


def domain_text(rows: list[dict]) -> str:
    """没有卡片能力时的纯文本回退。"""
    if not rows:
        return '（没有域名规则；联网请求仍逐个审批）'
    lines = ['域名策略：']
    for row in rows:
        # 与终端版（terminal_views.domains）一致：用中文，不暴露 allow/deny 字面量。
        marker = '拒绝' if row.get('scope') == 'deny' else '允许'
        lines.append(f"  {marker} {row.get('host')}  [{_expiry_label(row)}]")
    return chr(10).join(lines)
