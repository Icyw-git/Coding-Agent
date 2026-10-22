"""文档文本抽取：PDF / DOCX / PPTX / XLSX（只读，不落临时文件）。

边界与取舍：
- PDF 只解析文本层，依赖 pypdf；不做 OCR。扫描件会明确提示，而不是返回空文本。
- OOXML 本质是 zip + XML，DOCX/PPTX/XLSX 用标准库解析，不引入 python-docx、
  python-pptx、openpyxl 这类重量依赖；表格/幻灯片/工作表边界保留为纯文本。
- 输出有字符预算；读取方（工具层）还会做入口截断与落盘，两层职责不混。
- 所有 zip 成员读取都设上限，避免文档炸弹把内存/上下文撑爆。
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timedelta
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

_DATE_FORMAT_IDS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47, 50, 51, 52, 53, 54, 55, 56, 57, 58}


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


def detect_kind(data: bytes) -> str | None:
    """按内容识别格式，不信任扩展名。返回 pdf/docx/pptx/xlsx/ole/None。"""
    if data.startswith(b'%PDF-'):
        return 'pdf'
    if data.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        return 'ole'  # 旧版 .doc/.xls/.ppt 的 OLE 复合文档
    if not data.startswith(b'PK\x03\x04'):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return None
    if 'word/document.xml' in names:
        return 'docx'
    if 'ppt/presentation.xml' in names:
        return 'pptx'
    if 'xl/workbook.xml' in names:
        return 'xlsx'
    return None


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


def _pdf_text(data: bytes, filename: str, pages) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentError(
            'PDF extraction requires pypdf; install it with `pip install -r requirements-docs.txt`'
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt('')
            except Exception as exc:
                raise DocumentError('PDF is encrypted and could not be opened') from exc
            if not unlocked:
                raise DocumentError('PDF is encrypted and needs a password')
        total = len(reader.pages)
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f'cannot open PDF: {exc}') from exc
    selected, clipped = _parse_pages(pages, total)
    default_selection = pages is None or not str(pages).strip()
    sections: list[str] = []
    empty = 0
    for number in selected:
        try:
            text = reader.pages[number - 1].extract_text() or ''
        except Exception as exc:
            text = f'[page {number}: extraction failed: {exc}]'
        text = _clean_text(text)
        if not text:
            empty += 1
            text = '[no text layer on this page]'
        sections.append(f'--- page {number} ---\n{text}')
    body = _clip('\n\n'.join(sections))
    notes = []
    if default_selection:
        if total > len(selected):
            notes.append(
                f'only the first {len(selected)} of {total} pages are included; '
                f'use pages={len(selected) + 1}-{min(total, len(selected) * 2)} '
                'for the rest.'
            )
    elif clipped:
        notes.append(f'only the first {len(selected)} selected pages are included.')
    if selected and empty == len(selected):
        notes.append(
            'no text layer found; this may be a scanned PDF. '
            'pypdf cannot OCR - convert/render pages to images and use view_image.'
        )
    summary = f'{total} pages · extracted {len(selected)} page(s)'
    return _header('PDF', filename, summary, body, notes)


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


def extract_document(data: bytes, filename: str, *, pages=None, sheets=None) -> str:
    """识别并抽取文档文本；失败时抛出 DocumentError。"""
    if len(data) > MAX_DOC_BYTES:
        raise DocumentError(f'document is too large ({len(data)} bytes; max {MAX_DOC_BYTES})')
    kind = detect_kind(data)
    if kind is None:
        raise DocumentError('unsupported document format (supported: PDF, DOCX, PPTX, XLSX)')
    if kind == 'ole':
        raise DocumentError(
            'legacy or encrypted Office binary is not supported; save/decrypt it as PDF, DOCX, PPTX, or XLSX'
        )
    if kind == 'pdf':
        return _pdf_text(data, filename, pages)
    if kind == 'docx':
        return _docx_text(data, filename)
    if kind == 'pptx':
        return _pptx_text(data, filename, pages)
    if kind == 'xlsx':
        return _xlsx_text(data, filename, sheets)
    raise DocumentError(f'unsupported document format: {kind}')
