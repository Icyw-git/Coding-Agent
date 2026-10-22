"""read_document：PDF/DOCX/PPTX/XLSX 抽取、分页、错误边界与工具接线。"""
import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

import document_formats
import documents
import exec_policy
import harness
import tools

# 仅 PDF 文本抽取依赖 pypdf；Office 解析和工具边界在基础环境中也要验证。
requires_pdf = pytest.mark.skipif(
    importlib.util.find_spec('pypdf') is None,
    reason='pypdf is not installed (requirements-docs.txt)')


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    return tmp_path


def _zip(parts: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _pdf(texts: list[str]) -> bytes:
    """构造带文本层的合法 PDF（Helvetica，无外部依赖）。"""
    objects = {}
    page_ids = [4 + 2 * index for index in range(len(texts))]
    content_ids = [5 + 2 * index for index in range(len(texts))]
    objects[1] = b'<< /Type /Catalog /Pages 2 0 R >>'
    kids = ' '.join(f'{page_id} 0 R' for page_id in page_ids)
    objects[2] = f'<< /Type /Pages /Kids [{kids}] /Count {len(texts)} >>'.encode()
    objects[3] = b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'
    for index, text in enumerate(texts):
        page_id, content_id = page_ids[index], content_ids[index]
        objects[page_id] = (
            f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
            f'/Contents {content_id} 0 R '
            f'/Resources << /Font << /F1 3 0 R >> >> >>'
        ).encode()
        stream = f'BT /F1 18 Tf 72 720 Td ({text}) Tj ET'.encode()
        objects[content_id] = (
            f'<< /Length {len(stream)} >>\nstream\n'.encode()
            + stream + b'\nendstream'
        )
    pdf = b'%PDF-1.4\n'
    offsets = {}
    for object_id in sorted(objects):
        offsets[object_id] = len(pdf)
        pdf += (f'{object_id} 0 obj\n'.encode()
                + objects[object_id] + b'\nendobj\n')
    xref = len(pdf)
    max_id = max(objects)
    pdf += f'xref\n0 {max_id + 1}\n'.encode() + b'0000000000 65535 f \n'
    for object_id in range(1, max_id + 1):
        if object_id in offsets:
            pdf += f'{offsets[object_id]:010d} 00000 n \n'.encode()
        else:
            pdf += b'0000000000 65535 f \n'
    pdf += (f'trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\n'
            f'startxref\n{xref}\n%%EOF\n').encode()
    return pdf


W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
X = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'


def _docx() -> bytes:
    return _zip({
        'word/styles.xml': f'''<w:styles xmlns:w="{W}">
  <w:style w:styleId="1"><w:name w:val="标题 1"/></w:style>
  <w:style w:styleId="2"><w:name w:val="heading 2"/></w:style>
</w:styles>''',
        'word/document.xml': f'''<w:document xmlns:w="{W}"><w:body>
<w:p><w:pPr><w:pStyle w:val="1"/></w:pPr>
  <w:r><w:t>Title</w:t></w:r></w:p>
<w:p><w:pPr><w:pStyle w:val="2"/></w:pPr>
  <w:r><w:t>Section</w:t></w:r></w:p>
<w:p><w:r><w:t>Hello </w:t></w:r><w:r><w:t>world</w:t></w:r></w:p>
<w:tbl>
  <w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>1</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>2</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl></w:body></w:document>''',
    })


def _pptx() -> bytes:
    return _zip({
        'ppt/presentation.xml': (
            f'<p:presentation xmlns:p="{P}" xmlns:r="{R}">'
            '<p:sldIdLst><p:sldId id="256" r:id="rId2"/>'
            '<p:sldId id="257" r:id="rId3"/></p:sldIdLst>'
            '</p:presentation>'),
        'ppt/_rels/presentation.xml.rels': (
            f'<Relationships xmlns="{PKG}">'
            '<Relationship Id="rId2" Type="slide" Target="slides/slide1.xml"/>'
            '<Relationship Id="rId3" Type="slide" Target="slides/slide2.xml"/>'
            '</Relationships>'),
        'ppt/slides/slide1.xml': (
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp>'
            '<p:txBody><a:bodyPr/><a:p><a:r><a:t>Hello slide</a:t></a:r></a:p>'
            '<a:p><a:pPr><a:buChar char="•"/></a:pPr>'
            '<a:r><a:t>Bullet</a:t></a:r></a:p></p:txBody></p:sp>'
            '</p:spTree></p:cSld></p:sld>'),
        'ppt/slides/_rels/slide1.xml.rels': (
            f'<Relationships xmlns="{PKG}">'
            '<Relationship Id="rId9" Type="notesSlide" '
            'Target="../notesSlides/notesSlide9.xml"/>'
            '</Relationships>'),
        'ppt/notesSlides/notesSlide9.xml': (
            f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>'
            '<p:sp><p:nvSpPr><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr>'
            '<p:txBody><a:bodyPr/><a:p><a:r><a:t>42</a:t></a:r></a:p>'
            '</p:txBody></p:sp>'
            '<p:sp><p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr>'
            '<p:txBody><a:bodyPr/><a:p><a:r><a:t>Speaker note</a:t></a:r></a:p>'
            '</p:txBody></p:sp></p:spTree></p:cSld></p:notes>'),
        'ppt/slides/slide2.xml': (
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp>'
            '<p:txBody><a:bodyPr/><a:p><a:r><a:t>Second slide</a:t></a:r></a:p>'
            '</p:txBody></p:sp></p:spTree></p:cSld></p:sld>'),
    })


def _xlsx() -> bytes:
    return _zip({
        'xl/workbook.xml': (
            f'<workbook xmlns="{X}" xmlns:r="{R}"><workbookPr/>'
            '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/>'
            '<sheet name="Other" sheetId="2" r:id="rId2"/></sheets></workbook>'),
        'xl/_rels/workbook.xml.rels': (
            f'<Relationships xmlns="{PKG}">'
            '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="worksheet" Target="worksheets/sheet2.xml"/>'
            '</Relationships>'),
        'xl/sharedStrings.xml': (
            f'<sst xmlns="{X}"><si><t>Name</t></si><si><t>Alice</t></si></sst>'),
        'xl/styles.xml': (
            f'<styleSheet xmlns="{X}"><cellXfs count="2">'
            '<xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>'),
        'xl/worksheets/sheet1.xml': (
            f'<worksheet xmlns="{X}"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c>'
            '<c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2"><v>42</v></c>'
            '<c r="B2" t="b"><v>1</v></c>'
            '<c r="C2" s="1"><v>45292</v></c></row>'
            '</sheetData></worksheet>'),
        'xl/worksheets/sheet2.xml': (
            f'<worksheet xmlns="{X}"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Other sheet</t></is></c></row>'
            '</sheetData></worksheet>'),
    })


def test_detect_kind_and_unsupported():
    assert documents.detect_kind(_pdf(['x'])) == 'pdf'
    assert documents.detect_kind(_docx()) == 'docx'
    assert documents.detect_kind(_pptx()) == 'pptx'
    assert documents.detect_kind(_xlsx()) == 'xlsx'
    assert documents.detect_kind(b'not a document') is None
    with pytest.raises(documents.DocumentError, match='unsupported'):
        documents.extract_document(b'plain text', 'x.txt')


@requires_pdf
def test_pdf_extracts_text_and_page_selection(tmp_path):
    data = _pdf(['First page', 'Second page'])
    output = documents.extract_document(data, 'demo.pdf')
    assert '--- page 1 ---' in output and 'First page' in output
    assert '--- page 2 ---' in output and 'Second page' in output
    selected = documents.extract_document(data, 'demo.pdf', pages='2')
    assert 'Second page' in selected and 'First page' not in selected


@requires_pdf
def test_pdf_default_page_limit_and_scan_hint():
    many = _pdf([f'Page {index}' for index in range(1, 22)])
    output = documents.extract_document(many, 'many.pdf')
    assert 'only the first 20 of 21 pages' in output
    assert '--- page 20 ---' in output and '--- page 21 ---' not in output

    # 一页空白内容模拟「无文本层」（0 页 PDF 不合法）
    blank = _pdf([' '])
    output = documents.extract_document(blank, 'blank.pdf')
    assert 'no text layer' in output and 'scanned PDF' in output


@requires_pdf
def test_pdf_rejects_bad_page_selection():
    data = _pdf(['one'])
    with pytest.raises(documents.DocumentError, match='1-based'):
        documents.extract_document(data, 'x.pdf', pages='0')
    with pytest.raises(documents.DocumentError, match='exceeds total'):
        documents.extract_document(data, 'x.pdf', pages='2')


def test_docx_extracts_headings_paragraphs_and_tables():
    output = documents.extract_document(_docx(), 'demo.docx')
    assert 'DOCX: demo.docx' in output
    # styleId 通过 styles.xml 映射到「标题 1」/「heading 2」
    assert '# Title' in output and '## Section' in output
    assert 'Hello world' in output
    assert '| A | B |' in output and '| 1 | 2 |' in output


def test_xml_dtd_and_entities_are_rejected():
    data = _zip({
        'word/document.xml': (
            f'<!DOCTYPE x [<!ENTITY a "boom">]><w:document xmlns:w="{W}">'
            '<w:body><w:p><w:r><w:t>&a;</w:t></w:r></w:p></w:body></w:document>'),
    })
    with pytest.raises(documents.DocumentError, match='DTD/entity'):
        documents.extract_document(data, 'bomb.docx')


def test_pptx_extracts_slides_bullets_notes_and_selection():
    output = documents.extract_document(_pptx(), 'demo.pptx')
    assert '--- slide 1 ---' in output and 'Hello slide' in output
    assert '- Bullet' in output
    assert '[notes]' in output and 'Speaker note' in output
    assert '42' not in output   # sldNum 占位符不算备注
    assert 'Second slide' in output
    selected = documents.extract_document(_pptx(), 'demo.pptx', pages='2')
    assert 'Second slide' in selected and 'Hello slide' not in selected


def test_xlsx_extracts_shared_strings_booleans_dates_and_sheets():
    output = documents.extract_document(_xlsx(), 'demo.xlsx')
    assert '--- sheet: Data' in output
    assert 'Name | Alice' in output
    assert '42 | TRUE | 2024-01-01' in output
    assert 'Other sheet' in output
    selected = documents.extract_document(_xlsx(), 'demo.xlsx', sheets='Other')
    assert 'Other sheet' in selected and 'Name | Alice' not in selected
    with pytest.raises(documents.DocumentError, match='unknown sheet'):
        documents.extract_document(_xlsx(), 'demo.xlsx', sheets='Missing')


def test_legacy_office_binary_is_reported():
    data = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 32
    assert documents.detect_kind(data) == 'ole'
    with pytest.raises(documents.DocumentError, match='legacy or encrypted Office'):
        documents.extract_document(data, 'old.doc')


def test_zip_member_size_limit(monkeypatch):
    # 上限由解析器所在模块读取，补丁也要打在 document_formats 上；
    # documents 里的同名常量只是兼容 re-export。
    monkeypatch.setattr(document_formats, 'MAX_ZIP_MEMBER_BYTES', 16)
    data = _docx()
    with pytest.raises(documents.DocumentError, match='too large'):
        documents.extract_document(data, 'big.docx')


def test_run_read_document_reads_file_and_read_points_to_it(tmp_path):
    (tmp_path / 'demo.docx').write_bytes(_docx())
    output = tools.run_read_document('demo.docx')
    assert '# Title' in output
    hint = tools.run_read('demo.docx')
    assert 'use read_document' in hint


def test_run_read_document_missing_and_secret(tmp_path):
    assert 'not found' in tools.run_read_document('nope.pdf')
    (tmp_path / '.env').write_bytes(_pdf(['secret']))
    denied = tools.run_read_document('.env')
    assert 'Error' in denied and 'Title' not in denied


def test_tool_is_registered_and_read_only():
    assert 'read_document' in tools.tool_registry
    schema = next(item for item in tools.TOOLS
                  if item['function']['name'] == 'read_document')
    assert schema['function']['parameters']['required'] == ['path']
    assert 'read_document' in exec_policy.READ_ONLY_FILE_TOOLS
    assert 'read_document' in harness.PARALLEL_SAFE_TOOLS
    assert Path(__file__).is_file()
