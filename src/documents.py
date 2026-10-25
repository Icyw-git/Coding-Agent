"""文档文本抽取入口：识别格式，解析 PDF，分发 DOCX/PPTX/XLSX。

边界与取舍：
- PDF 只解析文本层，依赖 pypdf；不做 OCR。扫描件会明确提示，而不是返回空文本。
- OOXML 解析经 document_formats 门面分发到 document_docx/pptx/xlsx：本质是
  zip + XML，用标准库即可，不引入 python-docx、python-pptx、openpyxl 这类
  重量依赖；表格/幻灯片/工作表边界保留为纯文本。
- 输出有字符预算；读取方（工具层）还会做入口截断与落盘，两层职责不混。
- 所有 zip 成员读取都设上限，避免文档炸弹把内存/上下文撑爆。
"""

from __future__ import annotations

import io
import zipfile

from document_formats import (
    DEFAULT_PAGE_LIMIT,
    MAX_DOC_BYTES,
    MAX_OUTPUT_CHARS,
    MAX_SELECTED_PAGES,
    MAX_SELECTED_SHEETS,
    MAX_SHEET_COLS,
    MAX_SHEET_ROWS,
    MAX_TABLE_ROWS,
    MAX_ZIP_MEMBER_BYTES,
    DocumentError,
    _clean_text,
    _clip,
    _docx_text,
    _header,
    _parse_pages,
    _pptx_text,
    _xlsx_text,
)

# 拆分后保持既有导入点不变：常量与异常继续从本模块可见。
__all__ = [
    'DEFAULT_PAGE_LIMIT',
    'DocumentError',
    'MAX_DOC_BYTES',
    'MAX_OUTPUT_CHARS',
    'MAX_SELECTED_PAGES',
    'MAX_SELECTED_SHEETS',
    'MAX_SHEET_COLS',
    'MAX_SHEET_ROWS',
    'MAX_TABLE_ROWS',
    'MAX_ZIP_MEMBER_BYTES',
    'detect_kind',
    'extract_document',
]


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
            'pypdf cannot OCR; use render_document on the affected pages so the '
            'vision model can read the rendered images.'
        )
    summary = f'{total} pages · extracted {len(selected)} page(s)'
    return _header('PDF', filename, summary, body, notes)


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
