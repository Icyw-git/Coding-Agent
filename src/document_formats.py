"""OOXML 文档解析门面：DOCX / PPTX / XLSX 与共享工具。

实现按格式拆分，本模块只保留原有导入面，``documents.py`` 与既有调用点无需
感知拆分：

- ``document_ooxml``：zip/XML 防护、文本清理、页选择与共享常量；
- ``document_docx`` / ``document_pptx`` / ``document_xlsx``：各格式解析。

PDF 解析与格式识别仍在 ``documents.py`` 的抽取入口旁。

⚠ OOXML 本质是 zip + XML；所有成员读取都有上限，避免文档炸弹撑爆内存。
上限由实际读取它的 ``document_ooxml`` 持有，测试补丁要打在那一个模块上。
"""

from __future__ import annotations

from document_docx import _docx_text
from document_ooxml import (
    DEFAULT_PAGE_LIMIT,
    DEFAULT_SHEET_LIMIT,
    MAX_DOC_BYTES,
    MAX_OUTPUT_CHARS,
    MAX_SELECTED_PAGES,
    MAX_SELECTED_SHEETS,
    MAX_SHEET_COLS,
    MAX_SHEET_ROWS,
    MAX_TABLE_ROWS,
    MAX_ZIP_MEMBER_BYTES,
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
    _tag,
    _w,
    _x,
    _xml,
)
from document_pptx import _pptx_text
from document_xlsx import _xlsx_text

__all__ = [
    'DEFAULT_PAGE_LIMIT',
    'DEFAULT_SHEET_LIMIT',
    'DocumentError',
    'MAX_DOC_BYTES',
    'MAX_OUTPUT_CHARS',
    'MAX_SELECTED_PAGES',
    'MAX_SELECTED_SHEETS',
    'MAX_SHEET_COLS',
    'MAX_SHEET_ROWS',
    'MAX_TABLE_ROWS',
    'MAX_ZIP_MEMBER_BYTES',
    '_a',
    '_clean_text',
    '_clip',
    '_docx_text',
    '_header',
    '_join_zip_path',
    '_optional_member',
    '_p',
    '_parse_pages',
    '_pptx_text',
    '_r',
    '_read_member',
    '_relationships',
    '_tag',
    '_w',
    '_x',
    '_xlsx_text',
    '_xml',
]
