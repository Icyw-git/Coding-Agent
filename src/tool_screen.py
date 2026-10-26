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
        target = screen_capture.save_png(shot.png, directory)
    except screen_capture.ScreenError as exc:
        return f'Error: {exc}'
    except OSError as exc:
        return f'Error: cannot save screenshot: {exc}'
    region_text = (','.join(str(part) for part in shot.region)
                   if shot.region else 'full')
    text = (
        f'Screenshot saved: {target}\n'
        f'Size: {shot.width}x{shot.height}px, {len(shot.png)} bytes\n'
        f'Screen: {shot.screen}, region: {region_text}, '
        f'capture took {shot.duration_ms}ms\n'
        f'Displays: {_screen_summary(shot)}\n'
        'The image is attached in the next user message; inspect it before '
        'answering. Use screen_ocr with this path to extract text.')
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
                'no screenshots yet; call screen_capture first')
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
        raise ocr_windows.OcrError(f'screenshot not found: {candidate}')
    return candidate


def _format_lines(lines: list[dict]) -> str:
    rendered = []
    for index, line in enumerate(lines[:MAX_OCR_LINES], start=1):
        bbox = ','.join(str(part) for part in line['bbox'])
        rendered.append(f'{index}. [{bbox}] {line["text"]}')
    if len(lines) > MAX_OCR_LINES:
        rendered.append(f'... ({len(lines) - MAX_OCR_LINES} more lines)')
    return '\n'.join(rendered)


def ocr(path=None, lang: str = ocr_windows.DEFAULT_LANGUAGE, region=None, *,
        directory, runner=None, audit=None) -> str:
    try:
        target = _resolve_screenshot(path, directory)
        result = ocr_windows.recognize(
            target, lang=lang, region=region, runner=runner)
    except (ocr_windows.OcrError, screen_capture.ScreenError) as exc:
        return f'Error: {exc}'
    _notify(audit, 'screen_ocr', path=str(target), chars=len(result.text),
            lang=result.language, region=list(result.region) if result.region else None,
            lines=len(result.lines), duration_ms=result.duration_ms)
    header = (f'OCR ({result.language}) {result.width}x{result.height}px, '
              f'{len(result.lines)} lines, {len(result.text)} chars, '
              f'{result.duration_ms}ms · {target}')
    if not result.text.strip() and not result.lines:
        return f'{header}\nNo text found.'
    text = result.text
    if len(text) > MAX_OCR_TEXT_CHARS:
        text = (text[:MAX_OCR_TEXT_CHARS]
                + f'\n... (truncated; {len(result.text)} chars total)')
    blocks = [header, text.strip() or '(no plain text)']
    if result.lines:
        blocks.append('Lines (bbox x,y,w,h):\n' + _format_lines(result.lines))
    return '\n\n'.join(blocks)
