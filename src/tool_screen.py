"""screen_capture / screen_ocr 工具层：落盘、审计与默认截图选择。

截图固定落在 harness 的 cache（`SCREENSHOT_DIR`），不写工作区；OCR 只接受这个
目录里的文件，避免把工具变成「对任意路径做内容识别」的入口。审计只记路径与
计数，不记 OCR 文本本身。
"""
from __future__ import annotations

from pathlib import Path

import ocr_windows
import screen_capture
from agent_core import estimate_image_tokens
from tool_documents import ImageResult

MAX_OCR_TEXT_CHARS = 12_000
MAX_OCR_LINES = 200


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _notify(audit, event: str, **payload) -> None:
    if audit is None:
        return
    try:
        audit(event, **payload)
    except Exception:
        pass


def _screen_summary(shot: screen_capture.Capture) -> str:
    parts = [f'{item.get("name", "?")}'
             f'{" (primary)" if item.get("primary") else ""} '
             f'{tuple(item.get("bounds") or [])}'
             for item in shot.screens]
    return '; '.join(parts) if parts else 'unknown'


def capture(screen: str = 'primary', region=None, delay_ms=0, *,
            directory, runner=None, audit=None) -> str | ImageResult:
    try:
        shot = screen_capture.capture(
            screen=screen, region=region, delay_ms=delay_ms, runner=runner)
        target = screen_capture.save_png(shot.png, directory, meta={
            'screen': shot.screen,
            'width': shot.width, 'height': shot.height,
            'region': list(shot.region) if shot.region else None,
            'origin': list(shot.origin),
        })
    except screen_capture.ScreenError as exc:
        return f'截屏失败：{exc}'
    except OSError as exc:
        return f'截图存不下来：{exc}'
    region_text = (','.join(str(part) for part in shot.region)
                   if shot.region else 'full')
    text = (
        f'截图已保存：{target}\n'
        f'尺寸 {shot.width}x{shot.height}px，{len(shot.png)} 字节\n'
        f'屏幕 {shot.screen}，区域 {region_text}，用时 {shot.duration_ms}ms\n'
        f'显示器：{_screen_summary(shot)}\n'
        f'坐标系：绝对屏幕像素，图片左上角对应屏幕 ({shot.origin[0]},'
        f'{shot.origin[1]})；desktop_action 的 x/y 用同一套坐标。\n'
        '图片在接下来的用户消息里，回答前先看一眼。要取文字用 screen_ocr，'
        '它会直接给出可点击的屏幕坐标。')
    _notify(audit, 'screen_captured', path=str(target), sha1=target.stem,
            bytes=len(shot.png), screen=shot.screen,
            region=list(shot.region) if shot.region else None,
            width=shot.width, height=shot.height,
            duration_ms=shot.duration_ms)
    return ImageResult(text, [{
        'type': 'harness_image',
        'path': str(target),
        'mime': 'image/png',
        'name': target.name,
        'bytes': len(shot.png),
        'tokens': estimate_image_tokens(shot.png),
    }])


def _resolve_screenshot(path, directory) -> Path:
    root = Path(directory)
    if path is None or not str(path).strip():
        latest = screen_capture.latest_png(root)
        if latest is None:
            raise ocr_windows.OcrError(
                '还没有截图；先调一次 screen_capture')
        return latest
    candidate = Path(str(path)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if not _inside(candidate, root):
        raise ocr_windows.OcrError(
            f'OCR is limited to screenshots captured by screen_capture '
            f'({root}); got {candidate}')
    if not candidate.is_file():
        raise ocr_windows.OcrError(f'找不到这张截图：{candidate}')
    return candidate


def _line_center(line: dict) -> tuple[int, int] | None:
    bbox = line.get('bbox') or []
    if len(bbox) != 4:
        return None
    x, y, width, height = (int(part) for part in bbox)
    return x + width // 2, y + height // 2


def _format_lines(lines: list[dict], origin: tuple[int, int] | None = None) -> str:
    rendered = []
    for index, line in enumerate(lines[:MAX_OCR_LINES], start=1):
        bbox = ','.join(str(part) for part in line['bbox'])
        head = f'{index}. '
        if origin is not None:
            center = _line_center(line)
            if center is not None:
                head += f'@({center[0] + origin[0]},{center[1] + origin[1]}) '
        rendered.append(f'{head}[{bbox}] {line["text"]}')
    if len(lines) > MAX_OCR_LINES:
        rendered.append(f'... (还有 {len(lines) - MAX_OCR_LINES} 行未显示)')
    return '\n'.join(rendered)


def ocr(path=None, lang: str = ocr_windows.DEFAULT_LANGUAGE, region=None, *,
        directory, runner=None, audit=None) -> str:
    try:
        target = _resolve_screenshot(path, directory)
        result = ocr_windows.recognize(
            target, lang=lang, region=region, runner=runner)
    except (ocr_windows.OcrError, screen_capture.ScreenError) as exc:
        return f'识别失败：{exc}'
    _notify(audit, 'screen_ocr', path=str(target), chars=len(result.text),
            lang=result.language, region=list(result.region) if result.region else None,
            lines=len(result.lines), duration_ms=result.duration_ms)
    meta = screen_capture.load_meta(target)
    origin = None
    if meta and isinstance(meta.get('origin'), (list, tuple)):
        try:
            origin = (int(meta['origin'][0]), int(meta['origin'][1]))
        except (IndexError, TypeError, ValueError):
            origin = None
    header = (f'识别结果（{result.language}）{result.width}x{result.height}px，'
              f'{len(result.lines)} 行、{len(result.text)} 字，'
              f'用时 {result.duration_ms}ms · {target}')
    if not result.text.strip() and not result.lines:
        return f'{header}\n没有识别到文字。'
    text = result.text
    if len(text) > MAX_OCR_TEXT_CHARS:
        text = (text[:MAX_OCR_TEXT_CHARS]
                + f'\n...（截断，原文 {len(result.text)} 字）')
    blocks = [header, text.strip() or '（没有纯文本内容）']
    if result.lines:
        if origin is not None:
            lead = (f'每行的 @(x,y) 是该行中心点的屏幕坐标，'
                    f'可以直接给 desktop_action 当 x/y 用；方括号里是图像内的 '
                    f'bbox x,y,w,h。截图原点 ({origin[0]},{origin[1]})。')
            blocks.append(lead + '\n' + _format_lines(result.lines, origin))
        else:
            blocks.append('这张截图没有留下坐标信息（不是 screen_capture 生成的），'
                          '下面的 bbox 只能按图像像素理解。\n'
                          + _format_lines(result.lines))
    return '\n\n'.join(blocks)
