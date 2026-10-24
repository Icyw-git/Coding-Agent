"""Rich 渲染辅助：Markdown 兜底、流式代码围栏与按宽度复用的 Console。

虚拟视口只接受固定宽度的 `Text` 行，所以渲染入口统一在这里；Console 按宽度
缓存（拖动窗口会不断产生新宽度，缓存只保留最近用过的少量实例）。
"""
from __future__ import annotations

import re

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from rich.theme import Theme

# TUI 的 Rich Markdown 主题：避免默认的 “cyan on black” 代码块在深色终端上
# 显得突兀，也让正文/标题/链接与 Textual dark 主题一致。
MARKDOWN_THEME = Theme({
    'markdown.paragraph': '#d7d7d7',
    'markdown.h1': 'bold #ffffff',
    'markdown.h2': 'bold #ffffff',
    'markdown.h3': 'bold #eeeeee',
    'markdown.h4': 'bold #eeeeee',
    'markdown.h5': 'bold #dddddd',
    'markdown.h6': 'bold #dddddd',
    'markdown.strong': 'bold #ffffff',
    'markdown.em': 'italic #d7d7d7',
    'markdown.code': 'bold #87d7ff',
    'markdown.code_block': '#d7d7d7 on #1c1c1c',
    'markdown.block_quote': 'italic #a7a7a7',
    'markdown.item.bullet': '#d7af5f',
    'markdown.item.number': '#d7af5f',
    'markdown.link': 'underline #5fafff',
    'markdown.link_url': 'dim #5fafff',
    'markdown.table.border': '#5f8787',
    'markdown.hr': '#5f8787',
})

_FENCE_OPEN = re.compile(r'^( {0,3})(`{3,}|~{3,})([^\r\n]*)(\r?\n)?$')
_FENCE_CLOSE = re.compile(r'^ {0,3}(`{3,}|~{3,})[ \t]*(\r?\n)?$')


def _short_count(value) -> str:
    """12345 -> 12.3k，避免状态栏在窄终端被挤掉。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    if number >= 1_000_000:
        return f'{number / 1_000_000:.1f}m'
    if number >= 10_000:
        return f'{number / 1000:.1f}k'
    return str(number)


def _streaming_markdown_source(source: str) -> tuple[str, bool]:
    """Avoid expensive syntax lexing for labeled fenced code during streaming."""
    if '```' not in source and '~~~' not in source:
        return source, False
    output = []
    fence_char = None
    fence_size = 0
    changed = False
    for line in source.splitlines(keepends=True):
        if fence_char is None:
            match = _FENCE_OPEN.match(line)
            if match:
                fence = match.group(2)
                info = match.group(3)
                if fence[0] == '`' and '`' in info:
                    output.append(line)  # CommonMark: backticks invalidate this fence.
                    continue
                fence_char, fence_size = fence[0], len(fence)
                if info.strip() and info.split()[0] != 'text':
                    output.append(f'{match.group(1)}{fence}text{match.group(4) or ""}')
                    changed = True
                    continue
        else:
            close = _FENCE_CLOSE.match(line)
            if close:
                fence = close.group(1)
                if fence[0] == fence_char and len(fence) >= fence_size:
                    fence_char, fence_size = None, 0
        output.append(line)
    return (''.join(output), changed) if changed else (source, False)


def _render_markdown(text: str, width: int) -> list[Text]:
    """Markdown 渲染兜底：畸形链接/超长表格等不能让整帧崩溃。"""
    try:
        return _render_lines(RichMarkdown(text), width)
    except Exception:
        return _render_lines(Text(text), width)


_CONSOLES: dict[int, Console] = {}


def _console_for(width: int) -> Console:
    console = _CONSOLES.get(width)
    if console is None:
        # 拖动窗口会不断产生新宽度；缓存只保留最近用过的少量 Console。
        if len(_CONSOLES) >= 16:
            _CONSOLES.clear()
        console = Console(width=width, force_terminal=True,
                          color_system='truecolor', legacy_windows=False,
                          safe_box=True, theme=MARKDOWN_THEME)
        _CONSOLES[width] = console
    return console


def _render_lines(renderable, width: int) -> list[Text]:
    """把 Rich renderable 渲染成固定宽度的 Text 行（虚拟视口用）。"""
    console = _console_for(width)
    options = console.options.update(max_width=width, min_width=width)
    lines = console.render_lines(renderable, options, pad=False)
    result = []
    for segments in lines:
        result.append(Text.assemble(*[
            (segment.text, segment.style) for segment in segments
            if segment.text]))
    return result or [Text('')]
