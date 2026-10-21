"""Composer 补全候选：`/` 命令与 `@` 文件（纯函数，便于测试）。"""
from __future__ import annotations

from file_mentions import format_mention, match_mentions, mention_token


def suggestions(text: str, files: list[str],
                limit: int = 6) -> list[tuple[str, str]]:
    """返回 (插入值, 展示文本)；`/` 给命令，`@` 给文件，其它输入为空。"""
    value = text or ''
    if value.startswith('/'):
        from ui import commands
        body = value[1:]
        if ' ' in body:
            name, argument = body.split(' ', 1)
            spec = commands.find(name)
            if spec is None:
                return []
            return commands.argument_suggestions(spec, argument)[:limit]
        items = []
        for spec in commands.matches(body):
            if not spec.ported:
                continue
            suffix = ' ' if spec.argument_hint else ''
            label = f'/{spec.name}  {spec.description}'
            if spec.argument_hint:
                label += f'  {spec.argument_hint}'
            items.append((f'/{spec.name}{suffix}', label))
        return items[:limit]
    mention = mention_token(value)
    if mention is None:
        return []
    return [(format_mention(path), path)
            for path in match_mentions(files, mention[1], limit)]


def apply_suggestion(text: str, value: str) -> str:
    """把当前候选替换进输入文本（命令名整体替换，@ 只替换 token）。"""
    if (text or '').startswith('/'):
        space = (text or '').find(' ')
        if space == -1:
            return value
        return f'{text[:space + 1]}{value}'
    mention = mention_token(text or '')
    if mention is None:
        return value
    return f'{text[:mention[0]]}{value}'
