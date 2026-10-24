"""DOCX 文本抽取：段落、标题层级、列表与表格。"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from document_ooxml import (
    MAX_TABLE_ROWS,
    DocumentError,
    _clean_text,
    _clip,
    _header,
    _optional_member,
    _read_member,
    _w,
    _xml,
)


def _docx_paragraph_text(paragraph: ET.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == _w('t'):
            parts.append(node.text or '')
        elif node.tag == _w('tab'):
            parts.append('\t')
        elif node.tag in (_w('br'), _w('cr')):
            parts.append('\n')
    return _clean_text(''.join(parts))


def _docx_style_names(archive: zipfile.ZipFile) -> dict[str, str]:
    """styleId → 显示名；中文 Word 的标题样式 id 往往不是 Heading1。"""
    data = _optional_member(archive, 'word/styles.xml')
    if data is None:
        return {}
    root = _xml(data, 'word/styles.xml')
    names = {}
    for style in root.iter(_w('style')):
        identifier = style.get(_w('styleId')) or ''
        name = style.find(_w('name'))
        value = (name.get(_w('val')) if name is not None else '') or ''
        if identifier and value:
            names[identifier] = value
    return names


def _docx_paragraph_prefix(paragraph: ET.Element, style_names: dict[str, str]) -> str:
    properties = paragraph.find(_w('pPr'))
    if properties is None:
        return ''
    style = properties.find(_w('pStyle'))
    if style is not None:
        identifier = style.get(_w('val')) or ''
        value = style_names.get(identifier, identifier)
        normalized = value.lower().replace(' ', '')
        match = re.match(r'(?:heading|标题)(\d+)$', normalized)
        if match:
            return '#' * min(6, max(1, int(match.group(1)))) + ' '
    if properties.find(_w('numPr')) is not None:
        return '- '
    return ''


def _docx_table(table: ET.Element) -> tuple[list[str], bool]:
    rows: list[list[str]] = []
    truncated = False
    for row in table.findall(_w('tr')):
        if len(rows) >= MAX_TABLE_ROWS:
            truncated = True
            break
        cells = []
        for cell in row.findall(_w('tc')):
            paragraphs = [_docx_paragraph_text(p) for p in cell.iter(_w('p'))]
            cells.append(' '.join(text for text in paragraphs if text))
        if any(cells):
            rows.append(cells)
    if not rows:
        return [], False
    width = max(len(row) for row in rows)
    lines = []
    for index, row in enumerate(rows):
        padded = row + [''] * (width - len(row))
        lines.append('| ' + ' | '.join(padded) + ' |')
        if index == 0:
            lines.append('| ' + ' | '.join('---' for _ in padded) + ' |')
    return lines, truncated


def _docx_text(data: bytes, filename: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            root = _xml(_read_member(archive, 'word/document.xml'), 'word/document.xml')
            style_names = _docx_style_names(archive)
    except zipfile.BadZipFile as exc:
        raise DocumentError(f'cannot open DOCX: {exc}') from exc
    body = root.find(_w('body'))
    if body is None:
        raise DocumentError('DOCX has no document body')
    lines: list[str] = []
    paragraphs = 0
    tables = 0
    table_truncated = False
    for child in body:
        if child.tag == _w('p'):
            text = _docx_paragraph_text(child)
            if text:
                lines.append(_docx_paragraph_prefix(child, style_names) + text)
                paragraphs += 1
        elif child.tag == _w('tbl'):
            table_lines, truncated = _docx_table(child)
            if table_lines:
                if lines:
                    lines.append('')
                lines.extend(table_lines)
                tables += 1
                table_truncated = table_truncated or truncated
    body_text = _clip('\n'.join(lines)) if lines else '[document contains no extractable text]'
    notes = []
    if table_truncated:
        notes.append(f'tables are limited to {MAX_TABLE_ROWS} rows.')
    summary = f'{paragraphs} paragraphs · {tables} table(s)'
    return _header('DOCX', filename, summary, body_text, notes)
