"""共享 OOXML 工具：zip 成员读取上限、XML 防护、文本清理与页选择。

DOCX/PPTX/XLSX 三个解析器都建立在这里的常量与 helper 上。本模块只认字节与
自己的常量，不反向 import 格式模块或 documents。
"""

from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree as ET

MAX_DOC_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_CHARS = 20_000
MAX_SELECTED_PAGES = 50
DEFAULT_PAGE_LIMIT = 20
MAX_ZIP_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TABLE_ROWS = 100
MAX_SHEET_ROWS = 200
MAX_SHEET_COLS = 40
DEFAULT_SHEET_LIMIT = 8
MAX_SELECTED_SHEETS = 20

_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
_A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
_X = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


class DocumentError(ValueError):
    """文档无法解析时给模型看的错误（不抛到工具边界外）。"""


def _tag(namespace: str, name: str) -> str:
    return f'{{{namespace}}}{name}'


def _w(name: str) -> str:
    return _tag(_W, name)


def _p(name: str) -> str:
    return _tag(_P, name)


def _a(name: str) -> str:
    return _tag(_A, name)


def _x(name: str) -> str:
    return _tag(_X, name)


def _r(name: str) -> str:
    return _tag(_R, name)


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise DocumentError(f'missing archive part: {name}') from exc
    if info.file_size > MAX_ZIP_MEMBER_BYTES:
        raise DocumentError(f'archive part is too large: {name}')
    with archive.open(info) as handle:
        data = handle.read(MAX_ZIP_MEMBER_BYTES + 1)
    if len(data) > MAX_ZIP_MEMBER_BYTES:
        raise DocumentError(f'archive part is too large: {name}')
    return data


def _optional_member(archive: zipfile.ZipFile, name: str) -> bytes | None:
    if name not in archive.namelist():
        return None
    return _read_member(archive, name)


def _xml(data: bytes, part: str) -> ET.Element:
    # OOXML 不需要 DTD；拒绝实体声明，避免 billion-laughs 把 16 MiB 成员展开成内存炸弹。
    if re.search(rb'<!\s*(?:DOCTYPE|ENTITY)', data, re.IGNORECASE):
        raise DocumentError(f'DTD/entity declarations are not allowed in {part}')
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise DocumentError(f'invalid XML in {part}: {exc}') from exc


def _clean_text(text: str) -> str:
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = ''.join(ch for ch in text if ch in '\n\t' or ord(ch) >= 32)
    text = '\n'.join(line.rstrip() for line in text.split('\n'))
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS].rstrip() + '\n... [output truncated; narrow the page/sheet selection]'


def _parse_pages(spec, total: int, *, label: str = 'page') -> tuple[list[int], bool]:
    """解析 1-based 页码/幻灯片号：'1-5,8'、'3'、'10-'、'all'。

    返回 (选中的编号, 是否因 MAX_SELECTED_PAGES 被截断)。未指定时取前 20 个。
    """
    if total <= 0:
        return [], False
    if spec is None or not str(spec).strip():
        selected = list(range(1, min(total, DEFAULT_PAGE_LIMIT) + 1))
        return selected, total > len(selected)
    raw = str(spec).strip().lower()
    selected: list[int] = []
    if raw in ('all', '*'):
        selected = list(range(1, total + 1))
    else:
        for token in raw.split(','):
            token = token.strip()
            if not token:
                continue
            match = re.fullmatch(r'(\d*)\s*-\s*(\d*)', token)
            if match:
                start_text, end_text = match.groups()
                if not start_text and not end_text:
                    raise DocumentError(f'invalid {label} range: {token!r}')
                start = int(start_text) if start_text else 1
                end = int(end_text) if end_text else total
            elif token.isdigit():
                start = end = int(token)
            else:
                raise DocumentError(f'invalid {label} selection: {token!r} (use e.g. 1-5,8)')
            if start < 1 or end < 1:
                raise DocumentError(f'{label} numbers are 1-based: {token!r}')
            if start > total:
                raise DocumentError(f'{label} {start} exceeds total {total}')
            end = min(end, total)
            if start > end:
                raise DocumentError(f'invalid {label} range: {token!r}')
            selected.extend(range(start, end + 1))
    selected = sorted(set(selected))
    if not selected:
        raise DocumentError(f'no {label}s selected')
    truncated = len(selected) > MAX_SELECTED_PAGES
    return selected[:MAX_SELECTED_PAGES], truncated


def _header(kind: str, filename: str, summary: str, body: str, notes: list[str] | None = None) -> str:
    head = f'{kind}: {filename} · {summary} · {len(body)} chars'
    if notes:
        head += '\n' + ' '.join(note for note in notes if note)
    return head + '\n\n' + body


def _relationships(archive: zipfile.ZipFile, name: str) -> dict[str, str]:
    data = _optional_member(archive, name)
    if data is None:
        return {}
    root = _xml(data, name)
    result = {}
    for relationship in root:
        if not relationship.tag.endswith('Relationship'):
            continue
        identifier = relationship.get('Id')
        target = relationship.get('Target')
        if identifier and target:
            result[identifier] = target
    return result


def _join_zip_path(base: str, target: str) -> str:
    target = target.replace('\\', '/')
    parts = [] if target.startswith('/') else base.split('/')
    for part in target.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return '/'.join(parts)
