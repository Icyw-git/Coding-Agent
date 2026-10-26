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

_CLIPBOARD_SETUP = r'''
Add-Type -AssemblyName System.Windows.Forms
'''

_CLIPBOARD_BODY = r'''
function Invoke-HarnessOp($req) {
    if ($req.op -eq 'read') {
        $raw = Get-Clipboard -Raw
        if ($null -eq $raw) { $raw = '' }
        $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$raw)
        $total = $bytes.Length
        $max = [int]$req.max_bytes
        if ($max -gt 0 -and $total -gt $max) {
            $bytes = [byte[]]$bytes[0..($max - 1)]
        }
        return @{
            ok = $true
            total = $total
            data = [Convert]::ToBase64String($bytes)
        }
    }
    if ($req.op -eq 'write') {
        $bytes = [Convert]::FromBase64String([string]$req.data)
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        Set-Clipboard -Value $text
        return @{ ok = $true }
    }
    return @{ ok = $false; error = 'unknown clipboard op' }
}
'''

_CLIPBOARD_MAIN = r'''
# 独立进程模式入口（测试注入 runner 时走这里）。
try {
    $payload = Read-Payload $null
    $req = @{ op = [string]$payload.op; max_bytes = $payload.max_bytes; data = $payload.data }
    $result = Invoke-HarnessOp $req
    Write-Result $result 0
} catch {
    $msg = $_.Exception.Message
    if ($null -ne $script:HarnessFailure) { Write-Result $script:HarnessFailure 2 }
    else { Write-Result ([ordered]@{ ok = $false; error = 'clipboard_failed'; message = $msg }) 2 }
}
'''



# 独立进程回退路径：worker 不可用时仍要能读写（老实现，逐字保留）。
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


def worker_script() -> str:
    """常驻 worker 脚本：剪贴板读写复用同一个 PowerShell 进程。

    独立进程一次 387ms，全部是启动 + 程序集加载；worker 里不到 1ms。
    剪贴板读写是高频操作（问一句「把刚复制的发我」就一次），这条路径值得。
    """
    import ps_worker

    return (ps_worker.PRELUDE + _CLIPBOARD_SETUP
            + _CLIPBOARD_BODY + ps_worker.LOOP)


def _worker_enabled() -> bool:
    return (os.getenv('HARNESS_PS_WORKER') or '').strip().casefold() not in (
        '0', 'false', 'off', 'no')


def _read_via_worker(*, timeout: float):
    import ps_worker

    reply = ps_worker.run('clipboard', worker_script,
                          {'op': 'read', 'max_bytes': MAX_READ_BYTES},
                          timeout=max(timeout, ps_worker.DEFAULT_TIMEOUT))
    try:
        total = int(reply.get('total') or 0)
    except (TypeError, ValueError) as exc:
        raise ClipboardError('unexpected clipboard payload') from exc
    try:
        data = base64.b64decode(str(reply.get('data') or ''), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ClipboardError('clipboard returned invalid text data') from exc
    truncated = total > len(data)
    text = data.decode('utf-8', 'ignore' if truncated else 'replace')
    return text, total, truncated


def read(*, runner=None, timeout: float = DEFAULT_TIMEOUT) -> ClipboardRead:
    started = time.monotonic()
    if runner is None and _worker_enabled():
        try:
            text, total, truncated = _read_via_worker(timeout=timeout)
        except Exception as exc:
            print(f'  \033[33m[clipboard] worker unavailable, falling back: {exc}\033[0m')
        else:
            return ClipboardRead(text=text, total_bytes=total,
                                 truncated=truncated,
                                 duration_ms=int((time.monotonic() - started) * 1000))
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
    started = time.monotonic()
    if runner is None and _worker_enabled():
        try:
            import ps_worker

            ps_worker.run('clipboard', worker_script,
                          {'op': 'write',
                           'data': base64.b64encode(encoded).decode('ascii')},
                          timeout=max(timeout, ps_worker.DEFAULT_TIMEOUT))
        except Exception as exc:
            print(f'  \033[33m[clipboard] worker unavailable, falling back: {exc}\033[0m')
        else:
            return ClipboardWrite(chars=len(text), bytes=len(encoded),
                                  duration_ms=int((time.monotonic() - started) * 1000))
    payload = base64.b64encode(encoded)
    _run(WRITE_SCRIPT, payload, runner=runner, timeout=timeout)
    return ClipboardWrite(chars=len(text), bytes=len(encoded),
                          duration_ms=int((time.monotonic() - started) * 1000))
