"""记忆的三个显式工具：remember / forget / recall。

为什么需要它们
--------------
``memory.py`` 的自动抽取每回合都在跑，机制完整；缺的是**用户/模型主动点名**
这条路径。没有它，用户说「记住我周三下午不看代码」，只能指望自动抽取碰巧抓到；
想删掉一条记错的，只能去 `memories/` 目录手动找文件。

对齐 Codex/ChatGPT 的两半设计：
- 自动那一半（后台抽取+合并）我们已有；
- 显式这一半（saved memories：能记、能查、能删）就是本模块。

三条设计约束
------------
1. ``forget`` 必须逐次审批：删除比写入更需确认，而且不可逆。
2. ``remember`` 的 type 收窄成四个语义明确的枚举，不接受任意字符串——
   自由文本型别会让记忆分类失去意义。
3. 所有写入都过 ``memory_redact``：记忆是长期状态，一条密钥进来会跟着每次召回。
"""
from __future__ import annotations

import re

#: 显式记忆的类型枚举。与自动抽取的 MEMORY_TYPES 不同：这四个是**语义**分类，
#: 直接对应"该不该长期记住"的判断依据。
#: - preference：用户的稳定偏好（"我周三不看代码"）
#: - correction：用户纠正过我（"不是 A 是 B"），优先级最高
#: - fact：花时间才查到的确定事实（"部署走 make ship-prod"）
#: - context：项目/环境的背景设定
TYPES = ('preference', 'correction', 'fact', 'context')


def _fmt_list(items, *, empty: str = '(none)') -> str:
    if not items:
        return empty
    return '\n'.join(items)


def remember(text: str, *, mem_type: str = 'preference', name: str = '',
             description: str = '') -> str:
    """写一条记忆。返回给模型看的结果（含文件名，便于后续 forget）。"""
    import memory

    body = ' '.join(str(text or '').split()) if '\n' not in str(text or '') else str(text).strip()
    if not body:
        return 'Error: text is required'
    kind = str(mem_type or 'preference').strip().lower()
    if kind not in TYPES:
        return (f'Error: type must be one of {", ".join(TYPES)}; got {mem_type!r}')

    # 名字缺省取正文前 24 字：用户不会为了记一条事专门起名。
    label = ' '.join(str(name or '').split()) or body[:24]
    desc = ' '.join(str(description or '').split()) or body[:80]

    try:
        # 来源：会话 id + 用户原话。没有它就只能猜「为什么记这个」——
        # `/why` 靠这两个字段回答。取不到会话时留空，不影响写入。
        session_id = ''
        try:
            import hooks
            session_id = hooks.current_session_id() or ''
        except Exception:
            pass
        path, action = memory.write_memory_file(
            label, kind, desc, body, source=session_id, quote=body[:200],
            confidence=_confidence_for(kind))
    except Exception as exc:
        return f'Error: could not save memory: {type(exc).__name__}: {exc}'
    if action == 'update':
        return (f'Updated memory "{label}" (type={kind}) in {path.name}: '
                'an existing note was close enough to merge into.')
    if action == 'supersede':
        return (f'Replaced the earlier note with memory "{label}" (type={kind}) '
                f'as {path.name}. The old one is archived, not deleted.')
    return (f'Saved memory "{label}" (type={kind}) as {path.name}. '
            'It will be recalled automatically in later sessions.')


def _confidence_for(kind: str) -> float:
    """来源决定初值：用户明确要求的比模型推测的可信。"""
    return 0.9 if kind in ('preference', 'correction') else 0.6


def forget(name: str) -> str:
    """删除一条记忆。这是不可逆操作，调用方负责先征得用户同意。"""
    import memory

    target = ' '.join(str(name or '').split())
    if not target:
        return 'Error: name is required'
    try:
        removed = memory.delete_memory_file(target)
    except Exception as exc:
        return f'Error: could not delete memory: {type(exc).__name__}: {exc}'
    if not removed:
        listing = memory.list_memory_files()
        names = [item.get('name') for item in listing if item.get('name')]
        return (f'Error: no memory named {target!r}. '
                f'Known memories: {", ".join(names) if names else "(none)"}')
    return f'Forgotten: {target}.'


def recall(query: str = '', *, limit: int = 10) -> str:
    """按关键词在记忆正文里查。空 query 列出全部（带类型与描述）。"""
    import memory

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return 'Error: limit must be an integer'
    limit = max(1, min(limit, 50))

    entries = memory.list_memory_files()
    if not entries:
        return '(no memories saved yet)'

    needle = str(query or '').strip().casefold()
    hits = []
    for item in entries:
        filename = item.get('filename') or item.get('name')
        if not filename:
            continue
        name = str(item.get('name') or filename)
        meta = f"type={item.get('type') or '?'} description={item.get('description') or ''}"
        if not needle:
            hits.append(f'- {name} ({meta})')
            continue
        # 名字/描述命中优先，正文再查一遍：用户常记得原话而不是标题。
        if needle in name.casefold() or needle in meta.casefold():
            hits.append(f'- {name} ({meta})')
            continue
        body = memory.read_memory_file(filename) or ''
        if needle in str(body).casefold():
            snippet = _snippet(str(body), needle)
            hits.append(f'- {name} ({meta})\n  … {snippet}')

    if not hits:
        # 给模型下一步：换个词，或者用空 query 看全部。
        return f'(no memories matching {query!r}; try a shorter keyword or an empty query to list all)'
    shown = hits[:limit]
    more = f'\n… {len(hits) - len(shown)} more match(es); raise limit or narrow the query.' if len(hits) > limit else ''
    return _fmt_list(shown, empty='(no memories saved yet)') + more


def _snippet(text: str, needle: str, *, width: int = 120) -> str:
    """命中位置前后各截一段，让模型不用再读一遍全文。"""
    flat = ' '.join(text.split())
    at = flat.casefold().find(needle)
    if at < 0:
        return flat[:width]
    start = max(0, at - width // 3)
    return ('…' if start else '') + flat[start:start + width] + '…'


def _slug_like(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.casefold()).strip('-')
