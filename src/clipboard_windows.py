"""本机剪贴板：PowerShell Get-Clipboard / Set-Clipboard，runner 可注入。

文本走 base64 在两段传输（stdin 写入、stdout 读出），避开 Windows 控制台
默认 GBK 编码；读侧在 PowerShell 内按字节截断，防止一次巨大复制把结果撑爆。
这里只负责能力层，不判断该不该读/写——审批在 hooks/exec_policy。
"""
from __future__ import annotations

import base64
import binascii
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

DEFAULT_TIMEOUT = 10.0
MAX_READ_BYTES = 200 * 1024

READ_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
try {
    $raw = Get-Clipboard -Raw
    if ($null -eq $raw) { $raw = '' }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$raw)
    $total = $bytes.Length
    $max = [int]$env:HARNESS_CLIPBOARD_MAX_BYTES
    if ($max -gt 0 -and $total -gt $max) {
        $bytes = [byte[]]$bytes[0..($max - 1)]
    }
    [Console]::Out.Write("$total`n" + [Convert]::ToBase64String($bytes))
} catch {
    [Console]::Error.Write($_.Exception.Message)
    exit 2
}
'''

WRITE_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
try {
    $b64 = [Console]::In.ReadToEnd()
    $bytes = [Convert]::FromBase64String($b64.Trim())
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    Set-Clipboard -Value $text
} catch {
    [Console]::Error.Write($_.Exception.Message)
    exit 2
}
'''


class ClipboardError(ValueError):
    """剪贴板失败时给模型看的错误；不抛到工具边界外。"""


@dataclass(frozen=True)
class ClipboardRead:
    text: str
    total_bytes: int
    truncated: bool
    duration_ms: int


@dataclass(frozen=True)
class ClipboardWrite:
    chars: int
    bytes: int
    duration_ms: int


def _powershell_exe() -> str | None:
    override = (os.getenv('HARNESS_POWERSHELL') or '').strip()
    if override:
        return override
    return shutil.which('powershell') or shutil.which('pwsh')


def run_powershell(script: str, payload: bytes, *, timeout: float, env: dict):
    executable = _powershell_exe()
    if executable is None:
        raise ClipboardError('clipboard access requires Windows PowerShell')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    return subprocess.run(
        [executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-WindowStyle', 'Hidden', '-Command', script],
        input=payload, capture_output=True, timeout=timeout,
        env={**os.environ, **env}, creationflags=flags,
    )


def _failure_text(result) -> str:
    stderr = result.stderr or b''
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', 'replace')
    stderr = str(stderr).strip()
    return stderr[-600:] if stderr else f'exit code {getattr(result, "returncode", "?")}'


def _run(script: str, payload: bytes, *, runner, timeout: float, env=None):
    runner = runner or run_powershell
    try:
        result = runner(script, payload, timeout=timeout, env=env or {})
    except subprocess.TimeoutExpired as exc:
        raise ClipboardError(
            f'clipboard operation timed out after {timeout:g}s') from exc
    except ClipboardError:
        raise
    except OSError as exc:
        raise ClipboardError(f'clipboard PowerShell unavailable: {exc}') from exc
    if getattr(result, 'returncode', 0):
        raise ClipboardError(f'clipboard PowerShell failed: {_failure_text(result)}')
    return result


def _stdout_text(result) -> str:
    data = getattr(result, 'stdout', b'') or b''
    if isinstance(data, bytes):
        return data.decode('ascii', 'replace')
    return str(data)


def read(*, runner=None, timeout: float = DEFAULT_TIMEOUT) -> ClipboardRead:
    started = time.monotonic()
    result = _run(READ_SCRIPT, b'', runner=runner, timeout=timeout,
                  env={'HARNESS_CLIPBOARD_MAX_BYTES': str(MAX_READ_BYTES)})
    head, _, encoded = _stdout_text(result).partition('\n')
    try:
        total = int(head.strip())
    except ValueError as exc:
        raise ClipboardError('unexpected clipboard output from PowerShell') from exc
    try:
        data = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ClipboardError('clipboard returned invalid text data') from exc
    truncated = total > len(data)
    text = data.decode('utf-8', 'ignore' if truncated else 'replace')
    return ClipboardRead(text=text, total_bytes=total, truncated=truncated,
                         duration_ms=int((time.monotonic() - started) * 1000))


def write(text: str, *, runner=None, timeout: float = DEFAULT_TIMEOUT) -> ClipboardWrite:
    if not isinstance(text, str):
        raise ClipboardError('clipboard text must be a string')
    encoded = text.encode('utf-8')
    payload = base64.b64encode(encoded)
    started = time.monotonic()
    _run(WRITE_SCRIPT, payload, runner=runner, timeout=timeout)
    return ClipboardWrite(chars=len(text), bytes=len(encoded),
                          duration_ms=int((time.monotonic() - started) * 1000))
