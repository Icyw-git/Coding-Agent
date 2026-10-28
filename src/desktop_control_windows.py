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
ELEMENT_OPERATIONS = frozenset({'invoke', 'setvalue', 'toggle', 'select',
                                'expand', 'collapse', 'focus'})
WAIT_TARGETS = frozenset({'control', 'window', 'foreground'})
WAIT_STATES = frozenset({'present', 'absent'})
# 注意别和序列步骤里的 MIN_WAIT_MS/MAX_WAIT_MS（50~3000）混用：那是单步
# 「等动画」的时长，这里是「等条件成立」的总预算，量级完全不同。
TOOL_WAIT_MIN_MS = 200
TOOL_WAIT_MAX_MS = 30000
TOOL_WAIT_DEFAULT_MS = 5000
MAX_WAIT_TEXT = 200
MAX_ELEMENT_INDEX = 400
# 界面结构的默认预算。遍历是跨进程调用，元素多的应用（Office 套件、
# 浏览器）能到几千个节点，不设上限就会把一次工具调用拖成几秒。
DEFAULT_TREE_NODES = 120
DEFAULT_TREE_DEPTH = 12
DEFAULT_TREE_MS = 2500
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
    is_terminal: bool = False
    #: 窗口所在显示器的有效 DPI（96 = 100%）。0 表示读不到。
    dpi: int = 0
    error: str = ''


@dataclass(frozen=True)
class ActionResult:
    action: str
    state: WindowState
    duration_ms: int
    steps: tuple[dict, ...] = ()
    target: dict | None = None
    #: 动作之前的窗口状态。工具层用它算「到底变了什么」——只报动作已发出，
    #: 模型无法判断这一步是否生效。
    before: WindowState | None = None


@dataclass(frozen=True)
class ElementInfo:
    """一个可交互控件的快照。"""

    id: int
    label: str
    name: str = ''
    value: str = ''
    password: bool = False
    enabled: bool = True
    focusable: bool = False
    focused: bool = False
    bounds: tuple[int, int, int, int] | None = None
    operations: tuple[str, ...] = ()


@dataclass(frozen=True)
class WaitResult:
    """一次等待的结果。``matched`` 为假表示超时，调用方仍能拿到当前状态。"""

    matched: bool
    target: str
    desired: str
    elapsed_ms: int
    polls: int
    state: WindowState
    element: ElementInfo | None = None
    window: dict | None = None
    fingerprint: str = ''
    elements: int = 0
    windows: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class UiTree:
    """前台窗口的裁剪控件清单。``fingerprint`` 用于动作前发现结构变化。"""

    items: tuple[ElementInfo, ...]
    truncated: bool
    fingerprint: str
    state: WindowState
    duration_ms: int = 0

    @property
    def total(self) -> int:
        return len(self.items)


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
    // 显示器枚举与 DPI。GetDpiForMonitor 在 shcore.dll（Win8.1+），
    // GetDpiForWindow 在 user32（Win10 1607+）；两者都可能不存在，调用处一律 try/catch。
    [StructLayout(LayoutKind.Sequential)] public struct MONITORINFO {
        public int cbSize; public RECT rcMonitor; public RECT rcWork; public uint dwFlags;
    }
    public delegate bool MonitorEnumProc(IntPtr hMonitor, IntPtr hdc, IntPtr rect, IntPtr data);
    [DllImport("user32.dll")]
    public static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorEnumProc cb, IntPtr data);
    [DllImport("user32.dll")]
    public static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFO info);
    [DllImport("shcore.dll")]
    public static extern int GetDpiForMonitor(IntPtr hMonitor, int dpiType, out uint dpiX, out uint dpiY);
    [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr MonitorFromWindow(IntPtr hWnd, uint flags);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")]
    public static extern void mouse_event(
        uint flags, uint dx, uint dy, int data, UIntPtr extra);
    [DllImport("user32.dll")] public static extern uint SendInput(uint count, INPUT[] inputs, int size);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    // 64 位下 INPUT 必须是 40 字节：type(4) + 4 字节对齐填充 + union(32)。
    // 少了 Pack/Size 会得到 32 字节，SendInput 返回 0 且 GetLastError=87
    // （参数错误）——所有真实键鼠注入都会静默失败。
    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT { public uint type; public INPUTUNION u; }
    [StructLayout(LayoutKind.Explicit, Size = 32)]
    public struct INPUTUNION {
        [FieldOffset(0)] public MOUSEINPUT mouse;
        [FieldOffset(0)] public KEYBDINPUT keyboard;
        [FieldOffset(0)] public HARDWAREINPUT hardware;
    }
    [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT {
        public int dx; public int dy; public uint mouseData; public uint dwFlags;
        public uint time; public UIntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT {
        public ushort wVk; public ushort wScan; public uint dwFlags;
        public uint time; public UIntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Sequential)] public struct HARDWAREINPUT {
        public uint uMsg; public ushort wParamL; public ushort wParamH;
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

# 判定用的正则只编译一次。写法上是 `-match`，但它每次调用都会重新编译模式：
# 两条长模式实测共 9ms，占一次 Get-WindowState 的一半。序列里每步都要读一次
# 状态，这笔钱要反复付。
$script:SensitiveRegex = [regex]::new(
    '(?i)password|passwd|payment|checkout|bank|wallet|2fa|verification|' +
    'credit.?card|银行卡|支付|付款|密码|验证码')
$script:TerminalRegex = [regex]::new(
    '(?i)WindowsTerminal|^cmd$|^powershell|^pwsh|^conhost|^mintty|^wt$|' +
    '^bash$|^wsl$|^ConEmu|^Terminal$')
$script:TerminalClassRegex = [regex]::new(
    '(?i)ConsoleWindowClass|CASCADIA|VirtualConsole')
$script:ProcNames = @{}
$script:ProcNamesAt = [DateTime]::MinValue

# 焦点元素只读三个属性（名字 / 类型 / 是否密码框），合并成一次跨进程调用：
# 逐个读 .Current 实测 15ms，批量后 3ms 级。
$script:FocusCache = New-Object System.Windows.Automation.CacheRequest
foreach ($prop in @(
    [System.Windows.Automation.AutomationElement]::NameProperty,
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.AutomationElement]::IsPasswordProperty
)) { $script:FocusCache.Add($prop) }

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
function Get-Displays {
    # 每块显示器的物理矩形 + 工作区 + DPI。进程是 DPI-aware 的，所以这里的
    # 矩形就是物理像素——和截图、desktop_action 同一个坐标系。
    $screens = @{}
    try {
        foreach ($screen in [System.Windows.Forms.Screen]::AllScreens) {
            $key = "$($screen.Bounds.X),$($screen.Bounds.Y),$($screen.Bounds.Width),$($screen.Bounds.Height)"
            $screens[$key] = $screen
        }
    } catch { }
    $items = New-Object System.Collections.ArrayList
    $callback = {
        param($hMonitor, $hdc, $clip, $data)
        $info = New-Object HarnessDesktopNative+MONITORINFO
        $info.cbSize = [System.Runtime.InteropServices.Marshal]::SizeOf($info)
        $ok = $false
        try { $ok = [HarnessDesktopNative]::GetMonitorInfo($hMonitor, [ref]$info) } catch { }
        if (-not $ok) { return $true }
        $width = $info.rcMonitor.Right - $info.rcMonitor.Left
        $height = $info.rcMonitor.Bottom - $info.rcMonitor.Top
        $dpiX = 96; $dpiY = 96
        try {
            $dx = [uint32]0; $dy = [uint32]0
            if ([HarnessDesktopNative]::GetDpiForMonitor($hMonitor, 0, [ref]$dx, [ref]$dy) -eq 0 -and $dx -gt 0) {
                $dpiX = [int]$dx; $dpiY = [int]$dy
            }
        } catch { }
        $key = "$($info.rcMonitor.Left),$($info.rcMonitor.Top),$width,$height"
        $name = ''
        if ($screens.ContainsKey($key)) { $name = [string]$screens[$key].DeviceName }
        if (-not $name) { $name = 'DISPLAY' + ($items.Count + 1) }
        $items.Add([ordered]@{
            name = $name
            primary = (($info.dwFlags -band 1) -eq 1)
            bounds = @($info.rcMonitor.Left, $info.rcMonitor.Top, $width, $height)
            work = @($info.rcWork.Left, $info.rcWork.Top,
                     ($info.rcWork.Right - $info.rcWork.Left),
                     ($info.rcWork.Bottom - $info.rcWork.Top))
            dpi_x = $dpiX
            dpi_y = $dpiY
            scale = [Math]::Round($dpiX / 96.0, 2)
        }) | Out-Null
        return $true
    }
    try {
        [HarnessDesktopNative]::EnumDisplayMonitors(
            [IntPtr]::Zero, [IntPtr]::Zero, $callback, [IntPtr]::Zero) | Out-Null
    } catch { }
    return $items.ToArray()
}

function Get-MonitorIndexForRect($displays, $rect) {
    # 窗口中心点落在哪块屏上（跨屏时按面积最大那块算）。
    if ($null -eq $rect -or $displays.Count -eq 0) { return 0 }
    $cx = [int]$rect[0] + [int]([int]$rect[2] / 2)
    $cy = [int]$rect[1] + [int]([int]$rect[3] / 2)
    for ($i = 0; $i -lt $displays.Count; $i++) {
        $b = $displays[$i].bounds
        if ($cx -ge $b[0] -and $cy -ge $b[1] -and
            $cx -lt ($b[0] + $b[2]) -and $cy -lt ($b[1] + $b[3])) { return ($i + 1) }
    }
    return 0
}

function Get-ProcNames {
    # pid → 进程名。全表重建实测 20-24ms，而窗口列表和每次状态读取都要用；
    # 等待轮询是 100ms 一轮、序列动作每步都读一次状态，这笔钱会被放大。
    # 2 秒 TTL：进程名是展示字段，短暂过期无害（新进程查不到就回落 ''）。
    $age = ([DateTime]::UtcNow - $script:ProcNamesAt).TotalSeconds
    if ($script:ProcNames.Count -gt 0 -and $age -lt 2) { return $script:ProcNames }
    $map = @{}
    try {
        foreach ($proc in [System.Diagnostics.Process]::GetProcesses()) {
            try { $map[[int]$proc.Id] = $proc.ProcessName } catch { }
        }
    } catch { }
    $script:ProcNames = $map
    $script:ProcNamesAt = [DateTime]::UtcNow
    return $map
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
    # 先查缓存表（一次全表 20ms，2 秒内复用）；没命中再单查一次
    # （Process.GetProcessById 6ms，比 Get-Process 的 14ms 快一倍）。
    $names = Get-ProcNames
    if ($names.ContainsKey([int]$procId)) { $process = [string]$names[[int]$procId] }
    else {
        try {
            $process = [System.Diagnostics.Process]::GetProcessById([int]$procId).ProcessName
        } catch { }
    }
    $rect = New-Object HarnessDesktopNative+RECT
    $hasRect = [HarnessDesktopNative]::GetWindowRect($hwnd, [ref]$rect)
    $focusedName = ''; $focusedType = ''; $password = $false
    try {
        $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
        if ($null -ne $focused) {
            $cachedFocus = $focused.GetUpdatedCache($script:FocusCache)
            $fc = $focused.Cached
            if ($null -ne $cachedFocus) { $fc = $cachedFocus.Cached }
            $focusedName = [string]$fc.Name
            $focusedType = [string]$fc.ControlType.ProgrammaticName
            $password = [bool]$fc.IsPassword
        }
    } catch {}
    $all = "$($titleBuffer.ToString()) $process $($classBuffer.ToString()) $focusedName"
    $sensitive = $password -or $script:SensitiveRegex.IsMatch($all)
    # 终端类窗口单列：往里键入的任何字符都会被当作命令执行，风险高于普通窗口，
    # 而它的标题通常不含敏感词（就是 'PowerShell' / 'cmd'），敏感词匹配抓不到。
    $isTerminal = ($script:TerminalRegex.IsMatch($process) -or
                   $script:TerminalClassRegex.IsMatch($classBuffer.ToString()))
    # 窗口所在显示器的有效 DPI。150% 缩放下「逻辑坐标」和「物理坐标」差 1.5 倍，
    # 这是点偏位置最常见的成因，所以让它跟着状态一起回传。
    $dpi = 0
    try { $dpi = [int][HarnessDesktopNative]::GetDpiForWindow($hwnd) } catch { }
    return [ordered]@{
        available=$true; title=$titleBuffer.ToString(); process=$process; pid=[int]$procId
        window_id=$hwnd.ToInt64().ToString('X')
        class_name=$classBuffer.ToString()
        rect=if ($hasRect) { @($rect.Left,$rect.Top,($rect.Right-$rect.Left),($rect.Bottom-$rect.Top)) } else { $null }
        focused_name=$focusedName; focused_control_type=$focusedType
        focused_is_password=$password; sensitive_target=$sensitive
        is_terminal=$isTerminal; dpi=$dpi
    }
}

# ---------- 界面结构（裁剪后的无障碍树） ----------
# 只读、有预算：一个病理性的无障碍提供程序能让整次遍历卡到几秒甚至挂死，
# 所以每次遍历都带节点数 / 深度 / 时间三重上限，超了就返回已经采集到的部分。
$script:CONTROL_LABELS = @{
    'Button'='按钮'; 'Edit'='输入框'; 'ComboBox'='下拉框'; 'CheckBox'='复选框'
    'RadioButton'='单选框'; 'Hyperlink'='链接'; 'List'='列表'; 'ListItem'='列表项'
    'Tree'='树'; 'TreeItem'='树项'; 'Tab'='选项卡栏'; 'TabItem'='选项卡'
    'Menu'='菜单'; 'MenuItem'='菜单项'; 'MenuBar'='菜单栏'; 'Slider'='滑块'
    'Spinner'='数字框'; 'SplitButton'='分裂按钮'; 'Text'='文本'; 'Image'='图片'
    'Document'='文档'; 'Table'='表格'; 'DataGrid'='表格'; 'DataItem'='数据项'
    'Header'='表头'; 'HeaderItem'='表头项'; 'Group'='分组'; 'Pane'='面板'
    'Custom'='自绘控件'; 'Window'='窗口'; 'ToolBar'='工具栏'; 'StatusBar'='状态栏'
    'ProgressBar'='进度条'; 'Calendar'='日历'
}
# 纯容器：没有名字就不单独占一行，但里面的控件要继续往下看。
$script:TREE_CONTAINERS = @('Pane','Group','Custom','Window','List','Tree','Table',
    'DataGrid','Menu','MenuBar','ToolBar','StatusBar','TitleBar')
# 纯噪声：既不可交互也不提供信息，连子树一起跳过。
$script:TREE_NOISE = @('ScrollBar','Separator','ToolTip')
# 叶子类型：不会包含值得看的子控件，不必再花一次跨进程调用去问它的子节点。
# Chromium 系每次 FindAll(Children) 约 30ms，一个全是按钮的工具栏就能省掉一秒。
$script:TREE_LEAVES = @('Button','SplitButton','Edit','CheckBox','RadioButton',
    'Hyperlink','Text','Image','MenuItem','TabItem','Slider','Spinner',
    'ProgressBar','HeaderItem','DataItem','ScrollBar','Separator','ToolTip')
# 文档内部（网页正文 / PDF 页面）单独限额：网页里的链接和输入框都在 Document
# 下面，不能整个跳过；但 PDF 一页的文本块能到两万多个节点（实测整棵子树枚举
# 32 秒），所以进文档只走前这么多节点就停。
$script:TREE_DOCUMENT_BUDGET = 40
# 能读值的类型。文档类故意不读：整篇正文会把上下文撑爆。
$script:TREE_VALUE_TYPES = @('Edit','ComboBox','Spinner')

function Get-ControlLabel($controlType) {
    $key = [string]$controlType
    if ($script:CONTROL_LABELS.ContainsKey($key)) { return $script:CONTROL_LABELS[$key] }
    if (-not $key) { return '控件' }
    return $key
}

function New-TreeCacheRequest {
    # 属性一次批量取回：UIA 每个属性都是一次跨进程调用，120 个节点逐个读
    # Name/ControlType/Bounds 会是几千次往返。CacheRequest 把它们并进遍历本身。
    $cache = New-Object System.Windows.Automation.CacheRequest
    foreach ($prop in @(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.AutomationElement]::IsEnabledProperty,
        [System.Windows.Automation.AutomationElement]::IsOffscreenProperty,
        [System.Windows.Automation.AutomationElement]::IsPasswordProperty,
        [System.Windows.Automation.AutomationElement]::BoundingRectangleProperty,
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
        [System.Windows.Automation.AutomationElement]::IsKeyboardFocusableProperty,
        [System.Windows.Automation.AutomationElement]::HasKeyboardFocusProperty
    )) { $cache.Add($prop) }
    foreach ($pat in @(
        [System.Windows.Automation.InvokePattern]::Pattern,
        [System.Windows.Automation.ValuePattern]::Pattern,
        [System.Windows.Automation.TogglePattern]::Pattern,
        [System.Windows.Automation.SelectionItemPattern]::Pattern,
        [System.Windows.Automation.ExpandCollapsePattern]::Pattern
    )) { $cache.Add($pat) }
    # 默认 TreeScope=Element：只缓存这一个元素的属性，不牵连子树。
    return $cache
}

function Get-CachedNode($el, $cache) {
    # 兜底路径。主路径是「激活 CacheRequest + FindAll(Children)」——FindAll 返回的
    # 子节点自带缓存值，逐节点读 .Cached 即可（A/B 实测属性阶段 211ms -> 117ms，
    # WindowsTerminal 51ms -> 11ms）。
    # 但 TreeWalker 拿到的元素、或个别 provider 下 .Cached 会是空，这时才补一次
    # GetUpdatedCache（一次跨进程调用换回全部属性）。
    try { return $el.GetUpdatedCache($cache) } catch { return $null }
}

function Get-CachedPattern($el, $pattern) {
    # TryGetCachedPattern 而不是 GetCachedPattern：后者在「该控件不支持这个模式」
    # 时抛异常，而绝大多数控件都不支持大部分模式。按 5 个模式 × 每个节点算，
    # 抛异常构造占了遍历时间的一半以上（实测 38ms/节点 -> 9ms/节点）。
    $holder = $null
    try {
        if ($el.TryGetCachedPattern($pattern, [ref]$holder)) { return $holder }
    } catch { }
    return $null
}

function Read-ElementNode($el, $cache) {
    # 返回 @{info=有序字典或 $null; descend=是否继续下探}。
    # 元素通常已经在缓存里（遍历期间 CacheRequest 一直激活），直接读 .Cached；
    # 类型读不出来时才补一次 GetUpdatedCache——这条兜底只在少数 provider 上触发。
    $cached = $el
    $c = $el.Cached
    $type = ''
    try { $type = [string]$c.ControlType.ProgrammaticName } catch { }
    if (-not $type) {
        $cached = Get-CachedNode $el $cache
        if ($null -eq $cached) { return @{ info = $null; descend = $false; doc = $false } }
        $c = $cached.Cached
    }
    try {
        $type = [string]$c.ControlType.ProgrammaticName
        if ($type -like 'ControlType.*') { $type = $type.Substring(12) }
        $name = [string]$c.Name
        $password = [bool]$c.IsPassword
        $offscreen = [bool]$c.IsOffscreen
        $enabled = [bool]$c.IsEnabled
        $focused = [bool]$c.HasKeyboardFocus
        $focusable = [bool]$c.IsKeyboardFocusable
        $rect = $c.BoundingRectangle
        $bounds = @()
        if ($rect -ne [System.Windows.Rect]::Empty -and $rect.Width -gt 0) {
            $bounds = @([int]$rect.X, [int]$rect.Y, [int]$rect.Width, [int]$rect.Height)
        }
    } catch { return @{ info = $null; descend = $false; doc = $false } }
    if ($script:TREE_NOISE -contains $type) { return @{ info = $null; descend = $false; doc = $false } }
    # 屏幕外的元素给了坐标也没法点，除非它就是当前焦点（焦点可能在滚动区外）。
    if ($offscreen -and -not $focused) { return @{ info = $null; descend = $false; doc = $false } }

    $patterns = New-Object System.Collections.ArrayList
    if ($null -ne (Get-CachedPattern $cached ([System.Windows.Automation.InvokePattern]::Pattern))) {
        $patterns.Add('invoke') | Out-Null
    }
    if ($null -ne (Get-CachedPattern $cached ([System.Windows.Automation.TogglePattern]::Pattern))) {
        $patterns.Add('toggle') | Out-Null
    }
    if ($null -ne (Get-CachedPattern $cached ([System.Windows.Automation.SelectionItemPattern]::Pattern))) {
        $patterns.Add('select') | Out-Null
    }
    $expand = Get-CachedPattern $cached ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
    if ($null -ne $expand) {
        $state = ''
        try { $state = [string]$expand.Current.ExpandCollapseState } catch { }
        if ($state -eq 'Expanded') { $patterns.Add('collapse') | Out-Null }
        else { $patterns.Add('expand') | Out-Null }
    }
    $value = ''
    $readOnly = $false
    if ($script:TREE_VALUE_TYPES -contains $type) {
        $valuePattern = Get-CachedPattern $cached ([System.Windows.Automation.ValuePattern]::Pattern)
        if ($null -ne $valuePattern) {
            # 密码框的值一次都不读：读进程内本身就是泄漏，而且这类输入属于
            # 「必须用户自己敲」，不是审批能放行的。
            if (-not $password) {
                # 用 Current 而不是 Cached：CacheRequest 只缓存「这个模式存不存在」，
                # 模式自己的属性值即使缓存请求里也读不到（实测 Cached.Value 恒为空，
                # Current.Value 才正确）。只对输入类控件多付一次调用。
                try {
                    $value = [string]$valuePattern.Current.Value
                    $readOnly = [bool]$valuePattern.Current.IsReadOnly
                } catch { }
            }
            if (-not $readOnly) { $patterns.Add('setvalue') | Out-Null }
        }
    }

    $isContainer = $script:TREE_CONTAINERS -contains $type
    $leaf = $script:TREE_LEAVES -contains $type
    $interesting = ($name -ne '') -or (-not $isContainer) -or $focusable -or
                   ($patterns.Count -gt 0) -or ($value -ne '')
    $isDoc = ($type -eq 'Document')
    if (-not $interesting) { return @{ info = $null; descend = (-not $leaf); doc = $isDoc } }

    # 坐标离谱的节点通常是虚拟化容器的占位（实测见过 top=-470309），
    # 给了也没法点，还占掉一次工具调用的名额。
    if ($bounds.Count -eq 4 -and ([Math]::Abs([int]$bounds[1]) -gt 100000 -or
                                  [Math]::Abs([int]$bounds[0]) -gt 100000)) {
        return @{ info = $null; descend = $true; doc = ($type -eq 'Document') }
    }
    $info = [ordered]@{
        label = (Get-ControlLabel $type)
        name = $name
        value = $value
        password = $password
        enabled = $enabled
        focusable = $focusable
        focused = $focused
        bounds = $bounds
        operations = $patterns.ToArray()
    }
    return @{ info = $info; descend = (-not $leaf); doc = $isDoc }
}

function Get-TreeFingerprint($items) {
    # 结构指纹：只覆盖「编号 -> 类型/名字/位置」这条对应关系。动作前重算一遍，
    # 不一致就拒绝——界面变了还按旧编号点，点中的是另一个控件。
    $sb = New-Object Text.StringBuilder
    foreach ($item in $items) {
        [void]$sb.Append($item.id).Append('|').Append($item.label).Append('|')
        [void]$sb.Append($item.name).Append('|').Append(($item.bounds -join ',')).Append([char]10)
    }
    $sha = [System.Security.Cryptography.SHA1]::Create()
    try {
        $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($sb.ToString()))
        return ([BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Get-UiTree($hwnd, $maxNodes, $maxDepth, $maxMs) {
    # 显式栈的前序遍历：编号稳定（同一界面重复调用得到同一套编号），
    # 且能在任何时刻因为预算耗尽而停下，不会因为某个子树是巨兽而卡死。
    if ($hwnd -eq [IntPtr]::Zero) { return $null }
    $cache = New-TreeCacheRequest
    $items = New-Object System.Collections.ArrayList
    $elements = New-Object System.Collections.ArrayList
    $truncated = $false
    $started = [DateTime]::UtcNow
    $controlView = [System.Windows.Automation.Automation]::ControlViewCondition
    $activation = $cache.Activate()
    try {
        $root = $null
        try { $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd) } catch { }
        if ($null -eq $root) { return $null }
        $stack = New-Object System.Collections.Stack
        $stack.Push(@($root, 0, $false))
        $docNodes = 0
        while ($stack.Count -gt 0) {
            if ($items.Count -ge $maxNodes) { $truncated = $true; break }
            if (([DateTime]::UtcNow - $started).TotalMilliseconds -gt $maxMs) {
                $truncated = $true
                break
            }
            $frame = $stack.Pop()
            $el = $frame[0]
            $depth = [int]$frame[1]
            $underDoc = [bool]$frame[2]
            if ($null -eq $el) { continue }
            $node = Read-ElementNode $el $cache
            if ($null -ne $node.info) {
                $node.info.id = $items.Count + 1
                $items.Add($node.info) | Out-Null
                $elements.Add($el) | Out-Null
                if ($underDoc) { $docNodes++ }
            }
            if (-not $node.descend) { continue }
            if ($depth -ge $maxDepth) { $truncated = $true; continue }
            if ([bool]$node.doc) { $underDoc = $true }
            if ($underDoc -and $docNodes -ge $script:TREE_DOCUMENT_BUDGET) {
                $truncated = $true
                continue
            }
            # 一次调用取回全部子节点，而不是 GetFirstChild + N 次 GetNextSibling。
            # 实测 Chromium 系每次 GetNextSibling 要走一次跨进程调用（约 25ms），
            # 一个节点两个子节点就 50ms，120 个节点直接三秒开外。
            $found = $null
            try { $found = $el.FindAll([System.Windows.Automation.TreeScope]::Children, $controlView) } catch { }
            if ($null -eq $found -or $found.Count -eq 0) { continue }
            for ($i = $found.Count - 1; $i -ge 0; $i--) {
                $stack.Push(@($found[$i], ($depth + 1), $underDoc))
            }
        }
    } catch {
        $truncated = $true
    } finally {
        try { $activation.Dispose() } catch { }
    }
    return @{
        items = $items
        elements = $elements
        truncated = $truncated
        total = $items.Count
        fingerprint = (Get-TreeFingerprint $items)
        duration_ms = [int](([DateTime]::UtcNow - $started).TotalMilliseconds)
    }
}

function Get-TreeBudget($payload) {
    $nodes = 120
    if ($null -ne $payload.max_nodes) { $nodes = [int]$payload.max_nodes }
    if ($nodes -lt 5) { $nodes = 5 }
    if ($nodes -gt 400) { $nodes = 400 }
    $depth = 12
    if ($null -ne $payload.max_depth) { $depth = [int]$payload.max_depth }
    if ($depth -lt 2) { $depth = 2 }
    if ($depth -gt 40) { $depth = 40 }
    $ms = 2500
    if ($null -ne $payload.max_ms) { $ms = [int]$payload.max_ms }
    if ($ms -lt 200) { $ms = 200 }
    if ($ms -gt 20000) { $ms = 20000 }
    return @{ nodes = $nodes; depth = $depth; ms = $ms }
}

function Find-TreeMatch($tree, $needle) {
    # 按名字 / 值 / 类型标签找子串。返回值本身用于告诉模型「等到的是哪个控件」。
    if ($null -eq $tree) { return $null }
    $lower = $needle.ToLowerInvariant()
    foreach ($item in $tree.items) {
        $haystack = ([string]$item.name + ' ' + [string]$item.value + ' ' +
                     [string]$item.label).ToLowerInvariant()
        if ($haystack.Contains($lower)) { return $item }
    }
    return $null
}

function Wait-ForControl($payload, $state, $budget, $deadline) {
    # 前台窗口里等一个控件出现/消失。每次轮询都重新遍历一次，因为「出现」
    # 本身就是界面变化——拿旧的树判断等于没等。
    $hwnd = [IntPtr][Convert]::ToInt64([string]$state.window_id, 16)
    $wantAbsent = ([string]$payload.state -eq 'absent')
    $polls = 0
    $last = $null
    $lastMatch = $null
    $started = [DateTime]::UtcNow
    while ($true) {
        $polls++
        $last = Get-UiTree $hwnd $budget.nodes $budget.depth $budget.ms
        $lastMatch = Find-TreeMatch $last ([string]$payload.text)
        $hit = ($null -ne $lastMatch)
        if ($wantAbsent) { $hit = (-not $hit) }
        if ($hit) {
            return [ordered]@{
                matched = $true
                polls = $polls
                elapsed_ms = [int](([DateTime]::UtcNow - $started).TotalMilliseconds)
                element = $lastMatch
                fingerprint = $last.fingerprint
                elements = $last.items.Count
                tree_truncated = $last.truncated
            }
        }
        if ([DateTime]::UtcNow -ge $deadline) { break }
        Start-Sleep -Milliseconds 120
    }
    return [ordered]@{
        matched = $false
        polls = $polls
        elapsed_ms = [int](([DateTime]::UtcNow - $started).TotalMilliseconds)
        element = $null
        fingerprint = $(if ($null -ne $last) { $last.fingerprint } else { '' })
        elements = $(if ($null -ne $last) { $last.items.Count } else { 0 })
        tree_truncated = $(if ($null -ne $last) { $last.truncated } else { $false })
        last_match = $lastMatch
    }
}

function Wait-ForWindow($payload, $deadline, $foregroundOnly) {
    # 顶层窗口标题等待。窗口列表一次 30-50ms，所以可以 100ms 一轮密集轮询。
    $wantAbsent = ([string]$payload.state -eq 'absent')
    $needle = ([string]$payload.text).ToLowerInvariant()
    $wantPid = 0
    if ($null -ne $payload.expected_pid) { $wantPid = [int]$payload.expected_pid }
    $polls = 0
    $lastHit = $null
    $lastCount = 0
    $started = [DateTime]::UtcNow
    while ($true) {
        $polls++
        $windows = @(Get-VisibleWindows)
        if ($foregroundOnly) {
            $windows = @($windows | Where-Object { $_.foreground })
        }
        $lastCount = $windows.Count
        $lastHit = $null
        foreach ($window in $windows) {
            if ($wantPid -gt 0 -and [int]$window.pid -ne $wantPid) { continue }
            # 先取成字符串再调方法，不要写成 if ([string]$x.Contains(...))：
            # [string]$false 得到的是 'False'，而非空字符串在 PowerShell 里是
            # **真值**，于是每个窗口都会被当成命中（实测踩过：等一个不存在的
            # 标题会立刻「等到」）。
            $title = [string]$window.title
            if ($title.ToLowerInvariant().Contains($needle)) {
                $lastHit = $window
                break
            }
        }
        $hit = ($null -ne $lastHit)
        if ($wantAbsent) { $hit = (-not $hit) }
        if ($hit) {
            return [ordered]@{
                matched = $true
                polls = $polls
                elapsed_ms = [int](([DateTime]::UtcNow - $started).TotalMilliseconds)
                window = $lastHit
                windows = $lastCount
            }
        }
        if ([DateTime]::UtcNow -ge $deadline) { break }
        Start-Sleep -Milliseconds 100
    }
    return [ordered]@{
        matched = $false
        polls = $polls
        elapsed_ms = [int](([DateTime]::UtcNow - $started).TotalMilliseconds)
        window = $lastHit
        windows = $lastCount
    }
}

function Test-ExcludedProcess($state, $list) {
    # 病灶进程名单：某些应用的无障碍提供程序会让遍历卡住几秒甚至挂死，
    # 与其等超时，不如在对它发起任何 UIA 调用之前就跳过。
    if ($null -eq $list) { return $false }
    $procName = ([string]$state.process).Trim().ToLowerInvariant()
    if (-not $procName) { return $false }
    foreach ($entry in @($list)) {
        $candidate = ([string]$entry).Trim().ToLowerInvariant()
        if ($candidate -and $candidate -eq $procName) { return $true }
    }
    return $false
}

function Get-LivePattern($el, $pattern) {
    try { return $el.GetCurrentPattern($pattern) } catch { return $null }
}

function Invoke-ElementOperation($el, $node, $operation, $text) {
    # 返回 $null 表示成功，否则返回可读的拒绝理由（{code,message}）。
    if ($operation -eq 'setvalue') {
        if ([bool]$node.password) {
            $why = 'refusing to write into a password field; that must be typed by the user'
            return [ordered]@{ code = 'password_field'; message = $why }
        }
        $pattern = Get-LivePattern $el ([System.Windows.Automation.ValuePattern]::Pattern)
        if ($null -eq $pattern) {
            return [ordered]@{ code = 'operation_unsupported';
                message = 'that control does not accept a value' }
        }
        $readOnly = $false
        try { $readOnly = [bool]$pattern.Current.IsReadOnly } catch { }
        if ($readOnly) {
            return [ordered]@{ code = 'operation_unsupported';
                message = 'that control is read-only' }
        }
        try { $pattern.SetValue([string]$text) } catch {
            return [ordered]@{ code = 'operation_failed'; message = $_.Exception.Message }
        }
        return $null
    }
    if ($operation -eq 'invoke') {
        $pattern = Get-LivePattern $el ([System.Windows.Automation.InvokePattern]::Pattern)
        if ($null -eq $pattern) {
            return [ordered]@{ code = 'operation_unsupported';
                message = 'that control is not clickable; click it by coordinates instead' }
        }
        try { $pattern.Invoke() } catch {
            return [ordered]@{ code = 'operation_failed'; message = $_.Exception.Message }
        }
        return $null
    }
    if ($operation -eq 'toggle') {
        $pattern = Get-LivePattern $el ([System.Windows.Automation.TogglePattern]::Pattern)
        if ($null -eq $pattern) {
            return [ordered]@{ code = 'operation_unsupported';
                message = 'that control cannot be toggled' }
        }
        try { $pattern.Toggle() } catch {
            return [ordered]@{ code = 'operation_failed'; message = $_.Exception.Message }
        }
        return $null
    }
    if ($operation -eq 'select') {
        $pattern = Get-LivePattern $el ([System.Windows.Automation.SelectionItemPattern]::Pattern)
        if ($null -eq $pattern) {
            return [ordered]@{ code = 'operation_unsupported';
                message = 'that control cannot be selected' }
        }
        try { $pattern.Select() } catch {
            return [ordered]@{ code = 'operation_failed'; message = $_.Exception.Message }
        }
        return $null
    }
    if ($operation -eq 'expand' -or $operation -eq 'collapse') {
        $pattern = Get-LivePattern $el ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
        if ($null -eq $pattern) {
            return [ordered]@{ code = 'operation_unsupported';
                message = 'that control has nothing to expand' }
        }
        try {
            if ($operation -eq 'expand') { $pattern.Expand() } else { $pattern.Collapse() }
        } catch {
            return [ordered]@{ code = 'operation_failed'; message = $_.Exception.Message }
        }
        return $null
    }
    if ($operation -eq 'focus') {
        try { $el.SetFocus() } catch {
            return [ordered]@{ code = 'operation_failed'; message = $_.Exception.Message }
        }
        return $null
    }
    $why = 'operation must be invoke, setvalue, toggle, select, expand, collapse, or focus'
    return [ordered]@{ code = 'invalid_operation'; message = $why }
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
    $procNames = Get-ProcNames
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
            if ($procNames.ContainsKey([int]$procId)) { $process = [string]$procNames[[int]$procId] }
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
    if ($payload.action -eq 'displays') {
        # 只读：每块屏的物理矩形 / 工作区 / DPI，外加前台窗口落在哪块上。
        $all = @(Get-Displays)
        $vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
        $foreground = Get-WindowState
        $index = 0
        $dpi = 0
        if ($foreground.available) {
            $index = Get-MonitorIndexForRect $all $foreground.rect
            try {
                $target = [IntPtr][Convert]::ToInt64([string]$foreground.window_id, 16)
                $dpi = [int][HarnessDesktopNative]::GetDpiForWindow($target)
            } catch { }
        }
        return [ordered]@{
            ok = $true
            action = 'displays'
            displays = @($all)
            virtual_screen = @($vs.X, $vs.Y, $vs.Width, $vs.Height)
            foreground_index = $index
            foreground_dpi = $dpi
            foreground = $foreground
        }
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
    if ($payload.action -eq 'wait') {
        # 只读：等一个条件成立。轮询在 PowerShell 侧完成，一次工具调用顶
        # 之前「sleep + 重新快照」的好几轮往返。
        $timeoutMs = [int]$payload.timeout_ms
        if ($timeoutMs -le 0) { $timeoutMs = 5000 }
        $deadline = [DateTime]::UtcNow.AddMilliseconds($timeoutMs)
        $target = [string]$payload.target
        if ($target -eq 'window' -or $target -eq 'foreground') {
            $result = Wait-ForWindow $payload $deadline ($target -eq 'foreground')
            $result.action = 'wait'
            $result.target = $target
            $result.state = Get-WindowState
            return [ordered]@{ ok = $true; result = $result }
        }
        if (-not $state.available) {
            return New-Failure 'no_foreground_window' 'no foreground window' $state
        }
        if ($null -ne $payload.expected_pid -and
            ([int]$state.pid -ne [int]$payload.expected_pid -or
             [string]$state.window_id -ne [string]$payload.expected_window_id)) {
            Fail 'foreground_changed' 'foreground window changed before waiting' $state
        }
        if (Test-ExcludedProcess $state $payload.exclude) {
            $why = "reading the accessibility tree of $($state.process) is disabled "
            $why += '(HARNESS_DESKTOP_EXCLUDE_PROCESSES) because it can hang'
            return New-Failure 'process_excluded' $why $state
        }
        # 轮询用的预算比一次性遍历小：等待期间要跑好几轮，宁可每轮少看几个控件。
        $budget = Get-TreeBudget $payload
        if ($budget.nodes -gt 80) { $budget.nodes = 80 }
        if ($budget.ms -gt 900) { $budget.ms = 900 }
        $result = Wait-ForControl $payload $state $budget $deadline
        $result.action = 'wait'
        $result.target = 'control'
        $result.state = Get-WindowState
        return [ordered]@{ ok = $true; result = $result }
    }
    if ($payload.action -eq 'inspect') {
        # 只读：给模型一份裁剪过的控件清单。敏感窗口连结构都拒绝——「支付密码」
        # 「验证码」这些字段名本身也是隐私，不是只有值才算。
        if (-not $state.available) {
            return New-Failure 'no_foreground_window' 'no foreground window' $state
        }
        if ($state.sensitive_target) {
            $why = 'this window looks like a password, payment, or verification '
            $why += 'screen, so its structure is withheld; ask the user to handle it'
            return New-Failure 'sensitive_target' $why $state
        }
        if (Test-ExcludedProcess $state $payload.exclude) {
            $why = "reading the accessibility tree of $($state.process) is disabled "
            $why += '(HARNESS_DESKTOP_EXCLUDE_PROCESSES) because it can hang'
            return New-Failure 'process_excluded' $why $state
        }
        $budget = Get-TreeBudget $payload
        $target = [IntPtr][Convert]::ToInt64([string]$state.window_id, 16)
        $tree = Get-UiTree $target $budget.nodes $budget.depth $budget.ms
        if ($null -eq $tree) {
            return New-Failure 'tree_unavailable' 'could not read the UI tree of that window' $state
        }
        return [ordered]@{
            ok = $true
            action = 'inspect'
            state = $state
            items = @($tree.items)
            total = $tree.total
            truncated = $tree.truncated
            fingerprint = $tree.fingerprint
            duration_ms = $tree.duration_ms
        }
    }
    if (-not $state.available) { Fail 'no_foreground_window' 'no foreground window' $state }
    if ([int]$state.pid -ne [int]$payload.expected_pid -or
        [string]$state.window_id -ne [string]$payload.expected_window_id) {
        Fail 'foreground_changed' 'foreground window changed since the snapshot; take a new desktop_snapshot' $state
    }
    if ($state.sensitive_target) {
        Fail 'sensitive_target' 'desktop input is blocked on a password or sensitive window' $state
    }
    # 终端窗口：只有 type/key（会变成命令）需要拦；move/click/scroll 和纯 wait
    # 序列不受影响——点一下终端去选中文字是正常需求。
    $inputAction = ($payload.action -in @('type', 'key'))
    if ($payload.action -eq 'element' -and [string]$payload.operation -eq 'setvalue') {
        $inputAction = $true
    }
    if ($payload.action -eq 'sequence') {
        foreach ($st in @($payload.steps)) {
            if ([string]$st.action -in @('type', 'key')) { $inputAction = $true; break }
        }
    }
    if ($state.is_terminal -and $inputAction -and -not [bool]$payload.allow_terminal) {
        $why = 'typing into a terminal window would run commands; re-send with '
        $why += 'allow_terminal=true after confirming the window'
        Fail 'terminal_input' $why $state
    }
    if ($payload.action -eq 'element') {
        # 元素级动作：按编号找控件，走 UIA 的 Invoke/SetValue 等，不移动鼠标。
        # 好处是遮挡、DPI、动画时序都不影响，而且能被读回验证。
        $budget = Get-TreeBudget $payload
        $target = [IntPtr][Convert]::ToInt64([string]$state.window_id, 16)
        $tree = Get-UiTree $target $budget.nodes $budget.depth $budget.ms
        if ($null -eq $tree) {
            Fail 'tree_unavailable' 'could not read the UI tree of that window' $state
        }
        if ([string]$tree.fingerprint -ne [string]$payload.tree) {
            $why = 'the window changed since that inspection; call desktop_inspect '
            $why += 'again and use the new numbers'
            Fail 'tree_changed' $why $state
        }
        $index = [int]$payload.element - 1
        if ($index -lt 0 -or $index -ge $tree.items.Count) {
            Fail 'element_missing' "that window now lists $($tree.items.Count) controls" $state
        }
        $node = $tree.items[$index]
        $el = $tree.elements[$index]
        # 第四档：控件名命中「改密码 / 验证码 / 支付 / 提权」就直接拒，
        # 不进审批队列——排队等来的「是」在这里同样是错的。
        if ($null -ne $payload.forbidden) {
            foreach ($pattern in @($payload.forbidden)) {
                $expr = [string]$pattern
                if (-not $expr) { continue }
                $hit = $false
                try { $hit = [bool]($node.name -match $expr) } catch { $hit = $false }
                if ($hit) {
                    $why = 'that control belongs to a task only the user can do '
                    $why += '(password, verification, payment, or permissions); '
                    $why += 'tell them what to click and stop'
                    Fail 'must_do_self' $why $state
                }
            }
        }
        $problem = Invoke-ElementOperation $el $node ([string]$payload.operation) ([string]$payload.text)
        if ($null -ne $problem) { Fail $problem.code $problem.message $state }
        Start-Sleep -Milliseconds 40
        $after = Get-WindowState
        return [ordered]@{
            ok = $true
            action = 'element'
            operation = [string]$payload.operation
            element = [int]$payload.element
            target = $node
            steps_done = @()
            state_before = $state
            state_after = $after
        }
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
    import ps_worker

    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    # 脚本本身走标准输入：桌面能力的脚本超过 Windows 的 32767 字符命令行上限
    # （实测 WinError 206），而且 stdin 顺带免掉了引号与编码两道坑。
    payload = base64.b64encode(script.encode('utf-8')).decode('ascii') + chr(10)
    return subprocess.run(
        [executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-WindowStyle', 'Hidden', '-Command', ps_worker.BOOTSTRAP],
        input=payload, capture_output=True, text=True, encoding='utf-8',
        timeout=timeout, env={**os.environ, **env}, creationflags=flags,
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
        is_terminal=bool(value.get('is_terminal')),
        dpi=int(value.get('dpi') or 0),
        error=str(value.get('error') or ''),
    )


def tree_limits(environ=None) -> dict:
    """界面结构的预算（节点数 / 深度 / 毫秒）。越界收敛到边界。"""
    import settings

    return {
        'max_nodes': int(settings.number(
            'HARNESS_DESKTOP_TREE_NODES', DEFAULT_TREE_NODES,
            minimum=5, maximum=MAX_ELEMENT_INDEX, integer=True, environ=environ)),
        'max_depth': int(settings.number(
            'HARNESS_DESKTOP_TREE_DEPTH', DEFAULT_TREE_DEPTH,
            minimum=2, maximum=40, integer=True, environ=environ)),
        'max_ms': int(settings.number(
            'HARNESS_DESKTOP_TREE_MS', DEFAULT_TREE_MS,
            minimum=200, maximum=20000, integer=True, environ=environ)),
    }


def excluded_processes(environ=None) -> list[str]:
    """读无障碍树时直接跳过的进程名（病灶 provider 会让遍历卡住）。"""
    import settings

    return settings.csv('HARNESS_DESKTOP_EXCLUDE_PROCESSES', environ=environ)


def _element_info(value, index: int) -> ElementInfo:
    value = value if isinstance(value, dict) else {}
    bounds = value.get('bounds')
    try:
        bounds = tuple(int(part) for part in bounds) if bounds else None
    except (TypeError, ValueError):
        bounds = None
    operations = value.get('operations')
    operations = (tuple(str(item) for item in operations)
                  if isinstance(operations, (list, tuple)) else ())
    try:
        element_id = int(value.get('id') or index)
    except (TypeError, ValueError):
        element_id = index
    return ElementInfo(
        id=element_id,
        label=str(value.get('label') or '控件'),
        name=str(value.get('name') or ''),
        value=str(value.get('value') or ''),
        password=bool(value.get('password')),
        enabled=bool(value.get('enabled', True)),
        focusable=bool(value.get('focusable')),
        focused=bool(value.get('focused')),
        bounds=bounds,
        operations=operations,
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


def _validated_identity(expected_pid, expected_window_id) -> tuple[int, str]:
    """pid + 窗口句柄双因子。

    只要动作会改变某个窗口的状态，就必须同时说清是哪个进程的哪个窗口：
    单靠 pid 会在多窗口应用里打错窗口，单靠标题对同标题窗口完全无效。
    """
    try:
        pid = int(expected_pid)
    except (TypeError, ValueError) as exc:
        raise DesktopControlError(
            'expected_pid is required; get it from desktop_snapshot') from exc
    if pid <= 0:
        raise DesktopControlError('expected_pid must be a positive process id')
    if not isinstance(expected_window_id, str) or not expected_window_id.strip():
        raise DesktopControlError(
            'expected_window_id is required; get it from desktop_snapshot')
    window_id = expected_window_id.strip().upper()
    if len(window_id) > 32 or any(ch not in '0123456789ABCDEF' for ch in window_id):
        raise DesktopControlError(
            'expected_window_id must be the hexadecimal id from desktop_snapshot')
    return pid, window_id


def validate_element_action(operation, element, tree, *,
                            expected_pid=None, expected_window_id=None,
                            text=None) -> dict:
    """校验元素级动作的参数。

    ``tree`` 是 ``desktop_inspect`` 给的结构指纹，**必填**：界面在「看一眼」
    和「点一下」之间变了，旧编号会指向另一个控件，宁可让模型重新看一眼。
    """
    pid, window_id = _validated_identity(expected_pid, expected_window_id)
    op = str(operation or '').strip().casefold() or 'invoke'
    if op not in ELEMENT_OPERATIONS:
        raise DesktopControlError(
            'operation must be invoke, setvalue, toggle, select, expand, '
            'collapse, or focus')
    try:
        index = int(element)
    except (TypeError, ValueError) as exc:
        raise DesktopControlError(
            'element must be the number from desktop_inspect') from exc
    if index < 1 or index > MAX_ELEMENT_INDEX:
        raise DesktopControlError('element must be between 1 and '
                                  f'{MAX_ELEMENT_INDEX}')
    fingerprint = str(tree or '').strip().lower()
    if len(fingerprint) < 8 or len(fingerprint) > 64 or any(
            ch not in '0123456789abcdef' for ch in fingerprint):
        raise DesktopControlError(
            'tree must be the fingerprint desktop_inspect returned')
    payload = {'action': 'element', 'expected_pid': pid,
               'expected_window_id': window_id, 'tree': fingerprint,
               'element': index, 'operation': op}
    if op == 'setvalue':
        if not isinstance(text, str) or not text:
            raise DesktopControlError('setvalue requires text')
        if len(text) > MAX_TYPE_CHARS:
            raise DesktopControlError(
                f'setvalue text must be at most {MAX_TYPE_CHARS} characters')
        payload['text'] = text
    elif text is not None:
        raise DesktopControlError('text is only valid with operation=setvalue')
    return payload


def validate_action(action: str, *, expected_pid=None, expected_window_id=None,
                    x=None, y=None, text=None, key=None, amount=None,
                    button=None, count=None, steps=None,
                    allow_terminal=None) -> dict:
    action = str(action or '').strip().casefold()
    expected_pid, expected_window_id = _validated_identity(
        expected_pid, expected_window_id)
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
    # 显式放行终端输入必须由调用方逐次声明，不能隐式带上。
    terminal_ok = bool(allow_terminal)
    if action != 'click' and (button is not None or count is not None):
        raise DesktopControlError('button and count are only valid with click')
    if action == 'move':
        try:
            coords = {'x': int(x), 'y': int(y)}
        except (TypeError, ValueError) as exc:
            raise DesktopControlError('move requires integer x and y') from exc
        return {'action': action, 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id,
                'allow_terminal': terminal_ok, **coords}
    if action == 'click':
        return {'expected_pid': expected_pid,
                'expected_window_id': expected_window_id,
                **_validated_click(x, y, button, count)}
    if action == 'type':
        if not isinstance(text, str) or len(text) > MAX_TYPE_CHARS:
            raise DesktopControlError(f'type text must be a string of at most {MAX_TYPE_CHARS} characters')
        return {'action': action, 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id,
                'allow_terminal': terminal_ok, 'text': text}
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
            'expected_window_id': expected_window_id,
            'allow_terminal': terminal_ok, 'amount': amount}


def action(action: str, *, expected_pid=None, expected_window_id=None, x=None,
           y=None, text=None, key=None, amount=None, button=None, count=None,
           steps=None, allow_terminal=None, runner=None,
           timeout: float = DEFAULT_TIMEOUT) -> ActionResult:
    payload = validate_action(action, expected_pid=expected_pid,
                              expected_window_id=expected_window_id, x=x, y=y,
                              text=text, key=key, amount=amount,
                              button=button, count=count, steps=steps,
                              allow_terminal=allow_terminal)
    if payload['action'] == 'sequence':
        wait_s = sum(step.get('ms', 0) for step in payload['steps']) / 1000.0
        timeout = min(60.0, max(
            timeout, DEFAULT_TIMEOUT + 2.0 * len(payload['steps']) + wait_s))
    started = time.monotonic()
    record = _run(payload, runner=runner, timeout=timeout)
    step_records = tuple(item for item in (record.get('steps') or [])
                         if isinstance(item, dict))
    before = record.get('state_before')
    return ActionResult(
        action=payload['action'], state=_state(record.get('state_after')),
        steps=step_records,
        duration_ms=int((time.monotonic() - started) * 1000),
        before=_state(before) if isinstance(before, dict) else None,
    )


def inspect(*, runner=None, timeout: float | None = None) -> UiTree:
    """读取前台窗口的裁剪控件清单（只读，不改变任何状态）。"""
    limits = tree_limits()
    payload = {'action': 'inspect', **limits}
    excluded = excluded_processes()
    if excluded:
        payload['exclude'] = excluded
    if timeout is None:
        # 遍历自带毫秒预算，外层超时要比它宽，否则先被 Python 掐断、
        # 拿不到「已采集部分」这个有意义的结果。
        timeout = max(DEFAULT_TIMEOUT, limits['max_ms'] / 1000.0 + 5.0)
    reply = _run(payload, runner=runner, timeout=timeout)
    raw_items = reply.get('items')
    items = tuple(_element_info(item, index)
                  for index, item in enumerate(raw_items or (), 1)
                  if isinstance(item, dict))
    return UiTree(
        items=items,
        truncated=bool(reply.get('truncated')),
        fingerprint=str(reply.get('fingerprint') or ''),
        state=_state(reply.get('state')),
        duration_ms=int(reply.get('duration_ms') or 0),
    )


def element_action(operation, element, tree, *, expected_pid=None,
                   expected_window_id=None, text=None, forbidden=None,
                   runner=None, timeout: float = DEFAULT_TIMEOUT) -> ActionResult:
    """按编号操作控件（Invoke/SetValue/...），不移动鼠标。

    ``forbidden`` 是控件名模式表（由策略层给，例如第四档 MUST_DO_SELF）：
    命中就拒绝，且不进审批——这类事只能用户自己点。
    """
    payload = validate_element_action(
        operation, element, tree, expected_pid=expected_pid,
        expected_window_id=expected_window_id, text=text)
    if forbidden:
        payload['forbidden'] = [str(item)[:200] for item in forbidden][:20]
    started = time.monotonic()
    reply = _run(payload, runner=runner, timeout=timeout)
    target = reply.get('target')
    return ActionResult(
        action='element',
        state=_state(reply.get('state_after')),
        duration_ms=int((time.monotonic() - started) * 1000),
        target=_element_info(target, 0) if isinstance(target, dict) else None,
    )


def validate_wait(target='control', text=None, state='present', timeout_ms=None,
                  *, expected_pid=None, expected_window_id=None) -> dict:
    """校验等待条件。没有任何条件可等的「等」会白烧时间，直接拒绝。"""
    kind = str(target or 'control').strip().casefold()
    if kind not in WAIT_TARGETS:
        raise DesktopControlError(
            "target must be 'control', 'window', or 'foreground'")
    desired = str(state or 'present').strip().casefold()
    if desired not in WAIT_STATES:
        raise DesktopControlError("state must be 'present' or 'absent'")
    needle = ' '.join(str(text or '').split())
    if not needle:
        raise DesktopControlError('text is required: say what to wait for')
    if len(needle) > MAX_WAIT_TEXT:
        raise DesktopControlError(
            f'text must be at most {MAX_WAIT_TEXT} characters')
    try:
        wait_ms = int(timeout_ms if timeout_ms is not None
                      else TOOL_WAIT_DEFAULT_MS)
    except (TypeError, ValueError) as exc:
        raise DesktopControlError('timeout_ms must be an integer') from exc
    if wait_ms < TOOL_WAIT_MIN_MS or wait_ms > TOOL_WAIT_MAX_MS:
        raise DesktopControlError(
            f'timeout_ms must be between {TOOL_WAIT_MIN_MS} and {TOOL_WAIT_MAX_MS}')
    payload = {'action': 'wait', 'target': kind, 'state': desired,
               'text': needle, 'timeout_ms': wait_ms, **tree_limits()}
    if expected_pid is not None or expected_window_id is not None:
        pid, window_id = _validated_identity(expected_pid, expected_window_id)
        payload['expected_pid'] = pid
        payload['expected_window_id'] = window_id
    if kind == 'control':
        excluded = excluded_processes()
        if excluded:
            payload['exclude'] = excluded
    return payload


def wait(target='control', text=None, state='present', timeout_ms=None, *,
         expected_pid=None, expected_window_id=None, runner=None,
         timeout: float | None = None) -> WaitResult:
    """轮询等待一个条件成立（控件出现/消失、窗口出现/消失）。

    轮询在 PowerShell 侧完成：一次工具调用顶之前「sleep + 重新快照」的好几轮
    往返，而且每轮的证据都是同一套裁剪逻辑产出的，判据和 desktop_inspect 一致。
    """
    payload = validate_wait(target, text, state, timeout_ms,
                            expected_pid=expected_pid,
                            expected_window_id=expected_window_id)
    if timeout is None:
        timeout = payload['timeout_ms'] / 1000.0 + 8.0
    reply = _run(payload, runner=runner, timeout=timeout)
    result = reply.get('result')
    result = result if isinstance(result, dict) else {}
    element = result.get('element')
    window = result.get('window')
    return WaitResult(
        matched=bool(result.get('matched')),
        target=str(result.get('target') or payload['target']),
        desired=payload['state'],
        elapsed_ms=int(result.get('elapsed_ms') or 0),
        polls=int(result.get('polls') or 0),
        state=_state(result.get('state')),
        element=_element_info(element, 0) if isinstance(element, dict) else None,
        window=window if isinstance(window, dict) else None,
        fingerprint=str(result.get('fingerprint') or ''),
        elements=int(result.get('elements') or 0),
        windows=int(result.get('windows') or 0),
        truncated=bool(result.get('tree_truncated')),
    )


@dataclass(frozen=True)
class DisplayInfo:
    """一块显示器的物理矩形与 DPI。"""

    name: str
    primary: bool
    bounds: tuple[int, int, int, int]
    work: tuple[int, int, int, int]
    dpi_x: int = 96
    dpi_y: int = 96

    @property
    def scale(self) -> float:
        return round(self.dpi_x / 96.0, 2)


@dataclass(frozen=True)
class DisplaysResult:
    displays: tuple[DisplayInfo, ...]
    virtual_screen: tuple[int, int, int, int] | None
    foreground_index: int
    foreground_dpi: int
    state: WindowState


def _rect(value) -> tuple[int, int, int, int] | None:
    try:
        parts = tuple(int(part) for part in value)
    except (TypeError, ValueError):
        return None
    return parts if len(parts) == 4 else None


def displays(*, runner=None, timeout: float = DEFAULT_TIMEOUT) -> DisplaysResult:
    """每块显示器的物理矩形 / 工作区 / DPI，以及前台窗口落在哪块上。

    进程是 DPI-aware 的，所以这里的矩形是**物理像素**，与截图和
    ``desktop_action`` 同一套坐标；不带 DPI 感知的进程看到的会是逻辑像素
    （150% 缩放下差 1.5 倍），那正是点偏位置最常见的成因。
    """
    reply = _run({'action': 'displays'}, runner=runner, timeout=timeout)
    items = []
    for entry in reply.get('displays') or []:
        if not isinstance(entry, dict):
            continue
        bounds = _rect(entry.get('bounds'))
        work = _rect(entry.get('work')) or bounds
        if bounds is None:
            continue
        items.append(DisplayInfo(
            name=str(entry.get('name') or f'DISPLAY{len(items) + 1}'),
            primary=bool(entry.get('primary')),
            bounds=bounds, work=work,
            dpi_x=int(entry.get('dpi_x') or 96),
            dpi_y=int(entry.get('dpi_y') or 96),
        ))
    return DisplaysResult(
        displays=tuple(items),
        virtual_screen=_rect(reply.get('virtual_screen')),
        foreground_index=int(reply.get('foreground_index') or 0),
        foreground_dpi=int(reply.get('foreground_dpi') or 0),
        state=_state(reply.get('foreground')),
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
        'is_terminal': state.is_terminal,
        'dpi': state.dpi,
        **({'error': state.error} if state.error else {}),
    }
