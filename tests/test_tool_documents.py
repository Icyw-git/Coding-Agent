"""tool_documents：边界门、缓存去重与渲染结果的图片引用。"""
from pathlib import Path

import tool_documents


def _access(root, *, denied=None, log=None):
    log = [] if log is None else log

    def gate(tool, path, cwd):
        log.append(('gate', tool))
        return denied

    def resolve(path, cwd):
        log.append(('resolve', path))
        return root / path

    return gate, resolve


def test_view_image_denial_skips_resolution(tmp_path):
    log = []
    gate, resolve = _access(tmp_path, denied='Error: denied', log=log)

    result = tool_documents.view_image(
        'x.png', None, gate=gate, resolve=resolve, cache_dir=tmp_path)

    assert result == 'Error: denied'
    assert [entry[0] for entry in log] == ['gate']


def test_view_image_reports_unsupported_format(tmp_path):
    gate, resolve = _access(tmp_path)
    (tmp_path / 'notes.txt').write_text('text', encoding='utf-8')

    result = tool_documents.view_image(
        'notes.txt', None, gate=gate, resolve=resolve, cache_dir=tmp_path)

    assert 'unsupported image format' in result


def test_cache_image_is_content_addressed(tmp_path):
    first = tool_documents.cache_image(b'png-bytes', '.png', tmp_path)
    second = tool_documents.cache_image(b'png-bytes', '.png', tmp_path)

    assert first == second
    assert first.parent.name == 'images'
    assert first.read_bytes() == b'png-bytes'
    assert len(list((tmp_path / 'images').iterdir())) == 1


def test_render_document_returns_cached_page_refs(tmp_path, monkeypatch):
    (tmp_path / 'doc.pdf').write_bytes(b'%PDF-1.4')
    monkeypatch.setattr(
        'tool_documents.document_render.render_document',
        lambda data, name, pages=None, dpi=110: [(1, b'page-one')])
    gate, resolve = _access(tmp_path)
    cache = tmp_path / 'cache'

    result = tool_documents.render_document(
        'doc.pdf', None, gate=gate, resolve=resolve,
        pages='1', dpi=72, cache_dir=cache)

    assert isinstance(result, tool_documents.ImageResult)
    assert 'page(s) 1' in result.text
    ref = result.images[0]
    assert ref['name'] == 'doc-page1.png'
    assert Path(ref['path']).parent.name == 'images'
    assert Path(ref['path']).is_file()
