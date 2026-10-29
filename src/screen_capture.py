"""本机截屏：PowerShell + System.Drawing，返回 PNG 字节与屏幕布局。

只在 Windows 上有意义；PowerShell 进程通过参数注入（runner），单测不碰真实屏幕。
截图永不直接进模型上下文，由调用方落盘后走 view_image 的多模态链路。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 30.0
MAX_DELAY_MS = 5000
MAX_CAPTURE_PIXELS = 40_000_000
MAX_PNG_BYTES = 24 * 1024 * 1024
RETENTION_DAYS = 7
SCREEN_MODES = ('primary', 'all')
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


class ScreenError(ValueError):
    """截屏失败时给模型看的错误；不抛到工具边界外。"""


@dataclass(frozen=True)
class Capture:
    png: bytes
    width: int
    height: int
    screen: str
    region: tuple[int, int, int, int] | None
    screens: tuple[dict, ...]
    duration_ms: int
    #: 截图像素 (0,0) 对应的**绝对屏幕坐标**。OCR 的 bbox 是图像像素，
    #: 而 desktop_action 要的是屏幕坐标——没有这个原点，两边永远对不上。
    origin: tuple[int, int] = (0, 0)


CAPTURE_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class HarnessDpi {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
'@
[HarnessDpi]::SetProcessDPIAware() | Out-Null
function Write-Meta($obj, $code) {
    $json = $obj | ConvertTo-Json -Depth 6 -Compress
    [System.IO.File]::WriteAllText(
        $env:HARNESS_SCREEN_META, $json,
        (New-Object System.Text.UTF8Encoding($false)))
    if ($code -ne 0) { exit $code }
}
try {
    $delay = [int]$env:HARNESS_SCREEN_DELAY_MS
    if ($delay -gt 0) { Start-Sleep -Milliseconds $delay }
    $mode = $env:HARNESS_SCREEN_MODE
    $screens = New-Object System.Collections.ArrayList
    foreach ($s in [System.Windows.Forms.Screen]::AllScreens) {
        $screens.Add([ordered]@{
            name = $s.DeviceName
            primary = [bool]$s.Primary
            bounds = @($s.Bounds.X, $s.Bounds.Y, $s.Bounds.Width, $s.Bounds.Height)
        }) | Out-Null
    }
    if ($mode -eq 'primary') {
        $base = $screens | Where-Object { $_.primary } | Select-Object -First 1
        if ($null -eq $base) { $base = $screens | Select-Object -First 1 }
        $bx = [int]$base.bounds[0]; $by = [int]$base.bounds[1]
        $bw = [int]$base.bounds[2]; $bh = [int]$base.bounds[3]
    } else {
        $vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
        $bx = $vs.X; $by = $vs.Y; $bw = $vs.Width; $bh = $vs.Height
    }
    $region = $env:HARNESS_SCREEN_REGION
    if ($region) {
        $p = $region.Split(',')
        $x = $bx + [int]$p[0]; $y = $by + [int]$p[1]
        $w = [int]$p[2]; $h = [int]$p[3]
    } else {
        $x = $bx; $y = $by; $w = $bw; $h = $bh
    }
    if ($w -le 0 -or $h -le 0) { throw "invalid capture size ${w}x${h}" }
    $bitmap = New-Object System.Drawing.Bitmap($w, $h)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen($x, $y, 0, 0, (New-Object System.Drawing.Size($w, $h)))
        $bitmap.Save($env:HARNESS_SCREEN_OUT, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        $graphics.Dispose(); $bitmap.Dispose()
    }
    Write-Meta ([ordered]@{
        ok = $true
        width = $w; height = $h
        screen = $mode
        rect = @($x, $y, $w, $h)
        region = $region
        screens = @($screens.ToArray())
    }) 0
} catch {
    Write-Meta ([ordered]@{ ok = $false; error = 'capture_failed'; message = $_.Exception.Message }) 1
}
'''


def parse_region(region) -> tuple[int, int, int, int] | None:
    """解析 `x,y,w,h`（相对所选屏幕左上角）；空值返回 None。"""
    if region is None or (isinstance(region, str) and not region.strip()):
        return None
    if isinstance(region, str):
        parts = [part.strip() for part in region.split(',')]
    elif isinstance(region, (list, tuple)):
        parts = list(region)
    else:
        raise ScreenError('region 必须是 "x,y,w,h"')
    if len(parts) != 4:
        raise ScreenError('region 需要 4 个整数：x,y,w,h')
    try:
        values = tuple(int(part) for part in parts)
    except (TypeError, ValueError) as exc:
        raise ScreenError('region 需要 4 个整数：x,y,w,h') from exc
    x, y, width, height = values
    if min(x, y) < 0:
        raise ScreenError('region 的 x/y 不能是负数')
    if width <= 0 or height <= 0:
        raise ScreenError('region 的宽高必须为正')
    if width * height > MAX_CAPTURE_PIXELS:
        raise ScreenError(f'region 太大（{width}x{height}）')
    return values


def _delay_ms(value) -> int:
    try:
        delay = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ScreenError('delay_ms 必须是 0 到 5000 之间的整数') from exc
    if not 0 <= delay <= MAX_DELAY_MS:
        raise ScreenError(f'delay_ms 必须在 0 到 {MAX_DELAY_MS} 之间')
    return delay


def _powershell_exe() -> str | None:
    override = (os.getenv('HARNESS_POWERSHELL') or '').strip()
    if override:
        return override
    return shutil.which('powershell') or shutil.which('pwsh')


def run_powershell(script: str, env: dict, timeout: float):
    """默认 runner：Windows PowerShell 5.1（WinRT 在 7.x 上还要额外 SDK）。"""
    executable = _powershell_exe()
    if executable is None:
        raise ScreenError('截屏需要 Windows PowerShell')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    return subprocess.run(
        [executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-WindowStyle', 'Hidden', '-Command', script],
        capture_output=True, timeout=timeout, env={**os.environ, **env},
        creationflags=flags,
    )


def failure_text(result) -> str:
    """PowerShell 进程的失败摘要（控制台是 GBK，优先 UTF-8 再替换）。"""
    stderr = result.stderr or b''
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', 'replace')
    stderr = str(stderr).strip()
    return stderr[-600:] if stderr else f'exit code {result.returncode}'


def capture(*, screen: str = 'primary', region=None, delay_ms=0,
            runner=None, timeout: float = DEFAULT_TIMEOUT) -> Capture:
    mode = str(screen or 'primary').strip().lower()
    if mode not in SCREEN_MODES:
        raise ScreenError("screen must be 'primary' or 'all'")
    parsed_region = parse_region(region)
    delay = _delay_ms(delay_ms)
    runner = runner or run_powershell
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='harness-screen-') as tmp:
        out_path = Path(tmp) / 'capture.png'
        meta_path = Path(tmp) / 'meta.json'
        env = {
            'HARNESS_SCREEN_OUT': str(out_path),
            'HARNESS_SCREEN_META': str(meta_path),
            'HARNESS_SCREEN_MODE': mode,
            'HARNESS_SCREEN_DELAY_MS': str(delay),
            'HARNESS_SCREEN_REGION':
                ','.join(str(part) for part in parsed_region) if parsed_region else '',
        }
        try:
            result = runner(CAPTURE_SCRIPT, env, float(timeout))
        except subprocess.TimeoutExpired as exc:
            raise ScreenError(f'截屏超时（{timeout} 秒）') from exc
        except ScreenError:
            raise
        except OSError as exc:
            raise ScreenError(f'启动 PowerShell 失败：{exc}') from exc
        meta = None
        if meta_path.is_file():
            import json
            try:
                meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError) as exc:
                raise ScreenError(f'屏幕元数据不合法：{exc}') from exc
        if meta is not None and not meta.get('ok'):
            raise ScreenError(str(meta.get('message') or meta.get('error') or 'capture failed'))
        if result.returncode != 0:
            raise ScreenError(f'截屏失败：{failure_text(result)}')
        if not out_path.is_file():
            raise ScreenError('截屏没有产出图片')
        data = out_path.read_bytes()
        if not data.startswith(PNG_MAGIC):
            raise ScreenError('截屏产出的不是 PNG')
        if len(data) > MAX_PNG_BYTES:
            raise ScreenError(f'截图太大（{len(data)} 字节）')
        meta = meta or {}
        try:
            width = int(meta['width'])
            height = int(meta['height'])
        except (KeyError, TypeError, ValueError) as exc:
            raise ScreenError('屏幕元数据缺少宽高') from exc
        origin = (0, 0)
        rect = meta.get('rect')
        if isinstance(rect, (list, tuple)) and len(rect) >= 2:
            try:
                origin = (int(rect[0]), int(rect[1]))
            except (TypeError, ValueError):
                origin = (0, 0)
        screens = tuple(
            item for item in (meta.get('screens') or []) if isinstance(item, dict))
        return Capture(
            png=data, width=width, height=height, screen=mode,
            region=parsed_region, screens=screens,
            duration_ms=round((time.perf_counter() - started) * 1000),
            origin=origin,
        )


def save_png(data: bytes, directory, *, keep_ms: int | None = None,
             meta: dict | None = None) -> Path:
    """按内容哈希落盘；`keep_ms` 命中未过期文件时直接复用。

    ``meta`` 写成同名 `.json` 边车文件。截图之后才 OCR 的时候，只有边车能把
    「图像像素」换算成「屏幕坐标」；这个原点信息不留下来，后面就只能靠猜。
    """
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(data).hexdigest()
    target = target_dir / f'{digest}.png'
    sidecar = target_dir / f'{digest}.json'
    reuse = target.exists()
    if reuse and keep_ms is not None:
        age = (time.time() - target.stat().st_mtime) * 1000
        reuse = age <= keep_ms
    if not reuse:
        target.write_bytes(data)
    if meta is not None:
        import json

        payload = {**meta, 'sha1': digest, 'captured_at': time.time()}
        try:
            sidecar.write_text(json.dumps(payload, ensure_ascii=False),
                               encoding='utf-8')
        except OSError:
            # 边车写失败不该让一次成功的截图变成失败：只是少一个换算信息。
            pass
    return target


def load_meta(png_path) -> dict | None:
    """读截图的边车元数据；老截图没有边车就返回 None。"""
    import json

    path = Path(png_path)
    try:
        return json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def latest_png(directory) -> Path | None:
    target_dir = Path(directory)
    if not target_dir.is_dir():
        return None
    files = [path for path in target_dir.glob('*.png') if path.is_file()]
    return max(files, key=lambda path: path.stat().st_mtime, default=None)


def stats(directory) -> dict:
    target_dir = Path(directory)
    if not target_dir.is_dir():
        return {'files': 0, 'bytes': 0, 'oldest': None, 'newest': None}
    files = []
    for path in target_dir.glob('*.png'):
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((stat.st_mtime, stat.st_size))
    if not files:
        return {'files': 0, 'bytes': 0, 'oldest': None, 'newest': None}
    return {
        'files': len(files),
        'bytes': sum(size for _, size in files),
        'oldest': min(mtime for mtime, _ in files),
        'newest': max(mtime for mtime, _ in files),
    }


def prune(directory, *, days: int = RETENTION_DAYS, now: float | None = None) -> int:
    """删除超过保留期的截图；返回删除数量。"""
    target_dir = Path(directory)
    if not target_dir.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - days * 86400
    removed = 0
    for path in target_dir.glob('*.png'):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                path.with_suffix('.json').unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed
