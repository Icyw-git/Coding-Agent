"""read/edit 的分段、编码与替换语义。"""

import pytest

import tools


@pytest.fixture(autouse=True)
def allow_workspace_writes(monkeypatch):
    """临时目录不在仓库工作区内；用完全自主档避免测试卡在审批上。"""
    monkeypatch.setenv('HARNESS_SANDBOX_MODE', 'danger-full-access')


def test_read_supports_offset_and_reports_the_next_offset(tmp_path):
    path = tmp_path / 'lines.txt'
    path.write_text('\n'.join(f'line {i}' for i in range(10)), encoding='utf-8')

    out = tools.run_read(str(path), offset=3, limit=2)
    assert out == 'line 3\nline 4\n... (5 more lines; use offset=5)'


def test_read_default_truncation_can_continue_from_the_hint(tmp_path):
    path = tmp_path / 'big.txt'
    path.write_text('\n'.join(f'line {i}' for i in range(600)), encoding='utf-8')

    first = tools.run_read(str(path))
    assert 'line 0' in first
    assert 'line 199' in first
    assert 'line 200' not in first
    assert 'use offset=200' in first

    rest = tools.run_read(str(path), offset=200, limit=400)
    assert rest.startswith('line 200')
    assert rest.endswith('line 599')
    assert 'more lines' not in rest


def test_read_handles_offset_past_eof_and_invalid_values(tmp_path):
    path = tmp_path / 'small.txt'
    path.write_text('only', encoding='utf-8')

    assert 'no lines at offset 5' in tools.run_read(str(path), offset=5)
    assert tools.run_read(str(path), offset=-1).startswith('Error:')
    assert tools.run_read(str(path), limit=-1).startswith('Error:')


def test_read_decodes_gbk_and_utf16_without_mojibake(tmp_path):
    gbk = tmp_path / 'gbk.txt'
    gbk.write_bytes('中文标题\n第二行'.encode('gbk'))
    assert tools.run_read(str(gbk)) == '中文标题\n第二行'

    utf16 = tmp_path / 'utf16.txt'
    utf16.write_text('中文内容\n第二行', encoding='utf-16')
    assert tools.run_read(str(utf16)) == '中文内容\n第二行'


def test_read_rejects_binary_files(tmp_path):
    path = tmp_path / 'image.png'
    path.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR')

    out = tools.run_read(str(path))
    assert out.startswith('Error: binary file')


def test_edit_requires_unique_match_unless_replace_all(tmp_path):
    path = tmp_path / 'dup.txt'
    path.write_text('x\nx\n', encoding='utf-8')

    out = tools.run_edit(str(path), 'x', 'y')
    assert 'appears 2 times' in out
    assert path.read_text(encoding='utf-8') == 'x\nx\n'

    assert tools.run_edit(str(path), 'x', 'y', replace_all=True) == f'Edited {path}'
    assert path.read_text(encoding='utf-8') == 'y\ny\n'
    assert tools.run_edit(str(path), 'missing', 'z').startswith('Error: text not found')
    assert tools.run_edit(str(path), '', 'z').startswith('Error: old_text must not be empty')


def test_edit_preserves_gbk_encoding(tmp_path):
    path = tmp_path / 'gbk.txt'
    path.write_bytes('旧标题\n正文'.encode('gbk'))

    assert tools.run_edit(str(path), '旧标题', '新标题') == f'Edited {path}'
    raw = path.read_bytes()
    assert raw.decode('gbk') == '新标题\n正文'
    assert not raw.startswith(b'\xef\xbb\xbf')


def test_edit_preserves_utf16_and_crlf(tmp_path):
    path = tmp_path / 'utf16.txt'
    path.write_bytes('旧标题\r\n正文\r\n'.encode('utf-16'))

    assert tools.run_edit(str(path), '旧标题\n正文', '新标题\n内容') == f'Edited {path}'
    assert path.read_bytes().decode('utf-16') == '新标题\r\n内容\r\n'


def test_edit_preserves_plain_crlf(tmp_path):
    path = tmp_path / 'crlf.txt'
    path.write_bytes(b'a\r\nb\r\n')

    assert tools.run_edit(str(path), 'a\nb', 'x\ny') == f'Edited {path}'
    assert path.read_bytes() == b'x\r\ny\r\n'


def test_write_preserves_existing_encoding(tmp_path):
    path = tmp_path / 'gbk.txt'
    path.write_bytes('旧内容'.encode('gbk'))

    tools.run_write(str(path), '新内容', cwd=str(tmp_path))
    assert path.read_bytes().decode('gbk') == '新内容'


def test_grep_decodes_gbk_and_utf16(tmp_path):
    (tmp_path / 'gbk.txt').write_bytes('中文关键词'.encode('gbk'))
    (tmp_path / 'utf16.txt').write_text('另一个关键词', encoding='utf-16')

    out = tools.run_grep('关键词', path=str(tmp_path))
    assert 'gbk.txt:1' in out
    assert 'utf16.txt:1' in out
