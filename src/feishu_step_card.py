"""飞书 schema 2.0「任务过程」卡片：答案 + 可折叠的工具摘要。

结构参考 dsh-im 的流式过程卡（`node_modules/@xmanrui/dsh-im/src/channels/feishu/
feishu-cards.mjs`）：`schema: "2.0"`、`body.elements`、`markdown` / `collapsible_panel`
元素。与旧版 `interactive` 卡片（`div` + `lark_md`）互不影响：会话/审批卡片保持原样，
只有 agent 回复用这张过程卡。

为什么单独一个模块：卡片 schema、图标 token、折叠层级都是平台细节，放在网关里会
和消息编排混在一起。这里只负责「给定答案与工具摘要 → 卡片 JSON」。
"""
from __future__ import annotations

MAX_ANSWER_CHARS = 18_000
MAX_TOOL_LINES = 40

_PANEL_ICON = {'tag': 'standard_icon', 'token': 'down-small-ccm_outlined',
               'color': 'grey', 'size': '16px 16px'}
_PANEL_BORDER = {'color': 'grey', 'corner_radius': '8px'}


def _plain(content: str) -> dict:
    return {'tag': 'plain_text', 'content': str(content)}


def _markdown(content: str) -> dict:
    return {'tag': 'markdown', 'content': str(content)}


def _panel(title: str, lines: list[str], *, expanded: bool = False) -> dict:
    return {
        'tag': 'collapsible_panel',
        'expanded': bool(expanded),
        'background_color': 'grey-50',
        'border': dict(_PANEL_BORDER),
        'padding': '8px 8px 8px 8px',
        'header': {
            'title': _plain(title),
            'vertical_align': 'center',
            'padding': '8px 8px 8px 8px',
            'icon': dict(_PANEL_ICON),
            'icon_position': 'right',
            'icon_expanded_angle': -180,
        },
        'elements': [_markdown(chr(10).join(lines))],
    }


def menu_row() -> dict:
    """完成后卡片底部的常驻入口：随时打开控制台。"""
    return {
        'tag': 'column_set',
        'flex_mode': 'none',
        'columns': [{
            'tag': 'column',
            'width': 'weighted',
            'weight': 1,
            'elements': [{
                'tag': 'button',
                'text': _plain('⚙️ 控制台'),
                'type': 'default',
                'width': 'fill',
                'behaviors': [{'type': 'callback', 'value': {'panel': 'menu'}}],
            }],
        }],
    }


def tool_lines(tool_counts: dict[str, int] | None,
               tool_errors: list[dict] | None,
               *, total_ms: float = 0) -> list[str]:
    """把本回合工具调用整理成面板里的行：`bash × 5 · 1.2s`。"""
    counts = dict(tool_counts or {})
    lines = []
    for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f'`{name}` × {count}')
    if total_ms:
        lines.append(f'累计耗时 {total_ms / 1000:.1f}s')
    for item in (tool_errors or [])[:MAX_TOOL_LINES]:
        lines.append(f"⚠️ `{item.get('tool', 'tool')}` — {item.get('error', 'error')}")
    return lines[:MAX_TOOL_LINES]


def batch_lines(batch: dict) -> list[str]:
    """一个工具批次的行：名称 × 次数；失败批次附带错误类型。"""
    counts: dict[str, int] = {}
    for name in batch.get('tools') or []:
        counts[name] = counts.get(name, 0) + 1
    lines = [f'`{name}` × {count}' if count > 1 else f'`{name}`'
             for name, count in counts.items()]
    if batch.get('status') == 'error':
        lines.append('⚠️ ' + str(batch.get('error_type') or 'error'))
    return lines


def step_card(*, answer: str = '', status: str = 'completed',
              tool_counts: dict[str, int] | None = None,
              tool_errors: list[dict] | None = None, total_ms: float = 0,
              tool_batches: list[dict] | None = None,
              note: str = '') -> dict:
    """生成回复卡：答案在上，工具摘要折叠进「过程详情」。"""
    elements: list[dict] = []
    text = str(answer or '').strip()
    if text:
        elements.append(_markdown(text[:MAX_ANSWER_CHARS]))
    elif status == 'running':
        elements.append(_markdown('_正在处理…_'))

    batches = [batch for batch in (tool_batches or []) if batch.get('tools')]
    panels = []
    if batches:
        for index, batch in enumerate(batches[:6], 1):
            count = len(batch.get('tools') or [])
            if len(batches) == 1:
                label = f'🛠️ 工具摘要（{count}）'
            else:
                label = f'🛠️ 工具摘要 {index}（{count}）'
            panels.append(_panel(label, batch_lines(batch)))
    else:
        lines = tool_lines(tool_counts, tool_errors, total_ms=total_ms or 0)
        if lines:
            count = sum(int(value or 0) for value in (tool_counts or {}).values())
            panels.append(_panel(f'🛠️ 工具摘要（{count or len(lines)}）', lines))

    if panels:
        if status == 'running':
            elements.extend(panels)
        else:
            # 完成后整块折叠，手机上一眼只看答案与自己关心的细节
            elements.append({
                'tag': 'collapsible_panel',
                'expanded': False,
                'background_color': 'grey-50',
                'border': dict(_PANEL_BORDER),
                'padding': '8px 8px 8px 8px',
                'header': {
                    'title': _plain('📋 过程详情'),
                    'vertical_align': 'center',
                    'padding': '8px 8px 8px 8px',
                    'icon': dict(_PANEL_ICON),
                    'icon_position': 'right',
                    'icon_expanded_angle': -180,
                },
                'elements': panels,
            })

    if note:
        elements.append(_markdown(f'_{note}_'))
    if not elements:
        elements.append(_markdown(' '))
    return {
        'schema': '2.0',
        'header': {'title': _plain('⚙️ 任务过程'), 'template': 'blue'},
        'body': {'elements': elements},
    }
