"""审批卡片：把「多条待审批」合并成一张可原地更新的卡。

为什么合并：一次批量操作可能同时产生十几条审批请求（例如一串 `read`/`grep`），
一条一张卡会把聊天刷屏，也会让审批人看不到「总共还有几条没处理」。这里渲染成
一张卡：每条请求一段正文 + 自己的「允许 / 拒绝」按钮，底部可选「全部允许 / 全部拒绝」，
处理完一条就 PATCH 同一张卡（由网关维护 message_id）。

安全边界不变：
- 每条按钮仍然只带 `req_id`，归属校验由网关按「本聊天推送过的请求」判断；
- 「同类」按钮的判定键由服务端按登记内容重算，这里只传意图；
- 卡片只做展示，不缓存授权状态，也不会因为卡片旧就把请求当成已处理。
"""
from __future__ import annotations

import approval_view
from exec_policy import approval_class

# 一张卡最多展开几条完整详情：宁可提示“还有 N 条，回复 y2/n2”，也不做超大卡片。
MAX_DETAIL_ITEMS = 4
MAX_TOTAL_CHARS = 12_000
MAX_REASON_CHARS = 300


def _markdown(content: str) -> dict:
    return {'tag': 'markdown', 'content': content}


def _button(label: str, value: dict, *, kind: str = 'default') -> dict:
    return {'tag': 'button',
            'text': {'tag': 'plain_text', 'content': label},
            'type': kind,
            'value': value}


def _clip(text: str, limit: int) -> str:
    text = str(text or '')
    return text if len(text) <= limit else text[:limit] + '…'


def _request_block(req: dict, index: int, budget: int) -> tuple[str, list[dict]]:
    """单条请求的正文与按钮；预算用于控制整卡长度。"""
    tool = approval_view.safe_text(
        approval_view.tool_label(req.get('tool')))
    reason = _clip(approval_view.safe_text(req.get('reason') or ''), MAX_REASON_CHARS)
    detail = approval_view.detail(tool, req.get('args') or {})
    if detail and len(detail) > budget:
        detail = _clip(detail, budget)
    req_id = req['req_id']
    lines = [f'**#{index} · {tool}**']
    if reason:
        lines.append(f'原因：{reason}')
    if detail:
        lines.append(f'```\n{detail}\n```')
    buttons = [
        _button('✅ 允许', {'approval': req_id, 'allow': True}, kind='primary'),
    ]
    klass = approval_class(tool, req.get('args') or {})
    if klass:
        buttons.append(_button(f'📌 同类（{klass}）',
                               {'approval': req_id, 'allow': True,
                                'scope': 'session'}))
    buttons.append(_button('⛔ 拒绝', {'approval': req_id, 'allow': False},
                           kind='danger'))
    return '\n'.join(lines), buttons


def approval_card(requests: list[dict], *, show_bulk: bool = True) -> dict:
    """一张卡展示当前所有待审批请求。`requests` 按入队顺序（旧 → 新）。"""
    if not requests:
        return {
            'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text', 'content': '审批'},
                       'template': 'green'},
            'elements': [_markdown('✅ 待审批已全部处理完。')],
        }

    total = len(requests)
    elements: list[dict] = []
    used = 0
    shown = requests[:MAX_DETAIL_ITEMS]
    for index, req in enumerate(shown, 1):
        budget = max(400, MAX_TOTAL_CHARS - used)
        body, buttons = _request_block(req, index, budget)
        used += len(body)
        elements.append(_markdown(body))
        elements.append({'tag': 'action', 'actions': buttons})
        elements.append({'tag': 'hr'})

    rest = total - len(shown)
    if rest > 0:
        elements.append(_markdown(
            f'…另有 **{rest}** 条未展开：回复 `y序号` / `n序号` 按序号处理，'
            '或点下面的「全部允许 / 全部拒绝」。'))
    if show_bulk and total > 1:
        elements.append({'tag': 'action', 'actions': [
            _button(f'✅ 全部允许（{total}）', {'approval_bulk': 'allow'},
                    kind='primary'),
            _button('⛔ 全部拒绝', {'approval_bulk': 'deny'}, kind='danger'),
        ]})

    title = f'⚠ 需要审批（{total}）' if total > 1 else '⚠ 需要审批'
    return {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': title},
                   'template': 'orange'},
        'elements': elements,
    }


def approval_text(requests: list[dict]) -> str:
    """没有卡片能力时的纯文本回退（一条一段，按序号 y2/n2）。"""
    if not requests:
        return '✅ 待审批已全部处理完。'
    blocks = []
    for index, req in enumerate(requests, 1):
        tool = approval_view.safe_text(req.get('tool') or '工具')
        reason = _clip(approval_view.safe_text(req.get('reason') or ''), MAX_REASON_CHARS)
        detail = approval_view.detail(tool, req.get('args') or {})
        if detail:
            detail = _clip(detail, 600)
        tail = 'y' if index == 1 else f'y{index}'
        no = 'n' if index == 1 else f'n{index}'
        blocks.append(f'#{index} · {tool}\n原因: {reason}'
                      + (f'\n```\n{detail}\n```' if detail else '')
                      + f'\n回复 {tail} 允许 / {no} 拒绝')
    return chr(10).join(blocks[:MAX_DETAIL_ITEMS]) + (
        f'\n…另有 {len(requests) - MAX_DETAIL_ITEMS} 条，/perms 查看'
        if len(requests) > MAX_DETAIL_ITEMS else '')
