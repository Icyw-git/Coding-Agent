"""XLSX 文本抽取：工作表选择、共享字符串、日期格式与行列上限。"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

from document_ooxml import (
    DEFAULT_SHEET_LIMIT,
    MAX_SELECTED_SHEETS,
    MAX_SHEET_COLS,
    MAX_SHEET_ROWS,
    DocumentError,
    _clip,
    _header,
    _join_zip_path,
    _optional_member,
    _r,
    _read_member,
    _relationships,
    _x,
    _xml,
)

_DATE_FORMAT_IDS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47, 50, 51, 52, 53, 54, 55, 56, 57, 58}


def _xlsx_sheets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = _xml(_read_member(archive, 'xl/workbook.xml'), 'xl/workbook.xml')
    relationships = _relationships(archive, 'xl/_rels/workbook.xml.rels')
    names = set(archive.namelist())
    sheets = []
    for sheet in workbook.iter(_x('sheet')):
        name = sheet.get('name') or 'Sheet'
        target = relationships.get(sheet.get(_r('id')) or '')
        path = _join_zip_path('xl', target) if target else ''
        if path in names:
            sheets.append((name, path))
    if sheets:
        return sheets
    pattern = re.compile(r'xl/worksheets/sheet(\d+)\.xml$')
    paths = sorted(
        (name for name in names if pattern.fullmatch(name)), key=lambda name: int(pattern.fullmatch(name).group(1))
    )
    return [(f'Sheet{index}', path) for index, path in enumerate(paths, 1)]


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    data = _optional_member(archive, 'xl/sharedStrings.xml')
    if data is None:
        return []
    root = _xml(data, 'xl/sharedStrings.xml')
    strings = []
    for item in root.iter(_x('si')):
        strings.append(''.join(node.text or '' for node in item.iter(_x('t'))))
    return strings


def _is_date_format(code: str) -> bool:
    cleaned = re.sub(r'\[[^\]]*\]', '', code)
    cleaned = re.sub(r'"[^"]*"', '', cleaned).replace('\\', '')
    return bool(re.search(r'[ymdhs]', cleaned, re.IGNORECASE))


def _date_styles(archive: zipfile.ZipFile) -> list[bool]:
    data = _optional_member(archive, 'xl/styles.xml')
    if data is None:
        return []
    root = _xml(data, 'xl/styles.xml')
    custom = {}
    number_formats = root.find(_x('numFmts'))
    if number_formats is not None:
        for item in number_formats.findall(_x('numFmt')):
            try:
                custom[int(item.get('numFmtId') or 0)] = item.get('formatCode') or ''
            except ValueError:
                continue
    styles = []
    cell_formats = root.find(_x('cellXfs'))
    if cell_formats is not None:
        for item in cell_formats.findall(_x('xf')):
            try:
                fmt_id = int(item.get('numFmtId') or 0)
            except ValueError:
                fmt_id = 0
            styles.append(fmt_id in _DATE_FORMAT_IDS or _is_date_format(custom.get(fmt_id, '')))
    return styles


def _excel_date(value, date1904: bool) -> str:
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return str(value)
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    moment = base + timedelta(days=serial)
    if 0 <= serial < 1:
        return moment.strftime('%H:%M:%S')
    if moment.hour or moment.minute or moment.second:
        return moment.strftime('%Y-%m-%d %H:%M:%S')
    return moment.strftime('%Y-%m-%d')


def _column_index(reference: str) -> int:
    letters = ''.join(ch for ch in reference if ch.isalpha())
    index = 0
    for char in letters.upper():
        index = index * 26 + (ord(char) - 64)
    return index


def _xlsx_cell_value(cell: ET.Element, shared: list[str], date_styles: list[bool], date1904: bool) -> str:
    kind = cell.get('t') or ''
    raw = cell.find(_x('v'))
    if kind == 'inlineStr':
        inline = cell.find(_x('is'))
        if inline is not None:
            return ''.join(node.text or '' for node in inline.iter(_x('t')))
        return ''
    if raw is None or raw.text is None:
        formula = cell.find(_x('f'))
        if formula is not None and formula.text:
            return f'={formula.text}'
        return ''
    value = raw.text
    if kind == 's':
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ''
    if kind == 'b':
        return 'TRUE' if value == '1' else 'FALSE'
    if kind == 'e':
        return f'#ERROR({value})'
    if kind == 'str':
        return value
    try:
        style = int(cell.get('s') or 0)
    except ValueError:
        style = 0
    if 0 <= style < len(date_styles) and date_styles[style]:
        return _excel_date(value, date1904)
    return value


def _select_sheets(spec, sheets: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], bool]:
    if not sheets:
        return [], False
    if spec is None or not str(spec).strip():
        return sheets[:DEFAULT_SHEET_LIMIT], len(sheets) > DEFAULT_SHEET_LIMIT
    selected = []
    seen = set()
    for token in str(spec).split(','):
        token = token.strip()
        if not token:
            continue
        if token.isdigit():
            index = int(token)
            if index < 1 or index > len(sheets):
                raise DocumentError(f'sheet {index} exceeds total {len(sheets)}')
            item = sheets[index - 1]
        else:
            matches = [sheet for sheet in sheets if sheet[0].lower() == token.lower()]
            if not matches:
                available = ', '.join(name for name, _ in sheets[:12])
                raise DocumentError(f'unknown sheet {token!r} (available: {available})')
            item = matches[0]
        if item not in seen:
            selected.append(item)
            seen.add(item)
    if not selected:
        raise DocumentError('no sheets selected')
    truncated = len(selected) > MAX_SELECTED_SHEETS
    return selected[:MAX_SELECTED_SHEETS], truncated


def _xlsx_sheet_text(
    archive: zipfile.ZipFile, path: str, shared: list[str], date_styles: list[bool], date1904: bool
) -> tuple[str, int, bool]:
    root = _xml(_read_member(archive, path), path)
    lines = []
    truncated = False
    for row in root.iter(_x('row')):
        if len(lines) >= MAX_SHEET_ROWS:
            truncated = True
            break
        cells = {}
        next_column = 1
        for cell in row.findall(_x('c')):
            reference = cell.get('r') or ''
            column = _column_index(reference) if reference else next_column
            next_column = column + 1
            if column > MAX_SHEET_COLS:
                truncated = True
                continue
            value = _xlsx_cell_value(cell, shared, date_styles, date1904)
            if value:
                cells[column] = value
        if not cells:
            continue
        width = max(cells)
        lines.append(' | '.join(cells.get(index, '') for index in range(1, width + 1)))
    return '\n'.join(lines), len(lines), truncated


def _xlsx_text(data: bytes, filename: str, sheets) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            all_sheets = _xlsx_sheets(archive)
            if not all_sheets:
                raise DocumentError('XLSX has no worksheets')
            selected, clipped = _select_sheets(sheets, all_sheets)
            shared = _shared_strings(archive)
            date_styles = _date_styles(archive)
            workbook = _xml(_read_member(archive, 'xl/workbook.xml'), 'xl/workbook.xml')
            properties = workbook.find(_x('workbookPr'))
            date1904 = properties is not None and (properties.get('date1904') or '') in ('1', 'true')
            sections = []
            any_truncated = False
            for name, path in selected:
                text, rows, row_truncated = _xlsx_sheet_text(archive, path, shared, date_styles, date1904)
                any_truncated = any_truncated or row_truncated
                sections.append(f'--- sheet: {name} ({rows} rows shown) ---\n' + (text or '[empty sheet]'))
    except zipfile.BadZipFile as exc:
        raise DocumentError(f'cannot open XLSX: {exc}') from exc
    body = _clip('\n\n'.join(sections))
    notes = []
    if sheets is None or not str(sheets).strip():
        if len(all_sheets) > len(selected):
            notes.append(
                f'only the first {len(selected)} of {len(all_sheets)} sheets are included; use sheets= to choose.'
            )
    elif clipped:
        notes.append(f'only the first {len(selected)} selected sheets are included.')
    if any_truncated:
        notes.append(f'rows/columns are limited to {MAX_SHEET_ROWS} x {MAX_SHEET_COLS}.')
    summary = f'{len(all_sheets)} sheet(s) · extracted {len(selected)}'
    return _header('XLSX', filename, summary, body, notes)
