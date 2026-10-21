"""tool_text：BOM/编码探测、二进制拒绝与换行符保留。"""
import pytest

from tool_text import (
    BinaryFileError,
    decode_text,
    detect_newline,
    encode_text,
    read_text_file,
)


def test_reads_bom_encodings_and_normalizes_newlines(tmp_path):
    path = tmp_path / 'utf16.txt'
    path.write_bytes('第一行\r\n第二行'.encode('utf-16'))

    text, encoding, newline = read_text_file(path)

    assert text == '第一行\n第二行'
    assert encoding == 'utf-16'
    assert newline == '\r\n'


def test_decodes_gb18030_when_utf8_fails():
    text, encoding = decode_text('中文内容'.encode('gb18030'))

    assert text == '中文内容'
    assert encoding == 'gb18030'


def test_rejects_magic_and_nul_bytes():
    with pytest.raises(BinaryFileError, match='binary'):
        decode_text(b'\x89PNG\r\n\x1a\n' + b'payload')
    with pytest.raises(BinaryFileError, match='NUL'):
        decode_text(b'a\x00b')


def test_encode_restores_original_newline():
    assert encode_text('a\nb', 'utf-8', '\r\n') == b'a\r\nb'
    assert encode_text('a\nb', 'utf-8', '\n') == b'a\nb'
    assert detect_newline('a\r\nb') == '\r\n'
    assert detect_newline('a\rb') == '\r'
    assert detect_newline('a\nb') == '\n'
