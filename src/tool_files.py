"""read / write / edit 的实现：边界判定、编码保留与二进制保护。

路径解析（`resolve`）、无头审批门（`gate`）与写入前 checkpoint 捕获（`capture`）
由 tools.py 注入；本模块不读取工作区全局状态，可脱离工具注册表单独测试。
"""
from __future__ import annotations

from typing import Optional

from tool_text import BinaryFileError, encode_text, read_text_file

DOCUMENT_SUFFIXES = frozenset({'.pdf', '.docx', '.pptx', '.xlsx',
                               '.doc', '.ppt', '.xls'})


def read_file(path: str, *, resolve, gate, limit: Optional[int] = None,
              offset: int = 0, cwd: str = None) -> str:
    # 读默认放行，但秘密路径是硬拒；teammate 等无头路径不走 PreToolUse，
    # 边界上必须自己判一次。
    denied = gate('read', path, cwd)
    if denied:
        return denied
    try:
        try:
            offset = int(offset or 0)
            limit = None if limit is None else int(limit)
        except (TypeError, ValueError):
            return 'Error: offset and limit must be integers'
        if offset < 0 or (limit is not None and limit < 0):
            return 'Error: offset and limit must be non-negative'
        file_path = resolve(path, cwd)
        if file_path.is_dir():
            # 原始 OSError 会是 `[Errno 13] Permission denied: 'D:\harness-agent'`，
            # 用户看不出「这是目录」。直接给下一步动作。
            return (f'Error: {file_path} is a directory; use ls to list it, '
                    'or glob/grep to search inside it')
        if file_path.suffix.lower() in DOCUMENT_SUFFIXES:
            return (f'Error: {file_path.suffix} is a document; use read_document '
                    'to extract its text')
        text, _encoding, _newline = read_text_file(file_path)
        lines = text.splitlines()
        total = len(lines)
        if offset >= total:
            return f'(no lines at offset {offset}; file has {total} lines)'
        end = total if limit is None else min(total, offset + limit)
        if limit is None and total - offset > 500:
            # 大文件没传 limit：从 offset 起只取 200 行，并给出下一次的 offset。
            end = offset + 200
        chunk = '\n'.join(lines[offset:end])
        if end < total:
            chunk += f'\n... ({total - end} more lines; use offset={end})'
        return chunk if chunk else '(empty file)'
    except BinaryFileError as exc:
        return f'Error: {exc}'
    except Exception as e:
        return f"Error: {e}"


def write_file(path: str, content: str, *, resolve, gate, capture,
               cwd: str = None) -> str:
    # read-only 档 / 无头路径（teammate、后台）的写改同样要在边界拦一道：
    # 判定参数与路径解析一致，两处不会给出相反结论。
    denied = gate('write', path, cwd)
    if denied:
        return denied
    try:
        file_path = resolve(path, cwd)
        encoding, newline = 'utf-8', '\n'
        if file_path.is_file():
            try:
                _text, encoding, newline = read_text_file(file_path)
            except BinaryFileError:
                return f'Error: refusing to overwrite binary file: {path}'
        capture(file_path)   # 写盘前保存 pre-image，供 /rewind 回滚
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(encode_text(content, encoding, newline))
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def edit_file(path: str, old_text: str, new_text: str, *, resolve, gate,
              capture, replace_all: bool = False, cwd: str = None) -> str:
    denied = gate('edit', path, cwd)
    if denied:
        return denied
    try:
        if not old_text:
            return 'Error: old_text must not be empty'
        file_path = resolve(path, cwd)
        text, encoding, newline = read_text_file(file_path)
        count = text.count(old_text)
        if count == 0:
            return f"Error: text not found in {path}"
        if count > 1 and not replace_all:
            return (f'Error: text appears {count} times in {path}; '
                    'provide more context or set replace_all=true')
        updated = text.replace(old_text, new_text) if replace_all \
            else text.replace(old_text, new_text, 1)
        capture(file_path)   # 确认能改再抓 pre-image，避免记下无意义的快照
        file_path.write_bytes(encode_text(updated, encoding, newline))
        return f"Edited {path}"
    except BinaryFileError as exc:
        return f'Error: {exc}'
    except Exception as e:
        return f"Error: {e}"
