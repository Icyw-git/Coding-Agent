"""本机空闲秒数与锁屏状态（Windows）。

用途：主动性任务只在用户空闲时跑；锁屏时暂停截屏类工具。空闲用
``GetLastInputInfo``；锁屏用 ``OpenInputDesktop`` 打不开输入桌面来判断（锁屏后
输入桌面切到 Winlogon 安全桌面，普通进程拿不到句柄）。这是启发式判断：远程桌面
断开、UAC 安全桌面等场景也会表现为「锁屏」，但对本项目的用途足够。

结果缓存 2 秒，避免网关轮询把 PowerShell 拉起成 CPU 热点。``HARNESS_IDLE=0``
可整体关闭（返回 ``available=false``）。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

ENV = 'HARNESS_IDLE'
PLATFORM = sys.platform
TTL = 2.0
TIMEOUT = 20.0

_OFF = {'0', 'false', 'no', 'off'}

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class HarnessIdleProbe {
    [StructLayout(LayoutKind.Sequential)]
    public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
    [DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    [DllImport("kernel32.dll")] public static extern uint GetTickCount();
    [DllImport("user32.dll", SetLastError = true)]
    public static extern IntPtr OpenInputDesktop(uint dwFlags, bool fInherit, uint dwDesiredAccess);
    [DllImport("user32.dll", SetLastError = true)] public static extern bool CloseDesktop(IntPtr hDesktop);
}
'@
$info = New-Object HarnessIdleProbe+LASTINPUTINFO
$info.cbSize = [System.Runtime.InteropServices.Marshal]::SizeOf($info)
$ok = [HarnessIdleProbe]::GetLastInputInfo([ref]$info)
$idle = $null
if ($ok) {
    $tick = [HarnessIdleProbe]::GetTickCount()
    $idle = [math]::Floor(($tick - $info.dwTime) / 1000)
}
$desktop = [HarnessIdleProbe]::OpenInputDesktop(0, $false, 0x0100)
$locked = ($desktop -eq [IntPtr]::Zero)
if ($desktop -ne [IntPtr]::Zero) { [HarnessIdleProbe]::CloseDesktop($desktop) | Out-Null }
[pscustomobject]@{ idle_seconds = $idle; locked = $locked } | ConvertTo-Json -Compress
"""

_lock = threading.Lock()
_cache: dict | None = None
_cached_at = 0.0


def _unavailable() -> str | None:
    raw = (os.getenv(ENV) or '').strip().casefold()
    if raw in _OFF:
        return 'disabled'
    if PLATFORM != 'win32':
        return 'unsupported'
    return None


def enabled() -> bool:
    return _unavailable() is None


def reset() -> None:
    """清空缓存（测试用）。"""
    global _cache, _cached_at
    with _lock:
        _cache = None
        _cached_at = 0.0


def _payload(*, available: bool, idle_seconds, locked, error: str | None = None) -> dict:
    data = {
        'available': available,
        'idle_seconds': idle_seconds,
        'locked': locked,
        'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')
        .replace('+00:00', 'Z'),
    }
    if error:
        data['error'] = error
    return data


def _run(command: list[str], timeout: float):
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    return subprocess.run(command, capture_output=True, encoding='utf-8',
                          errors='replace', timeout=timeout, check=False, **kwargs)


def _command() -> list[str]:
    encoded = base64.b64encode(_SCRIPT.encode('utf-16-le')).decode('ascii')
    return ['powershell.exe', '-NoProfile', '-NonInteractive',
            '-EncodedCommand', encoded]


def _error_text(text, fallback: str) -> str:
    cleaned = ' '.join(str(text or '').split())
    return (cleaned or fallback)[:200]


def _remember(data: dict) -> dict:
    global _cache, _cached_at
    _cache = data
    _cached_at = time.monotonic()
    return dict(data)


def _probe(runner) -> dict:
    try:
        completed = runner(_command(), TIMEOUT)
    except subprocess.TimeoutExpired:
        return _payload(available=False, idle_seconds=None, locked=None,
                        error='timeout')
    except OSError as exc:
        return _payload(available=False, idle_seconds=None, locked=None,
                        error=_error_text(f'{type(exc).__name__}: {exc}', 'failed'))

    code = getattr(completed, 'returncode', 1)
    if code != 0:
        error = _error_text(getattr(completed, 'stderr', ''), f'exit {code}')
        return _payload(available=False, idle_seconds=None, locked=None,
                        error=error)

    text = str(getattr(completed, 'stdout', '') or '').lstrip('\ufeff').strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return _payload(available=False, idle_seconds=None, locked=None,
                        error='invalid output')
    if not isinstance(data, dict):
        return _payload(available=False, idle_seconds=None, locked=None,
                        error='invalid output')

    locked = data.get('locked')
    if not isinstance(locked, bool):
        locked = None
    idle_seconds = data.get('idle_seconds')
    if isinstance(idle_seconds, bool) or not isinstance(idle_seconds, (int, float)):
        return _payload(available=False, idle_seconds=None, locked=locked,
                        error='idle_unavailable')
    return _payload(available=True, idle_seconds=max(0, int(idle_seconds)),
                    locked=locked)


def state(*, runner=None, force: bool = False) -> dict:
    """返回 ``{available, idle_seconds, locked, checked_at[, error]}``。"""
    reason = _unavailable()
    if reason is not None:
        return _payload(available=False, idle_seconds=None, locked=None,
                        error=reason)

    with _lock:
        if (not force and _cache is not None
                and time.monotonic() - _cached_at < TTL):
            return dict(_cache)
        return _remember(_probe(runner or _run))
