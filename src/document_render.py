"""把文档页面渲染成 PNG，交给多模态模型看排版。

与 documents.py 的分工：
- documents.py 抽「文字」，适合检索、总结、改内容；
- 本模块出「图」，适合版式、图表、扫描页、表格对齐等纯文本会丢失的信息。

PDF 用 pypdfium2（PDFium，Apache-2.0）直接渲染。DOCX/PPTX 先转 PDF：优先
LibreOffice headless（跨平台），没有则明确报错，不启动 Office 桌面程序。
"""
from __future__ import annotations

import io
import math
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import documents

MAX_RENDER_PAGES = 8
DEFAULT_RENDER_PAGES = 3
DEFAULT_DPI = 110
MIN_DPI = 72
MAX_DPI = 200
MAX_PIXELS = 4_000_000
CONVERT_TIMEOUT = 90


class RenderError(ValueError):
    """文档无法渲染时给模型看的错误（不抛到工具边界外）。"""


def _dpi(value) -> int:
    try:
        dpi = int(value)
    except (TypeError, ValueError) as exc:
        raise RenderError('dpi must be an integer between 72 and 200') from exc
    if not MIN_DPI <= dpi <= MAX_DPI:
        raise RenderError(f'dpi must be between {MIN_DPI} and {MAX_DPI}')
    return dpi


def _select_pages(spec, total: int, label: str = 'page') -> list[int]:
    if total <= 0:
        raise RenderError(f'document has no {label}s')
    if spec is None or not str(spec).strip():
        return list(range(1, min(total, DEFAULT_RENDER_PAGES) + 1))
    selected, _ = documents._parse_pages(spec, total, label=label)
    if len(selected) > MAX_RENDER_PAGES:
        raise RenderError(
            f'at most {MAX_RENDER_PAGES} {label}s can be rendered per call; '
            f'got {len(selected)}')
    return selected


def _render_pdf(data: bytes, pages, dpi: int) -> list[tuple[int, bytes]]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RenderError(
            'PDF rendering requires pypdfium2; install it with '
            '`pip install pypdfium2`') from exc
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as exc:
        raise RenderError(f'cannot open PDF for rendering: {exc}') from exc
    try:
        selected = _select_pages(pages, len(pdf))
        rendered = []
        for number in selected:
            page = pdf[number - 1]
            width, height = page.get_size()
            if width <= 0 or height <= 0:
                raise RenderError(f'page {number} has an invalid size')
            # 像素上限：超大页面/高 DPI 时自动降采样，避免一张图打爆内存和上下文。
            limit_dpi = math.sqrt(MAX_PIXELS * 72 * 72 / (width * height))
            effective = max(1, min(dpi, int(limit_dpi)))
            bitmap = page.render(scale=effective / 72)
            image = bitmap.to_pil().convert('RGB')
            buffer = io.BytesIO()
            image.save(buffer, format='PNG', optimize=True)
            rendered.append((number, buffer.getvalue()))
        return rendered
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(f'cannot render PDF: {exc}') from exc
    finally:
        pdf.close()


def _find_soffice() -> str | None:
    configured = os.getenv('HARNESS_SOFFICE')
    if configured:
        return configured if Path(configured).is_file() else None
    for name in ('soffice', 'libreoffice'):
        found = shutil.which(name)
        if found:
            return found
    if os.name == 'nt':
        for root in (os.environ.get('PROGRAMFILES', r'C:\Program Files'),
                     os.environ.get('PROGRAMFILES(X86)',
                                    r'C:\Program Files (x86)')):
            candidate = Path(root) / 'LibreOffice' / 'program' / 'soffice.exe'
            if candidate.is_file():
                return str(candidate)
    return None


def _check_no_macros(data: bytes, filename: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise RenderError(f'cannot open {filename}: {exc}') from exc
    if any(name.lower().endswith('vbaproject.bin') for name in names):
        raise RenderError(
            'macro-enabled Office documents are not rendered; '
            'remove macros or export the document to PDF first')


def _office_to_pdf(data: bytes, suffix: str) -> bytes:
    soffice = _find_soffice()
    if not soffice:
        raise RenderError(
            'rendering DOCX/PPTX requires LibreOffice (soffice); '
            'export the document to PDF first, or install LibreOffice')
    _check_no_macros(data, suffix)
    with tempfile.TemporaryDirectory(prefix='harness-render-') as tmp:
        source = Path(tmp) / f'source{suffix}'
        source.write_bytes(data)
        profile = Path(tmp) / 'lo-profile'
        command = [
            soffice, '--headless', '--norestore',
            f'-env:UserInstallation={profile.as_uri()}',
            '--convert-to', 'pdf', '--outdir', tmp, str(source),
        ]
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=CONVERT_TIMEOUT,
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if os.name == 'nt' else 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RenderError(f'LibreOffice conversion failed: {exc}') from exc
        candidates = [path for path in Path(tmp).glob('*.pdf')]
        if not candidates:
            detail = (proc.stderr or proc.stdout or '').strip()[-500:]
            raise RenderError(
                f'LibreOffice did not produce a PDF (exit {proc.returncode})'
                + (f': {detail}' if detail else ''))
        pdf = candidates[0].read_bytes()
    if not pdf.startswith(b'%PDF-'):
        raise RenderError('LibreOffice produced an invalid PDF')
    return pdf


def render_document(data: bytes, filename: str, *, pages=None,
                    dpi=DEFAULT_DPI) -> list[tuple[int, bytes]]:
    """返回 [(页码, PNG bytes)]；失败抛 RenderError。

    只接受 PDF/DOCX/PPTX。XLSX 的打印分页和表格布局差异太大，建议用
    read_document 读单元格，或先打印成 PDF 再渲染。
    """
    size = _dpi(dpi)
    if len(data) > documents.MAX_DOC_BYTES:
        raise RenderError(
            f'document is too large ({len(data)} bytes; '
            f'max {documents.MAX_DOC_BYTES})')
    kind = documents.detect_kind(data)
    if kind == 'pdf':
        return _render_pdf(data, pages, size)
    if kind in ('docx', 'pptx'):
        pdf = _office_to_pdf(data, f'.{kind}')
        return _render_pdf(pdf, pages, size)
    if kind == 'xlsx':
        raise RenderError(
            'XLSX rendering is not supported; use read_document for cells, '
            'or print the workbook to PDF and render that')
    if kind == 'ole':
        raise RenderError(
            'legacy or encrypted Office binary is not supported; '
            'save/decrypt it as PDF, DOCX, or PPTX')
    raise RenderError(
        'unsupported document format for rendering '
        '(supported: PDF, DOCX, PPTX)')
