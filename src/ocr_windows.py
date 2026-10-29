"""Windows 本地 OCR：PowerShell + WinRT `Windows.Media.Ocr`，无云端调用。

输入一张本地图片（通常来自 screen_capture），输出每行文本与像素 bbox。
缺语言包时返回可执行的安装提示，而不是让工具抛栈。
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import screen_capture

DEFAULT_LANGUAGE = 'zh-Hans-CN'
DEFAULT_TIMEOUT = 60.0
MAX_OCR_BYTES = 32 * 1024 * 1024
INSTALL_HINT = (
    "install it from Settings > Time & language > Language & region, "
    "or run: Get-WindowsCapability -Online -Name 'Language.OCR*' | Select Name,State"
)


class OcrError(ValueError):
    """OCR 失败时给模型看的错误；不抛到工具边界外。"""


@dataclass(frozen=True)
class OcrResult:
    text: str
    lines: list[dict]
    language: str
    width: int
    height: int
    region: tuple[int, int, int, int] | None
    duration_ms: int


OCR_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Storage.FileAccessMode,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapTransform,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapBounds,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]

$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1)

function Await($operation, $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    $task.Wait(-1) | Out-Null
    $task.Result
}

function Write-Meta($obj, $code) {
    $json = $obj | ConvertTo-Json -Depth 10 -Compress
    [System.IO.File]::WriteAllText(
        $env:HARNESS_OCR_META, $json,
        (New-Object System.Text.UTF8Encoding($false)))
    if ($code -ne 0) { exit $code }
}

try {
    $langTag = $env:HARNESS_OCR_LANG
    if ($langTag -eq 'auto') {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    } else {
        $language = [Windows.Globalization.Language]::new($langTag)
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    }
    if ($null -eq $engine) {
        $available = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages |
            ForEach-Object { $_.LanguageTag })
        Write-Meta ([ordered]@{
            ok = $false
            error = 'ocr_language_unavailable'
            language = $langTag
            available = $available
        }) 1
    }
    $open = [Windows.Storage.StorageFile]::GetFileFromPathAsync($env:HARNESS_OCR_IN)
    $file = Await ($open) ([Windows.Storage.StorageFile])
    $read = $file.OpenAsync([Windows.Storage.FileAccessMode]::Read)
    $stream = Await ($read) ([Windows.Storage.Streams.IRandomAccessStream])
    $create = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
    $decoder = Await ($create) ([Windows.Graphics.Imaging.BitmapDecoder])
    $width = [int]$decoder.PixelWidth
    $height = [int]$decoder.PixelHeight
    $region = $env:HARNESS_OCR_REGION
    $offsetX = 0; $offsetY = 0
    if ($region) {
        $p = $region.Split(',')
        $offsetX = [int]$p[0]; $offsetY = [int]$p[1]
        $rw = [int]$p[2]; $rh = [int]$p[3]
        if ($offsetX + $rw -gt $width -or $offsetY + $rh -gt $height) {
            throw "识别区域 ${rw}x${rh}+${offsetX}+${offsetY} 超出图片 ${width}x${height}"
        }
        $transform = [Windows.Graphics.Imaging.BitmapTransform]::new()
        $bounds = [Windows.Graphics.Imaging.BitmapBounds]::new()
        $bounds.X = [uint32]$offsetX
        $bounds.Y = [uint32]$offsetY
        $bounds.Width = [uint32]$rw
        $bounds.Height = [uint32]$rh
        $transform.Bounds = $bounds
        $transform.ScaledWidth = [uint32]$rw
        $transform.ScaledHeight = [uint32]$rh
        $crop = $decoder.GetSoftwareBitmapAsync(
            [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
            [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied,
            $transform,
            [Windows.Graphics.Imaging.ExifOrientationMode]::IgnoreExifOrientation,
            [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage)
        $bitmap = Await ($crop) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $ocrWidth = $rw; $ocrHeight = $rh
    } else {
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $ocrWidth = $width; $ocrHeight = $height
    }
    $limit = [int][Windows.Media.Ocr.OcrEngine]::MaxImageDimension
    if ([Math]::Max($ocrWidth, $ocrHeight) -gt $limit) {
        throw "图片 ${ocrWidth}x${ocrHeight} 超过识别上限 ${limit}；请传更小的 region"
    }
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = New-Object System.Collections.ArrayList
    foreach ($line in $result.Lines) {
        $words = New-Object System.Collections.ArrayList
        $minX = [double]::MaxValue; $minY = [double]::MaxValue
        $maxX = 0.0; $maxY = 0.0
        foreach ($word in $line.Words) {
            $r = $word.BoundingRect
            $wx = [double]$r.X; $wy = [double]$r.Y
            $ww = [double]$r.Width; $wh = [double]$r.Height
            if ($wx -lt $minX) { $minX = $wx }
            if ($wy -lt $minY) { $minY = $wy }
            if (($wx + $ww) -gt $maxX) { $maxX = $wx + $ww }
            if (($wy + $wh) -gt $maxY) { $maxY = $wy + $wh }
            $words.Add([ordered]@{
                text = [string]$word.Text
                bbox = @(
                    [int][Math]::Round($wx + $offsetX),
                    [int][Math]::Round($wy + $offsetY),
                    [int][Math]::Round($ww),
                    [int][Math]::Round($wh))
            }) | Out-Null
        }
        if ($words.Count -gt 0) {
            $lineBox = @(
                [int][Math]::Round($minX + $offsetX),
                [int][Math]::Round($minY + $offsetY),
                [int][Math]::Round($maxX - $minX),
                [int][Math]::Round($maxY - $minY))
        } else {
            $lineBox = @(0, 0, 0, 0)
        }
        $lines.Add([ordered]@{
            text = [string]$line.Text
            bbox = $lineBox
            words = @($words.ToArray())
        }) | Out-Null
    }
    Write-Meta ([ordered]@{
        ok = $true
        language = $engine.RecognizerLanguage.LanguageTag
        width = $width
        height = $height
        region = $region
        text = [string]$result.Text
        lines = @($lines.ToArray())
    }) 0
} catch {
    Write-Meta ([ordered]@{
        ok = $false
        error = 'ocr_failed'
        message = $_.Exception.Message
    }) 1
}
'''


def _normalize_bbox(value) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        return [int(part) for part in value]
    except (TypeError, ValueError):
        return None


def _normalize_line(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    bbox = _normalize_bbox(item.get('bbox')) or [0, 0, 0, 0]
    words = []
    for word in item.get('words') or []:
        if not isinstance(word, dict):
            continue
        words.append({
            'text': str(word.get('text') or ''),
            'bbox': _normalize_bbox(word.get('bbox')) or [0, 0, 0, 0],
        })
    return {'text': str(item.get('text') or ''), 'bbox': bbox, 'words': words}


def recognize(path, *, lang: str = DEFAULT_LANGUAGE, region=None, runner=None,
              timeout: float = DEFAULT_TIMEOUT) -> OcrResult:
    language = str(lang or DEFAULT_LANGUAGE).strip()
    parsed_region = screen_capture.parse_region(region)
    source = Path(path)
    if not source.is_file():
        raise OcrError(f'找不到这张图片：{source}')
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise OcrError(f'读不了这张图片：{exc}') from exc
    if size > MAX_OCR_BYTES:
        raise OcrError(f'图片太大，无法识别（{size} 字节）')
    runner = runner or screen_capture.run_powershell
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='harness-ocr-') as tmp:
        meta_path = Path(tmp) / 'ocr.json'
        env = {
            'HARNESS_OCR_IN': str(source),
            'HARNESS_OCR_META': str(meta_path),
            'HARNESS_OCR_LANG': language,
            'HARNESS_OCR_REGION':
                ','.join(str(part) for part in parsed_region) if parsed_region else '',
        }
        try:
            result = runner(OCR_SCRIPT, env, float(timeout))
        except subprocess.TimeoutExpired as exc:
            raise OcrError(f'文字识别超时（{timeout} 秒）') from exc
        except screen_capture.ScreenError:
            raise
        except OSError as exc:
            raise OcrError(f'启动 PowerShell 失败：{exc}') from exc
        if not meta_path.is_file():
            raise OcrError(
                f'文字识别没有输出：{screen_capture.failure_text(result)}')
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            raise OcrError(f'识别元数据不合法：{exc}') from exc
        if not isinstance(meta, dict) or not meta.get('ok'):
            error = meta.get('error') if isinstance(meta, dict) else None
            if error == 'ocr_language_unavailable':
                available = meta.get('available') or []
                listed = ', '.join(str(tag) for tag in available) or '(none)'
                raise OcrError(
                    f"OCR language '{language}' is not installed; available: {listed}; "
                    f'{INSTALL_HINT}')
            message = (meta.get('message') if isinstance(meta, dict) else None) \
                or error or 'OCR failed'
            raise OcrError(str(message))
        try:
            width = int(meta['width'])
            height = int(meta['height'])
        except (KeyError, TypeError, ValueError) as exc:
            raise OcrError('识别元数据缺少宽高') from exc
        if parsed_region is not None:
            x, y, region_width, region_height = parsed_region
            if x + region_width > width or y + region_height > height:
                raise OcrError(
                    f'识别区域 {region_width}x{region_height}+{x}+{y} 超出'
                    f'图片 {width}x{height}')
        lines = [line for line in
                 (_normalize_line(item) for item in meta.get('lines') or [])
                 if line is not None]
        text = meta.get('text')
        if not isinstance(text, str):
            text = '\n'.join(line['text'] for line in lines)
        return OcrResult(
            text=text, lines=lines,
            language=str(meta.get('language') or language),
            width=width, height=height, region=parsed_region,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
