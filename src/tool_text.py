"""文件文本解码/编码原语：BOM 探测、二进制拒绝与换行符保留。

read / edit / apply_patch / grep 共用同一份判定：编码不一致时，同一文件会在
不同工具间表现分裂（Windows 上 GBK/UTF-16 尤其明显）。本模块只处理字节，
不解析路径、不读工作区状态。
"""
from __future__ import annotations

import codecs
from pathlib import Path


class BinaryFileError(ValueError):
    pass


TEXT_BOMS = (
    (codecs.BOM_UTF32_LE, 'utf-32'),
    (codecs.BOM_UTF32_BE, 'utf-32'),
    (codecs.BOM_UTF8, 'utf-8-sig'),
    (codecs.BOM_UTF16_LE, 'utf-16'),
    (codecs.BOM_UTF16_BE, 'utf-16'),
)

BINARY_MAGIC = (
    b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF87a', b'GIF89a',
    b'%PDF-', b'PK\x03\x04', b'\x7fELF', b'MZ',
)


def looks_binary_text(text: str) -> bool:
    sample = text[:4096]
    if not sample:
        return False
    controls = sum(1 for ch in sample if ord(ch) < 32 and ch not in '\n\r\t')
    return controls > max(8, len(sample) // 100)


def decode_text(data: bytes) -> tuple[str, str]:
    """按 BOM → UTF-8 → GB18030 解码；二进制内容抛 BinaryFileError。"""
    for bom, encoding in TEXT_BOMS:
        if data.startswith(bom):
            return data.decode(encoding), encoding
    if data.startswith(BINARY_MAGIC):
        raise BinaryFileError('binary file (not text)')
    if b'\x00' in data:
        raise BinaryFileError('binary file (contains NUL bytes)')
    try:
        return data.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode('gb18030')
    except UnicodeDecodeError:
        return data.decode('utf-8', errors='replace'), 'utf-8'
    if looks_binary_text(text):
        raise BinaryFileError('binary file (not text)')
    return text, 'gb18030'


def detect_newline(text: str) -> str:
    if '\r\n' in text:
        return '\r\n'
    if '\r' in text:
        return '\r'
    return '\n'


def read_text_file(path: Path) -> tuple[str, str, str]:
    """返回 (统一为 \\n 的文本, 编码, 原换行符)。"""
    text, encoding = decode_text(path.read_bytes())
    newline = detect_newline(text)
    return text.replace('\r\n', '\n').replace('\r', '\n'), encoding, newline


def encode_text(text: str, encoding: str, newline: str) -> bytes:
    if newline != '\n':
        text = text.replace('\r\n', '\n').replace('\n', newline)
    return text.encode(encoding)
