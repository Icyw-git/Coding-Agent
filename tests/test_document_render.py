"""render_document：PDF 渲染、Office 转 PDF、上限与工具接线。"""
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import document_render
import exec_policy
import harness
import tools
from test_documents import _docx, _pdf, _pptx, _xlsx

# 渲染依赖（pypdfium2 / Pillow）是可选的（requirements-docs.txt）。
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec('pypdfium2') is None
    or importlib.util.find_spec('PIL') is None,
    reason='document extras are not installed (requirements-docs.txt)')


def _open_image(data: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(data))


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    return tmp_path


def test_render_pdf_pages_as_png():
    rendered = document_render.render_document(
        _pdf(['First', 'Second', 'Third']), 'demo.pdf', pages='1-2', dpi=72)
    assert [number for number, _ in rendered] == [1, 2]
    for _, png in rendered:
        assert png.startswith(b'\x89PNG\r\n\x1a\n')
        image = _open_image(png)
        assert image.width > 0 and image.height > 0


def test_render_defaults_to_first_three_and_caps_eight():
    pages = document_render.render_document(
        _pdf([str(number) for number in range(1, 6)]), 'demo.pdf', dpi=72)
    assert [number for number, _ in pages] == [1, 2, 3]
    with pytest.raises(document_render.RenderError, match='at most 8'):
        document_render.render_document(
            _pdf([str(number) for number in range(1, 10)]), 'demo.pdf',
            pages='1-9', dpi=72)


def test_render_rejects_bad_dpi_and_unsupported_formats():
    for value in (71, 201, 'bad'):
        with pytest.raises(document_render.RenderError, match='dpi'):
            document_render.render_document(_pdf(['x']), 'x.pdf', dpi=value)
    with pytest.raises(document_render.RenderError, match='XLSX'):
        document_render.render_document(_xlsx(), 'x.xlsx')


def test_office_requires_soffice(monkeypatch):
    monkeypatch.setattr(document_render, '_find_soffice', lambda: None)
    for name, data in (('a.docx', _docx()), ('a.pptx', _pptx())):
        with pytest.raises(document_render.RenderError, match='LibreOffice'):
            document_render.render_document(data, name)


def test_office_rejects_macro_enabled_document(monkeypatch):
    monkeypatch.setattr(document_render, '_find_soffice', lambda: sys.executable)
    data = _docx_with_macro()
    with pytest.raises(document_render.RenderError, match='macro-enabled'):
        document_render.render_document(data, 'macro.docx')


def test_office_conversion_invokes_soffice(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        outdir = Path(command[command.index('--outdir') + 1])
        (outdir / 'source.pdf').write_bytes(_pdf(['converted']))
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(document_render, '_find_soffice', lambda: 'soffice')
    monkeypatch.setattr(document_render.subprocess, 'run', fake_run)
    pdf = document_render._office_to_pdf(_docx(), '.docx')
    assert pdf.startswith(b'%PDF-')
    command = commands[0]
    assert '--headless' in command
    assert '--convert-to' in command and 'pdf' in command
    assert any('source.docx' in part for part in command)


def test_render_document_returns_cached_image_refs(tmp_path):
    (tmp_path / 'demo.pdf').write_bytes(_pdf(['layout', 'second']))
    result = tools.run_render_document('demo.pdf', pages='1-2', dpi=72)
    assert isinstance(result, tools.ImageResult)
    assert 'attached' in result.text and 'page(s) 1, 2' in result.text
    assert len(result.images) == 2
    for number, ref in enumerate(result.images, 1):
        assert ref['type'] == 'harness_image'
        assert ref['mime'] == 'image/png'
        assert ref['tokens'] >= 1
        assert ref['name'] == f'demo-page{number}.png'
        assert Path(ref['path']).read_bytes().startswith(b'\x89PNG')


def test_render_document_missing_and_secret(tmp_path):
    assert 'not found' in tools.run_render_document('nope.pdf')
    (tmp_path / '.env').write_bytes(_pdf(['secret']))
    denied = tools.run_render_document('.env')
    assert 'Error' in denied


def test_render_tool_is_registered_and_read_only():
    assert 'render_document' in tools.tool_registry
    assert 'render_document' in exec_policy.READ_ONLY_FILE_TOOLS
    schema = next(item for item in tools.TOOLS
                  if item['function']['name'] == 'render_document')
    assert schema['function']['parameters']['required'] == ['path']


def _docx_with_macro() -> bytes:
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        with zipfile.ZipFile(io.BytesIO(_docx())) as source:
            for name in source.namelist():
                archive.writestr(name, source.read(name))
        archive.writestr('word/vbaProject.bin', b'macro')
    return buffer.getvalue()
