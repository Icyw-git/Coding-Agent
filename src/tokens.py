"""Token 估算：文本启发式与多模态图片成本。

只用于上下文保护（压缩阈值），不是账单 token：宁可高估，避免窗口被算爆。
模型名通过 ``llm_route.model_for`` 读取，测试 patch harness 的模型配置仍生效。
"""
import base64
import math
import os
import re
import struct
from pathlib import Path

import llm_route
from llm_route import harness as _h

# 上下文预算用的固定兜底，不是账单 token。常见值：GPT-4o 1024² ≈ 765、
# Claude 1024² ≈ 1398、Gemini 1024² ≈ 1032、DeepSeek 每图上限 1024。图片实际
# 成本随尺寸/模型/provider 变化，这里只要求「不把窗口算爆」；已知尺寸时
# estimate_image_tokens 会覆盖它。
IMAGE_TOKEN_ESTIMATE = 1000
# DeepSeek 官方只公布「缩放后每图最多 1024 token」，没有公开瓦片公式；
# 预算按上限计，宁可高估。
DEEPSEEK_IMAGE_TOKEN_LIMIT = 1024


def _env_image_token_estimate() -> int:
    """未知尺寸/旧引用用的固定兜底值；环境变量可调。"""
    raw = os.getenv('HARNESS_IMAGE_TOKEN_ESTIMATE')
    if not raw:
        return IMAGE_TOKEN_ESTIMATE
    try:
        return max(1, int(raw))
    except ValueError:
        return IMAGE_TOKEN_ESTIMATE


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """从文件头读宽高（不依赖 Pillow）；识别不了返回 None。"""
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
        return struct.unpack('>II', data[16:24])
    if data.startswith((b'GIF87a', b'GIF89a')) and len(data) >= 10:
        return struct.unpack('<HH', data[6:10])
    if data.startswith(b'BM') and len(data) >= 26:
        dib = int.from_bytes(data[14:18], 'little')
        if dib == 12:
            width, height = struct.unpack('<HH', data[18:22])
            return abs(width), abs(height)
        width, height = struct.unpack('<ii', data[18:26])
        return abs(width), abs(height)
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        chunk = data[12:16]
        if chunk == b'VP8X' and len(data) >= 30:
            return (1 + int.from_bytes(data[24:27], 'little'),
                    1 + int.from_bytes(data[27:30], 'little'))
        if chunk == b'VP8L' and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], 'little')
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if (chunk == b'VP8 ' and len(data) >= 30
                and data[23:26] == b'\x9d\x01\x2a'):
            return (int.from_bytes(data[26:28], 'little') & 0x3FFF,
                    int.from_bytes(data[28:30], 'little') & 0x3FFF)
    if data.startswith(b'\xff\xd8'):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index:index + 2], 'big')
            if length < 2 or index + length > len(data):
                break
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(data[index + 3:index + 5], 'big')
                width = int.from_bytes(data[index + 5:index + 7], 'big')
                return width, height
            index += length
    return None


def _openai_image_tokens(width: int, height: int) -> int:
    """GPT-4o 高细节规则：先塞进 2048²，短边缩到 768，再按 512 瓦片计。"""
    scale = min(1.0, 2048 / max(width, height))
    width, height = max(1, int(width * scale)), max(1, int(height * scale))
    if min(width, height) > 768:
        scale = 768 / min(width, height)
        width, height = max(1, int(width * scale)), max(1, int(height * scale))
    tiles = math.ceil(width / 512) * math.ceil(height / 512)
    return 85 + 170 * tiles


def _claude_image_tokens(width: int, height: int) -> int:
    # 旧版 Claude 文档的经验值：长边超过 1568 会缩放，约 (w*h)/750 且上限 ~1600
    scale = min(1.0, 1568 / max(width, height))
    width, height = max(1, int(width * scale)), max(1, int(height * scale))
    return min(1600, max(1, (width * height + 749) // 750))


def _gemini_image_tokens(width: int, height: int) -> int:
    return (max(1, math.ceil(width / 768)) * max(1, math.ceil(height / 768))
            * 258)


def _deepseek_image_tokens(width: int, height: int) -> int:
    # 官方只给「缩放后每图最多 1024」，没有公开公式；按上限保守计。
    return DEEPSEEK_IMAGE_TOKEN_LIMIT


def estimate_image_tokens(data: bytes, model: str | None = None) -> int:
    """按当前模型族估算图片 token；尺寸未知时退回固定值。

    这是上下文保护，不是计费：宁可略高，避免压缩阈值被低估。设置
    HARNESS_IMAGE_TOKEN_ESTIMATE 可覆盖固定兜底值。
    """
    fallback = _env_image_token_estimate()
    dimensions = image_dimensions(data)
    if dimensions is None:
        return fallback
    width, height = dimensions
    if width <= 0 or height <= 0:
        return fallback
    model_name = (model or llm_route.model_for('main') or '').lower()
    if 'claude' in model_name or 'anthropic' in model_name:
        estimate = _claude_image_tokens(width, height)
    elif 'gemini' in model_name:
        estimate = _gemini_image_tokens(width, height)
    elif 'deepseek' in model_name:
        estimate = _deepseek_image_tokens(width, height)
    else:
        estimate = _openai_image_tokens(width, height)
    return max(fallback, estimate)


def image_attachment_message(images: list[dict]) -> dict:
    """把 view_image 的附件包成一条紧随工具结果的 user 消息。

    Chat Completions 的 tool 消息对图片支持不一致（部分模型只认 user 里的
    image_url），所以图片走独立的 user 消息，tool 结果只保留文字摘要。
    """
    names = ', '.join(str(image.get('name') or 'image') for image in images)
    content = [{
        'type': 'text',
        'text': f'[image attachment] {names}. '
                'Inspect the attached image before continuing.',
    }]
    content.extend(dict(image) for image in images)
    return {'role': 'user', 'content': content}


def expand_image_parts(messages: list[dict]) -> list[dict]:
    """把 harness_image 引用展开成 OpenAI 的 image_url data URI。

    只复制含图片的消息，普通消息保持原对象；文件被清理时降级成一条文本，
    不让一次读图失败拖垮整个请求。只读 view_image 自己的缓存目录：会话事件
    可以被用户编辑，引用指向 `.env` 这类文件时不能借多模态通道把它送出去。
    """
    try:
        base = getattr(_h(), 'TOOL_RESULTS_DIR', None)
        cache_root = (Path(base) / 'images').resolve() if base else None
    except Exception:
        cache_root = None
    result = []
    for message in messages:
        content = message.get('content')
        if not isinstance(content, list) or not any(
                isinstance(part, dict) and part.get('type') == 'harness_image'
                for part in content):
            result.append(message)
            continue
        parts = []
        for part in content:
            if not isinstance(part, dict) or part.get('type') != 'harness_image':
                parts.append(part)
                continue
            path = Path(str(part.get('path') or ''))
            name = str(part.get('name') or path.name or 'image')
            try:
                resolved = path.resolve()
                if cache_root is not None and not resolved.is_relative_to(cache_root):
                    raise OSError('outside image cache')
                data = resolved.read_bytes()
            except (OSError, ValueError):
                parts.append({'type': 'text',
                              'text': f'[image unavailable: {name}]'})
                continue
            mime = str(part.get('mime') or 'image/png')
            encoded = base64.b64encode(data).decode('ascii')
            parts.append({'type': 'image_url',
                          'image_url': {'url': f'data:{mime};base64,{encoded}'}})
        updated = dict(message)
        updated['content'] = parts
        result.append(updated)
    return result


def image_token_cost(msgs) -> tuple[int, int]:
    """遍历消息里的图片 part，返回 (token 合计, 已计数的 part 数)。"""
    fallback = _env_image_token_estimate()
    total = 0
    seen = 0
    for message in msgs:
        content = (message.get('content') if isinstance(message, dict)
                   else getattr(message, 'content', None))
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get('type')
            if kind == 'harness_image':
                try:
                    total += int(part.get('tokens') or fallback)
                except (TypeError, ValueError):
                    total += fallback
                seen += 1
            elif kind == 'image_url':
                total += fallback
                seen += 1
    return total, seen


def estimate_size(msgs):
    # token 启发式：英文约 3.5 字符/token，中文约 1.3 字符/token（与 CONTEXT_LIMIT 同单位）
    raw = str(msgs)
    # 历史里正常只有引用；万一是旧消息里的 data URI，也不能按字符数把上下文算爆。
    # 仅在可能出现 data URI 时才跑正则：长会话里 str(msgs) 再加一次全串替换是纯开销。
    if 'data:' in raw:
        text = re.sub(r'data:[^;\s]+;base64,[A-Za-z0-9+/=]+', '[image]', raw)
    else:
        text = raw
    # 逐个 ord() 在纯 Python 里是长会话的主要成本；encode('ascii','ignore')
    # 用 C 层过滤出 ASCII 字符，计数结果与 `sum(1 for ch if ord(ch) < 128)` 一致。
    ascii_chars = len(text.encode('ascii', 'ignore'))
    cjk = len(text) - ascii_chars
    image_tokens, seen = image_token_cost(msgs)
    # 旧历史里若有字符串形态的图片引用，遍历不到：按 part 出现次数补齐。
    raw_images = raw.count("'harness_image'") + raw.count("'image_url'")
    image_tokens += max(0, raw_images - seen) * _env_image_token_estimate()
    return max(1, int(ascii_chars / 3.5 + cjk / 1.3) + image_tokens)
