"""建议卡片：手机上「点开始才执行」。

为什么需要它
------------
TUI 里 `/suggest start <编号>` 已经能采纳，但手机上没法输入编号——而且**建议一旦发到
飞书，就必须能当场决定**，否则它只是一条打扰。这张卡把每条建议变成两个按钮：

- 「开始」：走**正常受理**（和用户自己发一条消息同一条路，照旧审批）；
- 「不用了」：只记忽略，**不执行任何东西**。

卡片只是展示层：状态、采纳、忽略都走 `proactive_suggestions`，不缓存、不推断。
按钮上的 token 只携带建议 id，接收人由网关按当前聊天校验（防止卡片被转发后别人点）。
"""
from __future__ import annotations

MAX_ITEMS = 5
MAX_ACTION_CHARS = 120


def _text(content: str) -> dict:
    return {'tag': 'div',
            'text': {'tag': 'plain_text', 'content': str(content)}}


def _button(label: str, value: dict, *, kind: str = 'default') -> dict:
    return {'tag': 'button',
            'text': {'tag': 'plain_text', 'content': label},
            'type': kind,
            'value': value}


def _one_line(text: str) -> str:
    collapsed = ' '.join(str(text or '').split())
    if len(collapsed) > MAX_ACTION_CHARS:
        return collapsed[:MAX_ACTION_CHARS - 1] + '…'
    return collapsed


def suggestions_card(rows: list[dict], *, header: str = '') -> dict:
    """把待处理的建议渲染成卡片；没有建议时给一句可执行的说明。"""
    elements: list[dict] = []
    if header:
        elements.append(_text(header))
    if not rows:
        elements.append(_text('现在没有待处理的建议。'))
        elements.append(_button('再想几条', {'suggest_action': 'refresh'},
                                kind='primary'))
    else:
        elements.append(_text('这些是按当前状态想到的。点「开始」才会真的去做，'
                              '该确认的照旧会问你。'))
        for index, row in enumerate(rows[:MAX_ITEMS], 1):
            action = _one_line(row.get('proposed_action'))
            elements.append(_text(f'{index}. {action}'))
            elements.append({
                'tag': 'action',
                'actions': [
                    _button('开始', {'suggest_action': 'start',
                                    'suggestion_id': str(row.get('id') or '')},
                            kind='primary'),
                    _button('不用了', {'suggest_action': 'skip',
                                      'suggestion_id': str(row.get('id') or '')},
                            kind='default'),
                ],
            })
        if len(rows) > MAX_ITEMS:
            elements.append(_text(f'… 另有 {len(rows) - MAX_ITEMS} 条，'
                                  '说「还有哪些建议」可以继续看。'))
    return {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': '💡 建议'},
                   'template': 'blue'},
        'elements': elements,
    }


def suggestions_text(rows: list[dict]) -> str:
    """卡片发不出去时的纯文本兜底。"""
    if not rows:
        return '💡 建议\n现在没有待处理的建议。'
    lines = ['💡 建议（点开始才会执行）']
    for index, row in enumerate(rows[:MAX_ITEMS], 1):
        lines.append(f'{index}. {_one_line(row.get("proposed_action"))}')
    if len(rows) > MAX_ITEMS:
        lines.append(f'… 另有 {len(rows) - MAX_ITEMS} 条')
    return '\n'.join(lines)
