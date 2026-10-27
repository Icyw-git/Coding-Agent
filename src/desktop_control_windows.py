"""Windows foreground-window and input control via PowerShell/user32.

This module is only the OS capability layer.  Policy, approval, scheduled-turn
and subagent restrictions live in hooks.py/exec_policy.py.  The runner is
injectable so tests never move the real mouse or send real keystrokes.

The PowerShell side answers with a single base64(UTF-8 JSON) blob on stdout:
console codepages (GBK on zh-CN hosts) must never touch the payload.
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
MAX_WINDOWS = 100
MAX_SEQUENCE_STEPS = 12
SUPPORTED_KEYS = frozenset({
    'enter', 'return', 'tab', 'escape', 'esc', 'backspace', 'delete',
    'home', 'end', 'left', 'up', 'right', 'down', 'space', 'pageup',
    'pagedown', 'insert',
})
SINGLE_ACTIONS = frozenset({'move', 'click', 'type', 'key', 'scroll'})
STEP_FIELDS = {
    'move': frozenset({'action', 'x', 'y'}),
    'click': frozenset({'action', 'x', 'y', 'button', 'count'}),
    'type': frozenset({'action', 'text'}),
    'key': frozenset({'action', 'key'}),
    'scroll': frozenset({'action', 'amount'}),
    'wait': frozenset({'action', 'ms'}),
}


KEY_MODIFIERS = frozenset({'ctrl', 'alt', 'shift', 'win'})
CLICK_BUTTONS = frozenset({'left', 'right', 'middle'})
MIN_WAIT_MS = 50
MAX_WAIT_MS = 3000


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
    steps: tuple[dict, ...] = ()


# The input payload is base64 in an environment variable.  This avoids quoting
# and console-codepage problems for Chinese text and key names.
# worker 模式下只执行一次的装配部分（Add-Type 编译在这里，代价只付一次）。
_CONTROL_SETUP = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class HarnessDesktopNative {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(
        uint idAttach, uint idAttachTo, bool attach);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
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
# Physical-pixel coordinates, matching screen_capture.py; without this a
# scaled monitor would give the model one coordinate space and screenshots another.
[HarnessDesktopNative]::SetProcessDPIAware() | Out-Null

# All output leaves as one base64(UTF-8 JSON) line: redirected PowerShell 5.1
# stdout would otherwise use the OEM codepage (GBK on zh-CN) and titles would
# arrive mangled.
function Write-Result($obj, $code) {
    # worker 模式下 Write-Reply 已由 prelude 定义，直接走 JSON 行协议；
    # 独立进程模式没有预先定义 Write-Reply，退回 base64(UTF-8 JSON) 单行输出。
    if (Get-Command Write-Reply -ErrorAction SilentlyContinue) {
        Write-Reply $obj
        return
    }
    $json = $obj | ConvertTo-Json -Depth 8 -Compress
    [Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
    [Console]::Out.Flush()
}
function Read-Payload($req) {
    # worker 模式下参数已经是解析好的对象；独立进程模式仍支持 base64 环境变量。
    if ($null -ne $req) { return $req }
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
    $procId = [uint32]0
    [HarnessDesktopNative]::GetWindowThreadProcessId($hwnd, [ref]$procId) | Out-Null
    $process = ''
    try { $process = (Get-Process -Id $procId -ErrorAction Stop).ProcessName } catch {}
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
        available=$true; title=$titleBuffer.ToString(); process=$process; pid=[int]$procId
        window_id=$hwnd.ToInt64().ToString('X')
        class_name=$classBuffer.ToString()
        rect=if ($hasRect) { @($rect.Left,$rect.Top,($rect.Right-$rect.Left),($rect.Bottom-$rect.Top)) } else { $null }
        focused_name=$focusedName; focused_control_type=$focusedType
        focused_is_password=$password; sensitive_target=$sensitive
    }
}
function Invoke-Step($step, $state) {
    # Runs one action against the given fresh window state.  Returns $null on
    # success or an ordered record {code,message} describing the refusal.
    $action = [string]$step.action
    if ($action -eq 'move' -or $action -eq 'click') {
        $x=[int]$step.x; $y=[int]$step.y
        $rect=$state.rect
        if ($null -eq $rect -or $x -lt [int]$rect[0] -or $y -lt [int]$rect[1] -or
            $x -ge ([int]$rect[0]+[int]$rect[2]) -or
            $y -ge ([int]$rect[1]+[int]$rect[3])) {
            return [ordered]@{ code='out_of_window'; message='coordinates are outside the target window' }
        }
        $vs=[System.Windows.Forms.SystemInformation]::VirtualScreen
        if ($x -lt $vs.X -or $y -lt $vs.Y -or $x -ge ($vs.X+$vs.Width) -or $y -ge ($vs.Y+$vs.Height)) {
            return [ordered]@{ code='out_of_bounds'; message='coordinates are outside the virtual screen' }
        }
        if (-not [HarnessDesktopNative]::SetCursorPos($x,$y)) {
            return [ordered]@{ code='input_failed'; message='SetCursorPos failed' }
        }
        if ($action -eq 'click') {
            # Short hover dwell: menus and tooltips need the cursor to settle
            # before the button goes down.
            Start-Sleep -Milliseconds 20
            $button = 'left'
            if ($null -ne $step.button) { $button = ([string]$step.button).ToLowerInvariant() }
            $down = 0x0002; $up = 0x0004
            if ($button -eq 'right') { $down = 0x0008; $up = 0x0010 }
            elseif ($button -eq 'middle') { $down = 0x0020; $up = 0x0040 }
            elseif ($button -ne 'left') {
                return [ordered]@{ code='invalid_button'; message='button must be left, right, or middle' }
            }
            $count = 1
            if ($null -ne $step.count) { $count = [int]$step.count }
            if ($count -lt 1 -or $count -gt 2) {
                return [ordered]@{ code='invalid_count'; message='click count must be 1 or 2' }
            }
            for ($i = 0; $i -lt $count; $i++) {
                [HarnessDesktopNative]::mouse_event($down,0,0,0,[UIntPtr]::Zero)
                [HarnessDesktopNative]::mouse_event($up,0,0,0,[UIntPtr]::Zero)
                if ($i -lt $count - 1) { Start-Sleep -Milliseconds 40 }
            }
        }
    } elseif ($action -eq 'scroll') {
        $amount=[int]$step.amount
        if ([Math]::Abs($amount) -gt 20 -or $amount -eq 0) {
            return [ordered]@{ code='invalid_amount'; message='scroll amount must be between -20 and 20' }
        }
        # Wheel events land on the window under the cursor, not necessarily the
        # identity-checked foreground window: clamp the cursor into its rect first.
        $rect=$state.rect
        if ($null -ne $rect) {
            $cur=[System.Windows.Forms.Cursor]::Position
            if ($cur.X -lt [int]$rect[0] -or $cur.Y -lt [int]$rect[1] -or
                $cur.X -ge ([int]$rect[0]+[int]$rect[2]) -or
                $cur.Y -ge ([int]$rect[1]+[int]$rect[3])) {
                $cx=[int]([int]$rect[0] + [int]$rect[2]/2)
                $cy=[int]([int]$rect[1] + [int]$rect[3]/2)
                [HarnessDesktopNative]::SetCursorPos($cx,$cy) | Out-Null
            }
        }
        [HarnessDesktopNative]::mouse_event(0x0800,0,0,$amount*120,[UIntPtr]::Zero)
    } elseif ($action -eq 'type') {
        $text=[string]$step.text
        if ($text.Length -gt 1000) {
            return [ordered]@{ code='text_too_long'; message='typed text exceeds 1000 characters' }
        }
        foreach ($ch in $text.ToCharArray()) {
            [HarnessDesktopNative]::Unicode([uint16][char]$ch,$false)
            [HarnessDesktopNative]::Unicode([uint16][char]$ch,$true)
        }
    } elseif ($action -eq 'key') {
        # "ctrl+v" / "ctrl+shift+s": hold modifiers, tap the base key, release.
        $parts = ([string]$step.key).ToLowerInvariant().Split('+')
        $base = $parts[$parts.Length - 1]
        $mods = @()
        if ($parts.Length -gt 1) { $mods = $parts[0..($parts.Length - 2)] }
        $modMap=@{'ctrl'=17;'alt'=18;'shift'=16;'win'=91}
        $map=@{'enter'=13;'return'=13;'tab'=9;'escape'=27;'esc'=27;'backspace'=8;'delete'=46;'home'=36;'end'=35;'left'=37;'up'=38;'right'=39;'down'=40;'space'=32;'pageup'=33;'pagedown'=34;'insert'=45}
        $vk=0
        if ($base -match '^f([1-9]|1[0-2])$') { $vk=111+[int]$Matches[1] }
        elseif ($map.ContainsKey($base)) { $vk=$map[$base] }
        elseif ($base.Length -eq 1 -and [int][char]$base -lt 128) { $vk=[int][char]$base.ToUpperInvariant() }
        else { return [ordered]@{ code='invalid_key'; message='unsupported key name' } }
        foreach ($mod in $mods) {
            if (-not $modMap.ContainsKey($mod)) {
                return [ordered]@{ code='invalid_key'; message="unsupported modifier: $mod" }
            }
            [HarnessDesktopNative]::VirtualKey([uint16]$modMap[$mod],$false)
        }
        [HarnessDesktopNative]::VirtualKey([uint16]$vk,$false)
        [HarnessDesktopNative]::VirtualKey([uint16]$vk,$true)
        for ($i = $mods.Length - 1; $i -ge 0; $i--) {
            [HarnessDesktopNative]::VirtualKey([uint16]$modMap[$mods[$i]],$true)
        }
    } elseif ($action -eq 'wait') {
        $ms=[int]$step.ms
        if ($ms -lt 50 -or $ms -gt 3000) {
            return [ordered]@{ code='invalid_wait'; message='wait must be 50-3000 ms' }
        }
        Start-Sleep -Milliseconds $ms
    } else {
        return [ordered]@{ code='invalid_action'; message='unsupported desktop action' }
    }
    return $null
}
function Get-VisibleWindows {
    # 枚举有标题的可见顶层窗口：模型据此选目标，而不是靠用户手动切。
    # 进程名一次取全表：每个窗口单独 Get-Process 是 8ms 级开销，20 个窗口就是
    # 160ms——实测占本操作耗时的 90% 以上。
    $procNames = @{}
    try {
        foreach ($proc in [System.Diagnostics.Process]::GetProcesses()) {
            try { $procNames[$proc.Id] = $proc.ProcessName } catch { }
        }
    } catch { }
    $items = New-Object System.Collections.ArrayList
    $foreground = [HarnessDesktopNative]::GetForegroundWindow()
    $callback = {
        param($hwnd, $lparam)
        try {
            if (-not [HarnessDesktopNative]::IsWindowVisible($hwnd)) { return $true }
            $len = [HarnessDesktopNative]::GetWindowTextLength($hwnd)
            if ($len -le 0) { return $true }
            $sb = New-Object Text.StringBuilder ($len + 1)
            [HarnessDesktopNative]::GetWindowText($hwnd, $sb, $sb.Capacity) | Out-Null
            $title = $sb.ToString()
            if (-not $title.Trim()) { return $true }
            $procId = [uint32]0
            [HarnessDesktopNative]::GetWindowThreadProcessId($hwnd, [ref]$procId) | Out-Null
            $process = ''
            if ($procNames.ContainsKey([int]$procId)) { $process = $procNames[[int]$procId] }
            elseif ($procNames.ContainsKey($procId)) { $process = $procNames[$procId] }
            $rect = New-Object HarnessDesktopNative+RECT
            $hasRect = [HarnessDesktopNative]::GetWindowRect($hwnd, [ref]$rect)
            $items.Add([ordered]@{
                title = $title
                process = $process
                pid = [int]$procId
                window_id = $hwnd.ToInt64().ToString('X')
                foreground = ($hwnd -eq $foreground)
                minimized = [bool][HarnessDesktopNative]::IsIconic($hwnd)
                rect = if ($hasRect) {
                    @($rect.Left, $rect.Top, ($rect.Right - $rect.Left), ($rect.Bottom - $rect.Top))
                } else { $null }
            }) | Out-Null
        } catch { }
        return $true
    }
    [HarnessDesktopNative]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
    return $items.ToArray()
}

function New-Failure($code, $message, $state, $step = 0, $done = $null) {
    $obj = [ordered]@{ ok=$false; error=$code; message=$message; state=$state }
    if ($step -gt 0) { $obj.step = [int]$step }
    if ($null -ne $done) { $obj.steps_done = @($done) }
    return $obj
}

# worker 模式下失败对象先放这里，由顶层 catch 统一回包；独立进程模式直接输出
# 并靠 exit 结束。早先写成「返回失败对象」会让调用点继续跑完，一个请求回两次包。
$script:HarnessFailure = $null

function Fail($code, $message, $state, $step = 0, $done = $null) {
    $failure = New-Failure $code $message $state $step $done
    $script:HarnessFailure = $failure
    if (-not (Get-Command Write-Reply -ErrorAction SilentlyContinue)) {
        Write-Result $failure 2
    }
    throw [System.Exception]::new('__harness_failure__')
}
'''

_CONTROL_BODY = r'''
function Invoke-HarnessOp($req) {
    $payload = Read-Payload $req
    $state = Get-WindowState
    if ($payload.action -eq 'snapshot') {
        # 只返回对象，由顶层循环统一回包。这里若自己再 Write-Result，一个请求会
        # 产生两个响应，之后每个请求都读到上一条的答复（协议整体错位）。
        return [ordered]@{ ok = $true; state = $state }
    }
    if ($payload.action -eq 'list') {
        # 只读：列出可见窗口，不用先知道前台是谁。
        $limit = [int]$payload.limit
        if ($limit -le 0) { $limit = 20 }
        $all = @(Get-VisibleWindows)
        return [ordered]@{
            ok = $true
            total = $all.Count
            windows = @($all | Select-Object -First $limit)
        }
    }
    if ($payload.action -eq 'focus') {
        # 切窗口改变的是「后续动作打在哪」，属于输入操作：要求显式 expected_*。
        $target = [IntPtr][Convert]::ToInt64([string]$payload.expected_window_id, 16)
        if ($target -eq [IntPtr]::Zero) {
            return New-Failure 'invalid_window' 'expected_window_id must be a window id' $state
        }
        $procId = [uint32]0
        [HarnessDesktopNative]::GetWindowThreadProcessId($target, [ref]$procId) | Out-Null
        if ([int]$procId -ne [int]$payload.expected_pid) {
            return New-Failure 'window_gone' 'that window no longer belongs to the expected process' $state
        }
        if ([HarnessDesktopNative]::IsIconic($target)) {
            [HarnessDesktopNative]::ShowWindow($target, 9) | Out-Null   # SW_RESTORE
        }
        # Windows 的前台锁：后台进程抢焦点经常被拒（实测约一半会失败）。
        # 用 AttachThreadInput 把本进程的输入队列临时接到当前前台线程上，
        # 这是绕过该限制的常规做法；仍失败时如实说明，并建议用户手动点一下
        # 目标窗口——这比静默失败或假装成功要好。
        $placed = [HarnessDesktopNative]::SetForegroundWindow($target)
        if (-not $placed) {
            $fgPid = [uint32]0
            $fgWindow = [HarnessDesktopNative]::GetForegroundWindow()
            $fgThread = [HarnessDesktopNative]::GetWindowThreadProcessId(
                $fgWindow, [ref]$fgPid)
            $self = [HarnessDesktopNative]::GetCurrentThreadId()
            $attached = $false
            try {
                $attached = [HarnessDesktopNative]::AttachThreadInput($self, $fgThread, $true)
                [HarnessDesktopNative]::BringWindowToTop($target) | Out-Null
                $placed = [HarnessDesktopNative]::SetForegroundWindow($target)
            } catch { } finally {
                if ($attached) {
                    [HarnessDesktopNative]::AttachThreadInput($self, $fgThread, $false) | Out-Null
                }
            }
        }
        if (-not $placed) {
            $why = 'Windows refused to raise that window. Click it once '
            $why += 'yourself, then retry (foreground lock blocks background processes).'
            return New-Failure 'focus_failed' $why $state
        }
        # 轮询而不是固定 sleep：多数切换 10-30ms 就到位，固定等 120ms 等于每次
        # 白付 90ms。上限 300ms 兜住慢机器/动画长的窗口。
        $deadline = [DateTime]::UtcNow.AddMilliseconds(300)
        while ([DateTime]::UtcNow -lt $deadline) {
            if ([HarnessDesktopNative]::GetForegroundWindow() -eq $target) { break }
            Start-Sleep -Milliseconds 10
        }
        return [ordered]@{ ok = $true; action = 'focus'; state_after = (Get-WindowState) }
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
    if ($action -eq 'sequence') {
        $steps = @($payload.steps)
        $total = $steps.Count
        $done = New-Object System.Collections.ArrayList
        $index = 0
        foreach ($step in $steps) {
            $index++
            $label = "step $index/$total ($([string]$step.action))"
            $current = Get-WindowState
            if (-not $current.available) {
                Fail 'no_foreground_window' "$label`: no foreground window" $current $index $done
            }
            if ([int]$current.pid -ne [int]$payload.expected_pid -or
                [string]$current.window_id -ne [string]$payload.expected_window_id) {
                Fail 'foreground_changed' "$label`: foreground window changed; re-snapshot" $current $index $done
            }
            if ($current.sensitive_target) {
                Fail 'sensitive_target' "$label`: input blocked on a password or sensitive window" $current $index $done
            }
            $err = Invoke-Step $step $current
            if ($null -ne $err) {
                Fail $err.code "$label`: $($err.message)" $current $index $done
            }
            $done.Add([ordered]@{ action=[string]$step.action; ok=$true }) | Out-Null
            if ($index -lt $total) { Start-Sleep -Milliseconds 50 }
        }
        Start-Sleep -Milliseconds 30
        $after = Get-WindowState
        return [ordered]@{ ok=$true; action='sequence'; steps=$done.ToArray(); state_after=$after }
    }
    $err = Invoke-Step $payload $state
    if ($null -ne $err) { Fail $err.code $err.message $state }
    Start-Sleep -Milliseconds 30
    $after=Get-WindowState
    return [ordered]@{ ok=$true; action=$action; state_before=$state; state_after=$after }
}
'''

_CONTROL_MAIN = r'''
# 独立进程模式的入口：调一次、输出一次、退出。worker 模式不执行这一段。
try {
    $payload = Read-Payload $null
    $result = Invoke-HarnessOp $payload
    if ($null -eq $result) { $result = @{ ok = $true } }
    Write-Result $result 0
} catch {
    $msg = $_.Exception.Message
    if ($null -ne $script:HarnessFailure) {
        Write-Result $script:HarnessFailure 2
    } else {
        Write-Result ([ordered]@{ ok=$false; error='desktop_control_failed';
            message=$msg }) 2
    }
}
'''

# 兼容入口：既有的独立进程测试把 _CONTROL_SETUP + _CONTROL_BODY 拼起来跑。
CONTROL_SCRIPT = _CONTROL_SETUP + _CONTROL_BODY + _CONTROL_MAIN


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


def worker_script() -> str:
    """常驻 worker 用的完整脚本：prelude + 装配 + 请求循环。

    装配（Add-Type 编译）只在进程启动时做一次，之后每次请求只跑
    Invoke-HarnessOp——这是把 882ms/次 降到 0.2ms/次 的关键。
    """
    import ps_worker

    return (ps_worker.PRELUDE + _CONTROL_SETUP
            + _CONTROL_BODY + ps_worker.LOOP)


def _run_worker(payload: dict, *, timeout: float) -> dict:
    import ps_worker

    reply = ps_worker.run('desktop', worker_script, payload,
                          timeout=max(timeout, ps_worker.DEFAULT_TIMEOUT))
    return reply


def _run(payload: dict, *, runner=None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    # 显式注入 runner（测试）或关闭 worker 时走「每次起进程」的老路径。
    if runner is None and _worker_enabled():
        try:
            return _run_worker(payload, timeout=timeout)
        except ps_worker_error_types() as exc:
            if not _worker_fallback_allowed():
                raise DesktopControlError(str(exc)) from exc
            # 结构化失败（窗口变了、坐标越界）不是 worker 故障：重跑一次独立进程
            # 只会得到同样的结果，还会把可读的 message 换成「unexpected output」。
            # 只有 worker 真的不可用（启动失败/超时/协议错）才降级。
            if _is_worker_failure(exc):
                print(f'  [33m[desktop] worker unavailable, falling back: {exc}[0m')
            else:
                raise DesktopControlError(str(exc)) from exc
    return _run_standalone(payload, runner=runner, timeout=timeout)


def ps_worker_error_types():
    """worker 层可能抛出的异常类型（延迟 import，避免模块级循环）。"""
    import ps_worker

    return ps_worker.WorkerError


# 这些关键字说明是 worker 本身不可用，而不是「请求被正确拒绝」。
_WORKER_FAILURE_HINTS = (
    'not available', 'cannot start', 'did not become ready', 'closed unexpectedly',
    'stdin closed', 'non-JSON', 'not running', 'is unavailable', 'timed out',
    'exited during startup',
)


def _is_worker_failure(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(hint in text for hint in _WORKER_FAILURE_HINTS)


def _worker_enabled() -> bool:
    return (os.getenv('HARNESS_PS_WORKER') or '').strip().casefold() not in (
        '0', 'false', 'off', 'no')


def _worker_fallback_allowed() -> bool:
    return (os.getenv('HARNESS_PS_WORKER_FALLBACK') or '').strip().casefold() not in (
        '0', 'false', 'off', 'no')


def _run_standalone(payload: dict, *, runner=None, timeout: float = DEFAULT_TIMEOUT) -> dict:
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
        stdout = stdout.decode('ascii', 'replace')
    stderr = getattr(result, 'stderr', b'') or b''
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', 'replace')
    record = None
    try:
        record = json.loads(base64.b64decode(str(stdout).strip(), validate=True).decode('utf-8'))
    except (TypeError, ValueError):
        record = None
    if not isinstance(record, dict):
        raise DesktopControlError(f'unexpected desktop control output: {str(stderr).strip()[-300:]}')
    if getattr(result, 'returncode', 0) or not record.get('ok'):
        raise DesktopControlError(str(record.get('message') or record.get('error') or 'desktop control failed'))
    return record


def snapshot(*, runner=None, timeout: float = DEFAULT_TIMEOUT) -> WindowState:
    return _state(_run({'action': 'snapshot'}, runner=runner, timeout=timeout).get('state'))


def list_windows(*, limit: int = 20, runner=None,
                 timeout: float = DEFAULT_TIMEOUT) -> dict:
    """列出可见顶层窗口。返回 ``{'total': n, 'windows': [...]}``。

    只读，不改变任何状态——给模型一个「先看看有哪些目标」的入口，而不是
    必须靠用户手动切到某个窗口再猜。
    """
    try:
        limit = max(1, min(int(limit), MAX_WINDOWS))
    except (TypeError, ValueError) as exc:
        raise DesktopControlError('limit must be an integer') from exc
    reply = _run({'action': 'list', 'limit': limit}, runner=runner,
                 timeout=timeout)
    windows = []
    for item in reply.get('windows') or []:
        if not isinstance(item, dict):
            continue
        rect = item.get('rect')
        try:
            rect = tuple(int(v) for v in rect) if rect else None
        except (TypeError, ValueError):
            rect = None
        windows.append({
            'title': str(item.get('title') or ''),
            'process': str(item.get('process') or ''),
            'pid': int(item.get('pid') or 0),
            'window_id': str(item.get('window_id') or ''),
            'foreground': bool(item.get('foreground')),
            'minimized': bool(item.get('minimized')),
            'rect': rect,
        })
    return {'total': int(reply.get('total') or len(windows)),
            'windows': windows}


def focus_window(*, expected_pid=None, expected_window_id=None, runner=None,
                 timeout: float = DEFAULT_TIMEOUT) -> WindowState:
    """把指定窗口切到前台。仍需 expected_pid 双因子，避免打到刚被复用的窗口 ID。"""
    payload = validate_action('focus', expected_pid=expected_pid,
                              expected_window_id=expected_window_id)
    reply = _run(payload, runner=runner, timeout=timeout)
    return _state(reply.get('state_after'))


def _validated_key(key) -> str:
    """Normalize one key or a modifier combo such as ``ctrl+shift+s``."""
    if not isinstance(key, str) or not key.strip() or len(key) > 30:
        raise DesktopControlError('key requires a short key name or combo')
    parts = [part.strip().casefold() for part in key.strip().split('+')]
    if any(not part for part in parts):
        raise DesktopControlError('key combo has an empty part')
    *modifiers, base = parts
    if len(modifiers) > 4:
        raise DesktopControlError('key combo allows at most four modifiers')
    for modifier in modifiers:
        if modifier not in KEY_MODIFIERS:
            raise DesktopControlError(f'unsupported modifier: {modifier}')
    if len(set(modifiers)) != len(modifiers):
        raise DesktopControlError('key combo repeats a modifier')
    if base not in SUPPORTED_KEYS and not (
            len(base) == 1 and base.isascii() and base.isalnum()) and not (
            base.startswith('f') and base[1:].isdigit()
            and 1 <= int(base[1:]) <= 12):
        raise DesktopControlError('unsupported key name')
    return '+'.join([*modifiers, base])


def _validated_click(x_value, y_value, button, count) -> dict:
    try:
        coords = {'x': int(x_value), 'y': int(y_value)}
    except (TypeError, ValueError) as exc:
        raise DesktopControlError('click requires integer x and y') from exc
    normalized = {'action': 'click', **coords}
    button = 'left' if button is None else str(button).strip().casefold()
    if button not in CLICK_BUTTONS:
        raise DesktopControlError('button must be left, right, or middle')
    if button != 'left':
        normalized['button'] = button
    count = 1 if count is None else count
    try:
        count = int(count)
    except (TypeError, ValueError) as exc:
        raise DesktopControlError('click count must be 1 or 2') from exc
    if count not in (1, 2):
        raise DesktopControlError('click count must be 1 or 2')
    if count != 1:
        normalized['count'] = count
    return normalized


def _validated_step(step) -> dict:
    """Validate one sequence step; returns the normalized payload for it."""
    if not isinstance(step, dict):
        raise DesktopControlError('each sequence step must be an object')
    action = str(step.get('action') or '').strip().casefold()
    if action not in STEP_FIELDS:
        raise DesktopControlError(
            'each step needs action: move, click, type, key, scroll, or wait')
    unknown = set(step) - STEP_FIELDS[action]
    if unknown:
        raise DesktopControlError(
            f"{action} step has unknown fields: {', '.join(sorted(unknown))}")
    if action == 'click':
        return _validated_click(step.get('x'), step.get('y'),
                                step.get('button'), step.get('count'))
    if action == 'move':
        try:
            coords = {'x': int(step['x']), 'y': int(step['y'])}
        except (KeyError, TypeError, ValueError) as exc:
            raise DesktopControlError('move step requires integer x and y') from exc
        return {'action': action, **coords}
    if action == 'type':
        text = step.get('text')
        if not isinstance(text, str) or len(text) > MAX_TYPE_CHARS:
            raise DesktopControlError(
                f'type step text must be a string of at most {MAX_TYPE_CHARS} characters')
        return {'action': action, 'text': text}
    if action == 'key':
        return {'action': action, 'key': _validated_key(step.get('key'))}
    if action == 'wait':
        try:
            ms = int(step.get('ms'))
        except (TypeError, ValueError) as exc:
            raise DesktopControlError('wait step requires integer ms') from exc
        if not MIN_WAIT_MS <= ms <= MAX_WAIT_MS:
            raise DesktopControlError(
                f'wait ms must be between {MIN_WAIT_MS} and {MAX_WAIT_MS}')
        return {'action': action, 'ms': ms}
    try:
        amount = int(step.get('amount'))
    except (TypeError, ValueError) as exc:
        raise DesktopControlError(
            'scroll step requires a non-zero integer amount') from exc
    if amount == 0 or abs(amount) > MAX_SCROLL:
        raise DesktopControlError(
            f'scroll amount must be between -{MAX_SCROLL} and {MAX_SCROLL}, excluding 0')
    return {'action': action, 'amount': amount}


def validate_action(action: str, *, expected_pid=None, expected_window_id=None,
                    x=None, y=None, text=None, key=None, amount=None,
                    button=None, count=None, steps=None) -> dict:
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
    if action == 'sequence':
        if any(value is not None for value in (x, y, text, key, amount, button, count)):
            raise DesktopControlError(
                'sequence takes steps only; put x/y/text/key/amount/button/count '
                'inside each step')
        if not isinstance(steps, (list, tuple)) or not steps:
            raise DesktopControlError('sequence requires a non-empty steps list')
        if len(steps) > MAX_SEQUENCE_STEPS:
            raise DesktopControlError(f'sequence is limited to {MAX_SEQUENCE_STEPS} steps')
        return {'action': 'sequence', 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id,
                'steps': [_validated_step(step) for step in steps]}
    if steps is not None:
        raise DesktopControlError("steps is only valid with action='sequence'")
    if action == 'focus':
        # 切窗口只需要双因子身份，没有坐标/文本参数。
        return {'action': 'focus', 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id}
    if action not in SINGLE_ACTIONS:
        raise DesktopControlError(
            'action must be move, click, type, key, scroll, sequence, or focus')
    if action != 'click' and (button is not None or count is not None):
        raise DesktopControlError('button and count are only valid with click')
    if action == 'move':
        try:
            coords = {'x': int(x), 'y': int(y)}
        except (TypeError, ValueError) as exc:
            raise DesktopControlError('move requires integer x and y') from exc
        return {'action': action, 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id, **coords}
    if action == 'click':
        return {'expected_pid': expected_pid,
                'expected_window_id': expected_window_id,
                **_validated_click(x, y, button, count)}
    if action == 'type':
        if not isinstance(text, str) or len(text) > MAX_TYPE_CHARS:
            raise DesktopControlError(f'type text must be a string of at most {MAX_TYPE_CHARS} characters')
        return {'action': action, 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id, 'text': text}
    if action == 'key':
        return {'action': action, 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id,
                'key': _validated_key(key)}
    try:
        amount = int(amount)
    except (TypeError, ValueError) as exc:
        raise DesktopControlError('scroll requires a non-zero integer amount') from exc
    if amount == 0 or abs(amount) > MAX_SCROLL:
        raise DesktopControlError(f'scroll amount must be between -{MAX_SCROLL} and {MAX_SCROLL}, excluding 0')
    return {'action': action, 'expected_pid': expected_pid,
            'expected_window_id': expected_window_id, 'amount': amount}


def action(action: str, *, expected_pid=None, expected_window_id=None, x=None,
           y=None, text=None, key=None, amount=None, button=None, count=None,
           steps=None, runner=None, timeout: float = DEFAULT_TIMEOUT) -> ActionResult:
    payload = validate_action(action, expected_pid=expected_pid,
                              expected_window_id=expected_window_id, x=x, y=y,
                              text=text, key=key, amount=amount,
                              button=button, count=count, steps=steps)
    if payload['action'] == 'sequence':
        wait_s = sum(step.get('ms', 0) for step in payload['steps']) / 1000.0
        timeout = min(60.0, max(
            timeout, DEFAULT_TIMEOUT + 2.0 * len(payload['steps']) + wait_s))
    started = time.monotonic()
    record = _run(payload, runner=runner, timeout=timeout)
    step_records = tuple(item for item in (record.get('steps') or [])
                         if isinstance(item, dict))
    return ActionResult(
        action=payload['action'], state=_state(record.get('state_after')),
        steps=step_records,
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
