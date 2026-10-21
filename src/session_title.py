"""会话标题生成：取材、LLM 调用与本地兜底。

标题是副通道（housekeeping）的一次独立 LLM 调用，不属于主循环：失败时静默
退回本地兜底，绝不阻塞回复。放在 src 是因为 CLI、网关和 SDK 共用同一份逻辑；
client 由调用方注入，本模块不读取入口全局状态。
"""
from __future__ import annotations

import os

from session_store import is_injected_user_content


def generate_session_title(messages: list[dict], client) -> str:
    """Ask the configured model for a compact session topic, with a local fallback.

    取材是「会话在做什么」而非「最后说了什么」：首条有效 user（原始目标）+
    最近一条 assistant 结论 + 最近用过的工具名。旧实现 exchanges[-8:] 取尾部，
    长会话标题会变成最后一条助手回复的复述（实测 '工作区当前是干净的…'）。"""
    first_user = next((m for m in messages
                       if m.get('role') == 'user' and isinstance(m.get('content'), str)
                       and m['content'].strip()
                       and not is_injected_user_content(m['content'])), None)
    last_assistant_text = next((m['content'].strip() for m in reversed(messages)
                                if m.get('role') == 'assistant'
                                and isinstance(m.get('content'), str)
                                and m['content'].strip()), '')
    tool_names = []
    for m in reversed(messages):
        for call in m.get('tool_calls') or []:
            name = (call.get('function') or {}).get('name', '')
            if name and name not in tool_names:
                tool_names.append(name)
    has_tool_context = bool(tool_names)
    exchanges = []
    if first_user is not None:
        exchanges.append(f"user（目标）: {first_user['content'].strip()[:600]}")
    if last_assistant_text:
        exchanges.append(f"assistant（最近结论）: {last_assistant_text[:400]}")
    if tool_names:
        exchanges.append("最近使用的工具: " + ', '.join(tool_names[:8]))
    fallback = next((
        ' '.join(line.split()).strip()[:60]
        for message in messages
        if message.get('role') == 'user' and isinstance(message.get('content'), str)
        and not is_injected_user_content(message['content'])
        for line in message['content'].splitlines()
        if _is_title_candidate(line)
    ), None)
    if fallback is None:
        fallback = next((
            ' '.join(line.split()).strip()[:60]
            for message in messages
            if message.get('role') == 'assistant' and isinstance(message.get('content'), str)
            and not is_injected_user_content(message['content'])
            for line in message['content'].splitlines()
            if _is_title_candidate(line)
        ), '(untitled session)')
    if fallback == '(untitled session)' and not has_tool_context:
        return fallback
    if not exchanges:
        return '(untitled session)'
    try:
        response = client.chat.completions.create(
            model=os.getenv('LLM_MODEL_ID'),
            messages=[
                {'role': 'system', 'content': (
                    '为这段 coding agent 会话概括一个主题标题。只输出标题本体（名词短语，'
                    '不加句号、前缀或引号），使用用户原语言，不超过 20 个汉字或 8 个英文词。'
                    '概括的是整个会话在做什么，不是复述最后一条消息或结论。'
                    '忽略 <reminder>、<task_notification> 等系统注入和“继续、好的、go”等确认语；'
                    '如果记录没有提供足够信息，只输出 (untitled session)，不要猜测。'
                )},
                {'role': 'user', 'content': '\n'.join(exchanges)},
            ],
            temperature=0,
            # 思考模式（deepseek-flash 默认）reasoning 先于正文消耗预算，
            # 40 会得到空 content——标题静默退回本地兜底
            max_tokens=8000,
        )
        title = (response.choices[0].message.content or '').strip().strip('"“”\'')
        if title:
            return ' '.join(title.split())[:60]
    except Exception as exc:
        print(f'  [session title] using local fallback: {exc}')
    return fallback


def _is_title_candidate(line: str) -> bool:
    """本地兜底的可选行：过滤系统注入、极短行和整行重复的确认语。"""
    text = ' '.join(line.split()).strip()
    normalized = text.casefold()
    if len(text) < 12 or text.startswith(('[', '<', '- out:')):
        return False
    if normalized.startswith((
        'tool results executed before compaction:',
        'done after compact',
        'final answer',
        'recovered',
        '[scheduled]',
    )):
        return False
    words = normalized.split()
    return not (len(words) >= 4 and len(set(words)) == 1)
