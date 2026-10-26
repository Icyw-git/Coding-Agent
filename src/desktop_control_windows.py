"""Windows foreground-window and input control via PowerShell/user32.

This module is only the OS capability layer.  Policy, approval, scheduled-turn
and subagent restrictions live in hooks.py/exec_policy.py.  The runner is
injectable so tests never move the real mouse or send real keystrokes.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

DEFAULT_TIMEOUT = 10.0
MAX_TYPE_CHARS = 1000
MAX_SCROLL = 20
SUPPORTED_KEYS = frozenset({
    'enter', 'return', 'tab', 'escape', 'esc', 'backspace', 'delete',
    'home', 'end', 'left', 'up', 'right', 'down', 'space', 'pageup',
    'pagedown', 'insert',
})


class DesktopControlError(ValueError):
    """A desktop operation failed or was refused by the OS capability layer."""


@dataclass(frozen=True)
class WindowState:
    available: bool
    title: str = ''
    process: str = ''
    pid: int = 0
    window_id: str = ''
    class_name: str = ''
    rect: tuple[int, int, int, int] | None = None
    focused_name: str = ''
    focused_control_type: str = ''
    focused_is_password: bool = False
    sensitive_target: bool = False
    error: str = ''


@dataclass(frozen=True)
class ActionResult:
    action: str
    state: WindowState
    duration_ms: int


# The input payload is base64 in an environment variable.  This avoids quoting
# and console-codepage problems for Chinese text and key names.
CONTROL_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class HarnessDesktopNative {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder text, int count);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetClassName(IntPtr hWnd, System.Text.StringBuilder text, int count);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")]
    public static extern void mouse_event(
        uint flags, uint dx, uint dy, int data, UIntPtr extra);
    [DllImport("user32.dll")] public static extern uint SendInput(uint count, INPUT[] inputs, int size);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public INPUTUNION u; }
    [StructLayout(LayoutKind.Explicit)] public struct INPUTUNION {
        [FieldOffset(0)] public KEYBDINPUT keyboard;
    }
    [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT {
        public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public UIntPtr dwExtraInfo;
    }
    public const uint MOUSE_LEFT_DOWN=0x0002, MOUSE_LEFT_UP=0x0004;
    public const uint KEY_UP=0x0002, KEY_UNICODE=0x0004;
    public static void Unicode(ushort value, bool up) {
        var input = new INPUT[1]; input[0].type=1;
        input[0].u.keyboard.wScan=value; input[0].u.keyboard.dwFlags=KEY_UNICODE|(up?KEY_UP:0);
        if (SendInput(1,input,Marshal.SizeOf(typeof(INPUT))) != 1) throw new Exception("SendInput failed");
    }
    public static void VirtualKey(ushort value, bool up) {
        var input = new INPUT[1]; input[0].type=1;
        input[0].u.keyboard.wVk=value; input[0].u.keyboard.dwFlags=(up?KEY_UP:0);
        if (SendInput(1,input,Marshal.SizeOf(typeof(INPUT))) != 1) throw new Exception("SendInput failed");
    }
}
'@

function Read-Payload {
    $raw = [Convert]::FromBase64String($env:HARNESS_DESKTOP_PAYLOAD)
    return [Text.Encoding]::UTF8.GetString($raw) | ConvertFrom-Json
}
function Get-WindowState {
    $hwnd = [HarnessDesktopNative]::GetForegroundWindow()
    if ($hwnd -eq [IntPtr]::Zero) { return [ordered]@{ available=$false; error='no_foreground_window' } }
    $titleBuffer = New-Object Text.StringBuilder 1024
    $classBuffer = New-Object Text.StringBuilder 256
    [HarnessDesktopNative]::GetWindowText($hwnd, $titleBuffer, 1024) | Out-Null
    [HarnessDesktopNative]::GetClassName($hwnd, $classBuffer, 256) | Out-Null
    $pid = [uint32]0
    [HarnessDesktopNative]::GetWindowThreadProcessId($hwnd, [ref]$pid) | Out-Null
    $process = ''
    try { $process = (Get-Process -Id $pid -ErrorAction Stop).ProcessName } catch {}
    $rect = New-Object HarnessDesktopNative+RECT
    $hasRect = [HarnessDesktopNative]::GetWindowRect($hwnd, [ref]$rect)
    $focusedName = ''; $focusedType = ''; $password = $false
    try {
        $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
        if ($null -ne $focused) {
            $focusedName = [string]$focused.Current.Name
            $focusedType = [string]$focused.Current.ControlType.ProgrammaticName
            $password = [bool]$focused.Current.IsPassword
        }
    } catch {}
    $all = "$($titleBuffer.ToString()) $process $($classBuffer.ToString()) $focusedName"
    $sensitivePattern = '(?i)password|passwd|payment|checkout|bank|wallet|2fa|verification|credit.?card|'
    $sensitivePattern += '银行卡|支付|付款|密码|验证码'
    $sensitive = $password -or ($all -match $sensitivePattern)
    return [ordered]@{
        available=$true; title=$titleBuffer.ToString(); process=$process; pid=[int]$pid
        window_id=$hwnd.ToInt64().ToString('X')
        class_name=$classBuffer.ToString()
        rect=if ($hasRect) { @($rect.Left,$rect.Top,($rect.Right-$rect.Left),($rect.Bottom-$rect.Top)) } else { $null }
        focused_name=$focusedName; focused_control_type=$focusedType
        focused_is_password=$password; sensitive_target=$sensitive
    }
}
function Fail($code, $message, $state) {
    [ordered]@{ ok=$false; error=$code; message=$message; state=$state } | ConvertTo-Json -Depth 8 -Compress
    exit 2
}
try {
    $payload = Read-Payload
    $state = Get-WindowState
    if ($payload.action -eq 'snapshot') {
        [ordered]@{ ok=$true; state=$state } | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }
    if (-not $state.available) { Fail 'no_foreground_window' 'no foreground window' $state }
    if ([int]$state.pid -ne [int]$payload.expected_pid -or
        [string]$state.window_id -ne [string]$payload.expected_window_id) {
        Fail 'foreground_changed' 'foreground window changed since the snapshot; take a new desktop_snapshot' $state
    }
    if ($state.sensitive_target) {
        Fail 'sensitive_target' 'desktop input is blocked on a password or sensitive window' $state
    }
    $action = [string]$payload.action
    if ($action -eq 'move' -or $action -eq 'click') {
        $x=[int]$payload.x; $y=[int]$payload.y
        $rect=$state.rect
        if ($null -eq $rect -or $x -lt [int]$rect[0] -or $y -lt [int]$rect[1] -or
            $x -ge ([int]$rect[0]+[int]$rect[2]) -or
            $y -ge ([int]$rect[1]+[int]$rect[3])) {
            Fail 'out_of_window' 'coordinates must be inside the snapshotted foreground window' $state
        }
        $vs=[System.Windows.Forms.SystemInformation]::VirtualScreen
        if ($x -lt $vs.X -or $y -lt $vs.Y -or $x -ge ($vs.X+$vs.Width) -or $y -ge ($vs.Y+$vs.Height)) {
            Fail 'out_of_bounds' 'coordinates are outside the virtual screen' $state
        }
        if (-not [HarnessDesktopNative]::SetCursorPos($x,$y)) { Fail 'input_failed' 'SetCursorPos failed' $state }
        if ($action -eq 'click') {
            [HarnessDesktopNative]::mouse_event(0x0002,0,0,0,[UIntPtr]::Zero)
            [HarnessDesktopNative]::mouse_event(0x0004,0,0,0,[UIntPtr]::Zero)
        }
    } elseif ($action -eq 'scroll') {
        $amount=[int]$payload.amount
        if ([Math]::Abs($amount) -gt 20 -or $amount -eq 0) {
            Fail 'invalid_amount' 'scroll amount must be between -20 and 20' $state
        }
        [HarnessDesktopNative]::mouse_event(0x0800,0,0,$amount*120,[UIntPtr]::Zero)
    } elseif ($action -eq 'type') {
        $text=[string]$payload.text
        if ($text.Length -gt 1000) { Fail 'text_too_long' 'typed text exceeds 1000 characters' $state }
        foreach ($ch in $text.ToCharArray()) {
            [HarnessDesktopNative]::Unicode([uint16][char]$ch,$false)
            [HarnessDesktopNative]::Unicode([uint16][char]$ch,$true)
        }
    } elseif ($action -eq 'key') {
        $key=([string]$payload.key).ToLowerInvariant()
        $map=@{'enter'=13;'return'=13;'tab'=9;'escape'=27;'esc'=27;'backspace'=8;'delete'=46;'home'=36;'end'=35;'left'=37;'up'=38;'right'=39;'down'=40;'space'=32;'pageup'=33;'pagedown'=34;'insert'=45}
        $vk=0
        if ($key -match '^f([1-9]|1[0-2])$') { $vk=111+[int]$Matches[1] }
        elseif ($map.ContainsKey($key)) { $vk=$map[$key] }
        elseif ($key.Length -eq 1 -and [int][char]$key -lt 128) { $vk=[int][char]$key.ToUpperInvariant() }
        else { Fail 'invalid_key' 'unsupported key name' $state }
        [HarnessDesktopNative]::VirtualKey([uint16]$vk,$false)
        [HarnessDesktopNative]::VirtualKey([uint16]$vk,$true)
    } else { Fail 'invalid_action' 'unsupported desktop action' $state }
    Start-Sleep -Milliseconds 30
    $after=Get-WindowState
    [ordered]@{ ok=$true; action=$action; state_before=$state; state_after=$after } | ConvertTo-Json -Depth 8 -Compress
} catch {
    [ordered]@{ ok=$false; error='desktop_control_failed';
        message=$_.Exception.Message } | ConvertTo-Json -Depth 8 -Compress
    exit 2
}
'''


def _powershell_exe() -> str | None:
    override = (os.getenv('HARNESS_POWERSHELL') or '').strip()
    return override or shutil.which('powershell') or shutil.which('pwsh')


def run_powershell(script: str, env: dict, timeout: float):
    executable = _powershell_exe()
    if executable is None:
        raise DesktopControlError('desktop control requires Windows PowerShell')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    return subprocess.run(
        [executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-WindowStyle', 'Hidden', '-Command', script],
        capture_output=True, timeout=timeout, env={**os.environ, **env},
        creationflags=flags,
    )


def _state(value) -> WindowState:
    value = value if isinstance(value, dict) else {}
    rect = value.get('rect')
    try:
        rect = tuple(int(item) for item in rect) if rect is not None else None
    except (TypeError, ValueError):
        rect = None
    return WindowState(
        available=bool(value.get('available')),
        title=str(value.get('title') or ''), process=str(value.get('process') or ''),
        pid=int(value.get('pid') or 0),
        window_id=str(value.get('window_id') or ''),
        class_name=str(value.get('class_name') or ''),
        rect=rect, focused_name=str(value.get('focused_name') or ''),
        focused_control_type=str(value.get('focused_control_type') or ''),
        focused_is_password=bool(value.get('focused_is_password')),
        sensitive_target=bool(value.get('sensitive_target')),
        error=str(value.get('error') or ''),
    )


def _run(payload: dict, *, runner=None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    data = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode('ascii')
    runner = runner or run_powershell
    try:
        result = runner(CONTROL_SCRIPT, {'HARNESS_DESKTOP_PAYLOAD': data}, timeout)
    except subprocess.TimeoutExpired as exc:
        raise DesktopControlError(f'desktop control timed out after {timeout:g}s') from exc
    except DesktopControlError:
        raise
    except OSError as exc:
        raise DesktopControlError(f'desktop control unavailable: {exc}') from exc
    stdout = getattr(result, 'stdout', b'') or b''
    if isinstance(stdout, bytes):
        stdout = stdout.decode('utf-8', 'replace')
    stderr = getattr(result, 'stderr', b'') or b''
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', 'replace')
    try:
        record = json.loads(str(stdout).strip())
    except (TypeError, ValueError) as exc:
        raise DesktopControlError(f'unexpected desktop control output: {str(stderr).strip()[-300:]}') from exc
    if getattr(result, 'returncode', 0) or not record.get('ok'):
        raise DesktopControlError(str(record.get('message') or record.get('error') or 'desktop control failed'))
    return record


def snapshot(*, runner=None, timeout: float = DEFAULT_TIMEOUT) -> WindowState:
    return _state(_run({'action': 'snapshot'}, runner=runner, timeout=timeout).get('state'))


def validate_action(action: str, *, expected_pid=None, expected_window_id=None,
                    x=None, y=None, text=None, key=None, amount=None) -> dict:
    action = str(action or '').strip().casefold()
    try:
        expected_pid = int(expected_pid)
    except (TypeError, ValueError) as exc:
        raise DesktopControlError('expected_pid is required; get it from desktop_snapshot') from exc
    if expected_pid <= 0:
        raise DesktopControlError('expected_pid must be a positive process id')
    if not isinstance(expected_window_id, str) or not expected_window_id.strip():
        raise DesktopControlError('expected_window_id is required; get it from desktop_snapshot')
    expected_window_id = expected_window_id.strip().upper()
    if len(expected_window_id) > 32 or any(ch not in '0123456789ABCDEF' for ch in expected_window_id):
        raise DesktopControlError('expected_window_id must be the hexadecimal id from desktop_snapshot')
    if action not in {'move', 'click', 'type', 'key', 'scroll'}:
        raise DesktopControlError('action must be move, click, type, key, or scroll')
    if action in {'move', 'click'}:
        try:
            coords = {'x': int(x), 'y': int(y)}
        except (TypeError, ValueError) as exc:
            raise DesktopControlError(f'{action} requires integer x and y') from exc
        return {'action': action, 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id, **coords}
    if action == 'type':
        if not isinstance(text, str) or len(text) > MAX_TYPE_CHARS:
            raise DesktopControlError(f'type text must be a string of at most {MAX_TYPE_CHARS} characters')
        return {'action': action, 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id, 'text': text}
    if action == 'key':
        if not isinstance(key, str) or not key.strip() or len(key) > 30:
            raise DesktopControlError('key requires a short key name')
        normalized = key.strip().casefold()
        if normalized not in SUPPORTED_KEYS and not (
                len(normalized) == 1 and normalized.isascii() and normalized.isalnum()) \
                and not (normalized.startswith('f') and normalized[1:].isdigit()
                         and 1 <= int(normalized[1:]) <= 12):
            raise DesktopControlError('unsupported key name')
        return {'action': action, 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id, 'key': normalized}
    try:
        amount = int(amount)
    except (TypeError, ValueError) as exc:
        raise DesktopControlError('scroll requires a non-zero integer amount') from exc
    if amount == 0 or abs(amount) > MAX_SCROLL:
        raise DesktopControlError(f'scroll amount must be between -{MAX_SCROLL} and {MAX_SCROLL}, excluding 0')
    return {'action': action, 'expected_pid': expected_pid,
            'expected_window_id': expected_window_id, 'amount': amount}


def action(action: str, *, expected_pid=None, expected_window_id=None, x=None,
           y=None, text=None, key=None, amount=None, runner=None,
           timeout: float = DEFAULT_TIMEOUT) -> ActionResult:
    payload = validate_action(action, expected_pid=expected_pid,
                              expected_window_id=expected_window_id, x=x, y=y,
                              text=text, key=key, amount=amount)
    started = time.monotonic()
    record = _run(payload, runner=runner, timeout=timeout)
    return ActionResult(
        action=payload['action'], state=_state(record.get('state_after')),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def state_dict(state: WindowState) -> dict:
    return {
        'available': state.available, 'title': state.title, 'process': state.process,
        'pid': state.pid, 'window_id': state.window_id,
        'class_name': state.class_name,
        'rect': list(state.rect) if state.rect is not None else None,
        'focused_name': state.focused_name,
        'focused_control_type': state.focused_control_type,
        'focused_is_password': state.focused_is_password,
        'sensitive_target': state.sensitive_target,
        **({'error': state.error} if state.error else {}),
    }
