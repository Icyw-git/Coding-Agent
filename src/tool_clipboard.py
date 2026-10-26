"""clipboard_read / clipboard_write 工具层：格式化与审计。

审计只记长度、截断标记与耗时，不记剪贴板正文——正文可能包含密码或私信，
落进会话事件流等于把敏感内容再扩散一次。
"""
from __future__ import annotations

import clipboard_windows
from clipboard_windows import MAX_READ_BYTES


def _notify(audit, event: str, **payload) -> None:
    if audit is None:
        return
    try:
        audit(event, **payload)
    except Exception:
        pass


def read(*, runner=None, audit=None) -> str:
    try:
        result = clipboard_windows.read(runner=runner)
    except clipboard_windows.ClipboardError as exc:
        return f'Error: {exc}'
    _notify(audit, 'clipboard_read', bytes=result.total_bytes,
            chars=len(result.text), truncated=result.truncated,
            duration_ms=result.duration_ms)
    header = f'Clipboard text: {result.total_bytes} bytes'
    if result.truncated:
        header += (f' (truncated; showing the first '
                   f'{MAX_READ_BYTES // 1024} KiB)')
    if not result.text:
        return f'{header}\n\nThe clipboard is empty or contains no text.'
    return f'{header}\n\n{result.text}'


def write(text, *, runner=None, audit=None) -> str:
    if not isinstance(text, str):
        return 'Error: text must be a string'
    try:
        result = clipboard_windows.write(text, runner=runner)
    except clipboard_windows.ClipboardError as exc:
        return f'Error: {exc}'
    _notify(audit, 'clipboard_written', chars=result.chars,
            bytes=result.bytes, duration_ms=result.duration_ms)
    return f'Clipboard updated: {result.chars} chars ({result.bytes} bytes).'
