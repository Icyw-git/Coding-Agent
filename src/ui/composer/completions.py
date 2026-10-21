"""Composer 补全候选：`/` 命令与 `@` 文件（纯函数，便于测试）。"""
from __future__ import annotations

from file_mentions import format_mention, match_mentions, mention_token

TUI_COMMANDS = frozenset({
    'help', '?', 'status', 'model', 'pwd', 'quit', 'exit', 'q',
    'clear', 'export', 'sessions', 'transcripts', 'resume', 'more', 'older',
    'allow', 'deny', 'ask', 'search', 'failures', 'failed', 'all',
    'show-all',
})


def suggestions(text: str, files: list[str],
                limit: int = 6) -> list[tuple[str, str]]:
    """返回 (插入值, 展示文本)；`/` 给命令，`@` 给文件，其它输入为空。"""
    value = text or ''
    if value.startswith('/'):
        from cli_commands import COMMANDS
        typed = value.strip().lower()
        items = []
        for command in COMMANDS:
            names = (command.name, *command.aliases)
            supported = any(name.lstrip('/') in TUI_COMMANDS for name in names)
            if supported and any(name.startswith(typed) for name in names):
                items.append((command.name,
                              f'{command.name}  {command.description}'))
            if len(items) >= limit:
                break
        return items
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
