"""Composer 补全候选：`/` 命令与 `@` 文件（纯函数，便于测试）。"""
from __future__ import annotations

from file_mentions import format_mention, match_mentions, mention_token


def suggestions(text: str, files: list[str],
                limit: int = 6) -> list[tuple[str, str]]:
    """返回 (插入值, 展示文本)；`/` 给命令，`@` 给文件，其它输入为空。"""
    value = text or ''
    if value.startswith('/'):
        from ui import commands
        query = value[1:].strip()
        return [(f'/{spec.name}',
                 f'/{spec.name}  {spec.description}')
                for spec in commands.matches(query)
                if spec.ported][:limit]
    mention = mention_token(value)
    if mention is None:
        return []
    return [(format_mention(path), path)
            for path in match_mentions(files, mention[1], limit)]


def apply_suggestion(text: str, value: str) -> str:
    """把当前候选替换进输入文本（命令名整体替换，@ 只替换 token）。"""
    if (text or '').startswith('/'):
        return value
    mention = mention_token(text or '')
    if mention is None:
        return value
    return f'{text[:mention[0]]}{value}'
