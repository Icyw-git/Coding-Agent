"""图片与文档工具：view_image / read_document / render_document。

图片不进 tool 消息，而是按内容哈希落到工具缓存目录，由
`agent_core.expand_image_parts` 在请求前展开成 data URI。路径解析、边界门与
缓存根目录由 tools.py 注入，本模块不读取工作区全局状态。
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import document_render
import documents
from agent_core import estimate_image_tokens
from tool_text import BinaryFileError

IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_SIGNATURES = (
    (b'\x89PNG\r\n\x1a\n', 'image/png', '.png'),
    (b'\xff\xd8\xff', 'image/jpeg', '.jpg'),
    (b'GIF87a', 'image/gif', '.gif'),
    (b'GIF89a', 'image/gif', '.gif'),
    (b'BM', 'image/bmp', '.bmp'),
)


class ImageResult:
    """工具结果：给模型看的文本 + 需要以 user 多模态消息注入的图片。

    图片本体不塞进 tool 结果（OpenAI Chat Completions 的 tool 消息对图片支持
    并不一致），而是落盘成缓存文件，由 agent_core.expand_image_parts 在每次
    请求前转成 data URI。消息历史里只留引用，压缩/转录不会被 base64 撑爆。
    """

    def __init__(self, text: str, images: list[dict] | None = None):
        self.text = text
        self.images = images or []


def image_type(data: bytes) -> tuple[str, str] | None:
    for magic, mime, suffix in IMAGE_SIGNATURES:
        if data.startswith(magic):
            return mime, suffix
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp', '.webp'
    return None


def cache_image(data: bytes, suffix: str, base_dir) -> Path:
    """把图片落到工具缓存目录；同一内容只写一次。"""
    directory = (Path(base_dir) / 'images' if base_dir is not None
                 else Path(tempfile.gettempdir()) / 'harness-images')
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f'{hashlib.sha1(data).hexdigest()}{suffix}'
    if not target.exists():
        target.write_bytes(data)
    return target


def view_image(path: str, cwd: str | None, *, gate, resolve,
               max_bytes: int = IMAGE_MAX_BYTES, cache_dir=None):
    denied = gate('view_image', path, cwd)
    if denied:
        return denied
    try:
        file_path = resolve(path, cwd)
        if not file_path.is_file():
            return f'Error: image not found: {path}'
        data = file_path.read_bytes()
        if len(data) > max_bytes:
            return (f'Error: image is too large ({len(data)} bytes; max '
                    f'{max_bytes}). Resize it first.')
        detected = image_type(data)
        if detected is None:
            return (f'Error: unsupported image format: {path} '
                    '(supported: PNG, JPEG, GIF, WebP, BMP)')
        mime, suffix = detected
        cached = cache_image(data, suffix, cache_dir)
        text = (f'Viewed image: {file_path.name} ({mime}, {len(data)} bytes). '
                'The image is attached in the next user message; inspect it before '
                'answering.')
        return ImageResult(text, [{
            'type': 'harness_image',
            'path': str(cached),
            'mime': mime,
            'name': file_path.name,
            'bytes': len(data),
            'tokens': estimate_image_tokens(data),
        }])
    except BinaryFileError as exc:
        return f'Error: {exc}'
    except Exception as exc:
        return f'Error: {exc}'


def read_document(path: str, cwd: str | None, *, gate, resolve,
                  pages: str | None = None, sheets: str | None = None) -> str:
    denied = gate('read_document', path, cwd)
    if denied:
        return denied
    try:
        file_path = resolve(path, cwd)
        if not file_path.is_file():
            return f'Error: document not found: {path}'
        data = file_path.read_bytes()
        return documents.extract_document(
            data, file_path.name, pages=pages, sheets=sheets)
    except documents.DocumentError as exc:
        return f'Error: {exc}'
    except Exception as exc:
        return f'Error: {exc}'


def render_document(path: str, cwd: str | None, *, gate, resolve,
                    pages: str | None = None, dpi: int = 110,
                    cache_dir=None):
    denied = gate('render_document', path, cwd)
    if denied:
        return denied
    try:
        file_path = resolve(path, cwd)
        if not file_path.is_file():
            return f'Error: document not found: {path}'
        data = file_path.read_bytes()
        rendered = document_render.render_document(
            data, file_path.name, pages=pages, dpi=dpi)
        if not rendered:
            return f'Error: no pages rendered from {path}'
        images = []
        for number, png in rendered:
            cached = cache_image(png, '.png', cache_dir)
            images.append({
                'type': 'harness_image',
                'path': str(cached),
                'mime': 'image/png',
                'name': f'{file_path.stem}-page{number}.png',
                'bytes': len(png),
                'tokens': estimate_image_tokens(png),
            })
        page_text = ', '.join(str(number) for number, _ in rendered)
        total_kib = sum(len(png) for _, png in rendered) / 1024
        text = (f'Rendered page(s) {page_text} of {file_path.name} at {dpi} dpi '
                f'({len(images)} image(s), {total_kib:.0f} KiB). The images are '
                'attached in the next user message; inspect layout, charts, tables, '
                'and scanned text. Use read_document when you only need text.')
        return ImageResult(text, images)
    except document_render.RenderError as exc:
        return f'Error: {exc}'
    except Exception as exc:
        return f'Error: {exc}'
