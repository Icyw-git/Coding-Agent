"""PPTX 文本抽取：幻灯片、表格与演讲者备注。"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from document_ooxml import (
    MAX_TABLE_ROWS,
    DocumentError,
    _a,
    _clean_text,
    _clip,
    _header,
    _join_zip_path,
    _optional_member,
    _p,
    _parse_pages,
    _r,
    _read_member,
    _relationships,
    _xml,
)


def _pptx_slide_paths(archive: zipfile.ZipFile) -> list[str]:
    presentation = _xml(_read_member(archive, 'ppt/presentation.xml'), 'ppt/presentation.xml')
    relationships = _relationships(archive, 'ppt/_rels/presentation.xml.rels')
    names = set(archive.namelist())
    paths = []
    for slide_id in presentation.iter(_p('sldId')):
        target = relationships.get(slide_id.get(_r('id')) or '')
        if not target:
            continue
        path = _join_zip_path('ppt', target)
        if path in names:
            paths.append(path)
    if not paths:
        pattern = re.compile(r'ppt/slides/slide(\d+)\.xml$')
        paths = sorted(
            (name for name in names if pattern.fullmatch(name)), key=lambda name: int(pattern.fullmatch(name).group(1))
        )
    return paths


def _drawing_text(paragraph: ET.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == _a('t'):
            parts.append(node.text or '')
        elif node.tag == _a('br'):
            parts.append('\n')
        elif node.tag == _a('tab'):
            parts.append('\t')
    return _clean_text(''.join(parts))


def _table_block(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    width = max(len(row) for row in rows)
    lines = []
    for index, row in enumerate(rows):
        padded = row + [''] * (width - len(row))
        lines.append('| ' + ' | '.join(padded) + ' |')
        if index == 0:
            lines.append('| ' + ' | '.join('---' for _ in padded) + ' |')
    return lines


def _pptx_slide_text(archive: zipfile.ZipFile, path: str) -> str:
    root = _xml(_read_member(archive, path), path)
    table_paragraphs: set[int] = set()
    blocks: list[str] = []
    for table in root.iter(_a('tbl')):
        rows: list[list[str]] = []
        for row in table.findall(_a('tr')):
            if len(rows) >= MAX_TABLE_ROWS:
                break
            cells = []
            for cell in row.findall(_a('tc')):
                texts = []
                for paragraph in cell.iter(_a('p')):
                    table_paragraphs.add(id(paragraph))
                    text = _drawing_text(paragraph)
                    if text:
                        texts.append(text)
                cells.append(' '.join(texts))
            if any(cells):
                rows.append(cells)
        table_lines = _table_block(rows)
        if table_lines:
            blocks.append('\n'.join(table_lines))
    for paragraph in root.iter(_a('p')):
        if id(paragraph) in table_paragraphs:
            continue
        text = _drawing_text(paragraph)
        if not text:
            continue
        properties = paragraph.find(_a('pPr'))
        bullet = properties is not None and (
            properties.find(_a('buChar')) is not None or properties.find(_a('buAutoNum')) is not None
        )
        blocks.append(('- ' if bullet else '') + text)
    return '\n'.join(blocks)


def _pptx_notes(archive: zipfile.ZipFile, slide_path: str) -> str:
    base_dir = slide_path.rpartition('/')[0]
    relationships = _relationships(archive, f'{base_dir}/_rels/{slide_path.rpartition("/")[2]}.rels')
    target = next((value for value in relationships.values() if 'notesSlide' in value), None)
    if target:
        name = _join_zip_path(base_dir, target)
    else:
        match = re.search(r'slide(\d+)\.xml$', slide_path)
        if match is None:
            return ''
        name = f'ppt/notesSlides/notesSlide{match.group(1)}.xml'
    data = _optional_member(archive, name)
    if data is None:
        return ''
    root = _xml(data, name)
    lines = []
    for shape in root.iter(_p('sp')):
        placeholder = next(shape.iter(_p('ph')), None)
        if placeholder is not None and placeholder.get('type') in ('sldNum', 'sldImg', 'hdr', 'ftr', 'dt'):
            continue  # 页码/缩略图/页眉页脚不是演讲者备注
        for paragraph in shape.iter(_a('p')):
            text = _drawing_text(paragraph)
            if not text or text.lower().startswith('click to '):
                continue
            lines.append(text)
    return '\n'.join(lines)


def _pptx_text(data: bytes, filename: str, pages) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            paths = _pptx_slide_paths(archive)
            if not paths:
                raise DocumentError('PPTX has no slides')
            selected, clipped = _parse_pages(pages, len(paths), label='slide')
            sections = []
            for number in selected:
                text = _pptx_slide_text(archive, paths[number - 1])
                notes = _pptx_notes(archive, paths[number - 1])
                section = f'--- slide {number} ---\n' + (text or '[no text]')
                if notes:
                    section += '\n[notes]\n' + notes
                sections.append(section)
    except zipfile.BadZipFile as exc:
        raise DocumentError(f'cannot open PPTX: {exc}') from exc
    body = _clip('\n\n'.join(sections))
    notes = []
    if pages is None or not str(pages).strip():
        if len(paths) > len(selected):
            notes.append(
                f'only the first {len(selected)} of {len(paths)} slides are included; '
                f'use pages={len(selected) + 1}-'
                f'{min(len(paths), len(selected) * 2)} for the rest.'
            )
    elif clipped:
        notes.append(f'only the first {len(selected)} selected slides are included.')
    summary = f'{len(paths)} slides · extracted {len(selected)}'
    return _header('PPTX', filename, summary, body, notes)
