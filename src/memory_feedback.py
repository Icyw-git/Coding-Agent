"""post-action 反馈信号：从「模型做了什么 → 用户怎么反应」里学，而不只是读用户说了什么。

为什么需要
----------
PAHF 论文的结论：持续个性化的三步循环里，

1. 行动前澄清歧义
2. 用记忆里的偏好约束行动
3. **行动后收到反馈，写回记忆纠正偏差**

> The final and most critical step is Post-Action Feedback Integration. This
> reactive loop is the essential mechanism for correcting miscalibrated beliefs
> and adapting to non-stationarity, **which pre-action queries alone cannot solve**.

论文的例子：第一天记「Kate 喜欢可乐」，第二天她困了要饮料、送上可乐、她说
「我困的时候更想喝茶」——**只有第三条信息能纠正记忆**。只记「喜欢可乐」会永久错。

我们原来的 `extract_memories` 只拼对话文本（`f'{role}: {content}'`），工具结果
虽然也在 messages 里，但没有和「用户随后说了什么」配对。这个模块补的就是那条链。

设计约束
--------
- **纯本地、零模型调用**：只做确定性匹配。判定错了顶多少一条信号，不会污染记忆。
- **只产出提示，不直接写记忆**：信号拼进抽取提示词，由记忆层决定记不记——避免
  「两处都在判断什么值得记」的分裂。
- **宁少不多**：纠正信号要窄（明确否定词），正反馈信号要宽（但权重低）。理由是
  误判"这是纠正"会覆盖掉正确记忆，代价远高于漏掉一条。
"""
from __future__ import annotations

#: 明确的否定/纠正用语。窄名单：宁可漏判，不可误判——误判会把正确记忆覆盖掉。
CORRECTION_PATTERNS = (
    '不对', '不是', '错了', '搞错', '重来', '重新来', '我说的是', '不是这样',
    '别这样', '不要这样', '我刚才说', '理解错', '你弄错', '改一下', '应该是',
    'no,', 'nope', 'not what i', 'that is wrong', "that's wrong", 'redo',
    'try again', 'i said', 'actually,',
)

#: 正反馈。宽一些无妨：低权重，只用来说明"这个做法可以保留"。
#: 注意这是**整句**匹配（见 _is_approval），不是前缀匹配：
#: “好主意”是评价提议，“可以吗”是疑问，“好的话说回来…”是转折——都不是满意。
APPROVAL_PATTERNS = (
    '好', '好的', '可以', '就这样', '对', '行', '不错', '很好', '谢谢',
    '辛苦了', '完美', 'ok', 'okay', 'good', 'great', 'perfect', 'nice',
    'thanks', 'thank you', 'lgtm', 'looks good',
)

#: 条件性偏好修正：用户不是否定，而是**补充条件**。
#: PAHF 论文的核心例子「我困的时候更想喝茶」就属于这一类——它不含任何否定词，
#: 但如果只记前一天的「喜欢可乐」，第二天就会永久错。
REFINE_PATTERNS = (
    '的时候', '的话', '时更', '更想', '更喜欢', '改成', '换成', '还是用',
    '下次', '以后', '以后都', 'prefer', 'instead', 'next time', 'from now on',
    'when i',
)

#: 隐式纠正：用户改了模型刚写过的文件（由工具调用序列推断）。
_WRITE_TOOLS = frozenset({'write', 'edit', 'apply_patch'})

#: 单条消息最多取多少字作证据，避免把整段对话塞进提示词。
QUOTE_CHARS = 160
#: 最多产出几条信号。
MAX_SIGNALS = 6


def _text_of(content) -> str:
    """把 content（可能是 str 或多模态 block 列表）压成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            text = getattr(block, 'text', None)
            if text is None and isinstance(block, dict):
                text = block.get('text')
            if text:
                parts.append(str(text))
        return ' '.join(parts)
    return ''


def _quote(text: str) -> str:
    flat = ' '.join(str(text or '').split())
    return flat[:QUOTE_CHARS] + ('…' if len(flat) > QUOTE_CHARS else '')


def _is_correction(text: str) -> bool:
    low = text.casefold()
    return any(token in low for token in CORRECTION_PATTERNS)


def _is_approval(text: str) -> bool:
    """是否是一句单纯的认可。

    必须是**整句**匹配（允许末尾的标点/语气词），不能用前缀：
    前缀匹配会把“好主意”（评价提议）、“可以吗”（疑问）、
    “好的话说回来…”（转折）都当成满意。
    """
    low = str(text or '').strip().casefold()
    if not low or len(low) > 24:
        return False
    # 去掉末尾标点与常见语气词，再整体比对
    trimmed = low.rstrip('。！!？?.~ 呀啊的呢')
    if trimmed in APPROVAL_PATTERNS:
        return True
    # 允许“好的，谢谢”这类两词组合（都在名单内）
    pieces = [p.strip('。！!？?.~ ') for p in trimmed.replace('，', ',').split(',')]
    pieces = [p for p in pieces if p]
    return bool(pieces) and all(p in APPROVAL_PATTERNS for p in pieces)


def _is_refine(text: str) -> bool:
    """用户是否在补充/收窄一个偏好（而非否定它）。"""
    low = str(text or '').casefold()
    return any(token in low for token in REFINE_PATTERNS)


def _tool_outcomes(messages: list) -> list[dict]:
    """本回合的工具调用结果（成功/失败），按出现顺序。"""
    out = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get('role') != 'tool':
            continue
        body = _text_of(msg.get('content'))
        failed = body.lstrip().lower().startswith('error')
        out.append({'failed': failed, 'excerpt': _quote(body)})
    return out


def _written_paths(messages: list) -> list[str]:
    """本回合被写过的路径（用来判断用户是否改了模型刚写的东西）。"""
    paths = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            continue
        for call in (msg.get('tool_calls') or []):
            name = ''
            args = {}
            fn = call.get('function') if isinstance(call, dict) else None
            if isinstance(fn, dict):
                name = str(fn.get('name') or '')
                raw = fn.get('arguments')
                if isinstance(raw, str):
                    try:
                        import json
                        args = json.loads(raw) or {}
                    except (ValueError, TypeError):
                        args = {}
                elif isinstance(raw, dict):
                    args = raw
            if name in _WRITE_TOOLS and isinstance(args, dict):
                path = str(args.get('path') or '').strip()
                if path:
                    paths.append(path)
    return paths


def collect(messages: list) -> list[dict]:
    """从一条消息流里提炼 post-action 反馈信号。

    返回 ``[{'kind': ..., 'quote': ..., 'weight': ...}]``，按价值排序。
    kind 取 ``correction`` / ``rewrite`` / ``failure`` / ``approval``。
    """
    if not messages:
        return []
    signals: list[dict] = []
    written = _written_paths(messages)

    # 1) 显式纠正：用户紧接着说了否定词。最高价值。
    for index, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        body = _text_of(msg.get('content'))
        if not body.strip():
            continue
        # 只在「前面有 assistant/tool 动作」时才可能是纠正——首轮消息不是纠正。
        if index < 2:
            continue
        if _is_correction(body):
            signals.append({'kind': 'correction', 'quote': _quote(body),
                            'weight': 'high'})
        elif _is_refine(body):
            # 不是否定而是补充条件（「我困的时候更想喝茶」）：
            # 这类信号必须记成**带条件的偏好**，而不是覆盖成无条件偏好。
            signals.append({'kind': 'refine', 'quote': _quote(body),
                            'weight': 'high'})

    # 2) 隐式纠正：用户写入的路径里，有模型刚刚写过的。
    user_paths = set()
    for msg in messages:
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        for call in (msg.get('tool_calls') or []):
            fn = call.get('function') if isinstance(call, dict) else None
            if isinstance(fn, dict) and str(fn.get('name') or '') in _WRITE_TOOLS:
                user_paths.add(str(fn.get('name')))
    if written and user_paths:
        # 用户在本回合也用了写工具，且模型此前写过东西：可能是在改写模型的结果。
        signals.append({'kind': 'rewrite',
                        'quote': f'user also wrote after assistant wrote {written[0]}',
                        'weight': 'high'})

    # 3) 失败反馈：工具报错后用户换了做法。
    outcomes = _tool_outcomes(messages)
    if any(item['failed'] for item in outcomes):
        failed = next(item for item in outcomes if item['failed'])
        signals.append({'kind': 'failure', 'quote': failed['excerpt'],
                        'weight': 'medium'})

    # 4) 正反馈：低权重，只说明"这个做法可以保留"。
    for msg in messages:
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        body = _text_of(msg.get('content'))
        if body and _is_approval(body):
            signals.append({'kind': 'approval', 'quote': _quote(body),
                            'weight': 'low'})

    # correction/rewrite 优先；同 kind 去重
    order = {'correction': 0, 'refine': 1, 'rewrite': 2, 'failure': 3,
             'approval': 4}
    seen = set()
    unique = []
    for item in sorted(signals, key=lambda x: order.get(x['kind'], 9)):
        key = (item['kind'], item['quote'])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:MAX_SIGNALS]


def as_prompt_block(signals: list[dict]) -> str:
    """把信号拼成给抽取提示词的一段文本。无信号返回空串。"""
    if not signals:
        return ''
    lines = ['Observed post-action feedback in this turn:']
    for item in signals:
        label = {
            'correction': 'USER CORRECTED the assistant',
            'refine': 'user NARROWED/QUALIFIED a preference',
            'rewrite': 'user rewrote what the assistant produced',
            'failure': 'a tool call failed',
            'approval': 'user accepted the result',
        }.get(item['kind'], item['kind'])
        lines.append(f'- [{item["weight"]}] {label}: "{item["quote"]}"')
    lines.append(
        'When the user corrected or rewrote something, prefer producing a '
        '"correction" memory that REPLACES the earlier wrong assumption, rather '
        'than a separate preference that coexists with it.')
    if any(item['kind'] == 'refine' for item in signals):
        lines.append(
            'When the user qualified a preference with a condition (e.g. '
            '"when I am sleepy I prefer tea"), record it as a CONDITIONAL '
            'preference in the form "when <condition>: <preference>" — do NOT '
            'replace the unconditional one, and do NOT drop the condition.')
    return '\n'.join(lines)
