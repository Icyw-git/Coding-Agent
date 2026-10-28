"""记忆的 durability 判定：一条笔记该不该提升为长期记忆（A5）。

为什么要分两层
--------------
PAHF 的做法是 session notes 合并后清除，只有 durable 的才进 global memory。
它给的例子很说明问题：一趟旅行里用户说"这次不想托运行李"——

> The second note was intentionally discarded because it was **explicitly scoped
> to that specific trip** and was not considered durable.

如果这类一次性上下文进了长期记忆，之后每次订票都会被它干扰。

为什么用确定性规则而不是让模型判
--------------------------------
- 成本：一次判断一次调用，而写入是高频操作。
- 可解释：用户问"为什么这条没进长期记忆"时，要给得出理由。
- 宁可留：漏判只是多一条冗余（合并机制会处理），误判是把有用信息丢掉。

所以规则保守：**只有明确说"这次/本次"才降级**，其余默认提升。真正的质量把关
交给异步评测（`memory_eval.judge_quality`），它能在真实使用后给出 keep/drop
的统计，而不是在写入时赌一把。
"""
from __future__ import annotations

import re

#: 明确限定在单次事件的措辞。命中即判为非 durable。
_EPHEMERAL = (
    '这次', '本次', '这一回', '这个任务', '当前任务', '今天先',
    'temporarily', 'for now', 'this time', 'this trip', 'this task',
    'just this once',
)

#: 明确表达长期性的措辞。命中即判为 durable（强信号）。
_DURABLE = (
    '以后', '以后都', '今后', '总是', '一直', '每次', '每次都', '平时',
    '我一向', '我习惯', '始终',
    'always', 'usually', 'every time', 'from now on', 'going forward',
    'by default', 'normally',
)

#: 条件性偏好（`when X`）天然是 durable：条件会重复出现。
_CONDITION = re.compile(r'(?i)\bwhen\s+\S+|的时候|时更|的话')

#: 时间点类事实（版本号、临时路径）倾向非 durable。
#: 只认明确写法，避免把"90 天轮换"这类长期规则误判。
_STALE_MARKERS = (
    '临时', '暂时', 'temporarily', 'for now',
)


def classify(entry: dict) -> tuple[str, str]:
    """返回 ``(scope, reason)``。scope 是 ``'session'`` 或 ``'global'``。

    顺序有意义：先看短暂性（最强否定），再看长期性，最后看条件与默认。
    """
    text = ' '.join([
        str(entry.get('description') or ''),
        str(entry.get('body') or ''),
    ]).casefold()

    for marker in _EPHEMERAL:
        if marker in text:
            return 'session', f'explicitly scoped to one event ({marker!r})'
    for marker in _STALE_MARKERS:
        if marker in text:
            return 'session', f'temporary wording ({marker!r})'
    for marker in _DURABLE:
        if marker in text:
            return 'global', f'durable wording ({marker!r})'
    if _CONDITION.search(text):
        return 'global', 'conditional preference repeats'
    # 默认提升：漏判只是多一条冗余，误判会丢有用信息。
    return 'global', 'default (no one-off marker)'


def apply(entry: dict) -> dict:
    """如果条目没显式指定 scope，就按 durability 判定填入。

    显式指定的（用户用 `remember` 时、或抽取模型明确标了）不覆盖——人比规则准。
    """
    if str(entry.get('scope') or '').strip():
        return entry
    scope, reason = classify(entry)
    return {**entry, 'scope': scope, 'scope_reason': reason}


def session_only(entries: list[dict]) -> list[dict]:
    """挑出仍停在 session 层的条目（合并时用来决定谁该被丢弃）。"""
    return [item for item in entries if item.get('scope') == 'session']
