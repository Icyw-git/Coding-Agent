"""桌面控制的 PowerShell 源码（纯文本，没有 Python 逻辑）。

为什么单独一个模块
------------------
``desktop_control_windows`` 原来 2560 行，其中 **1622 行是这里的 PowerShell 文本**
（装配、动作分发、控件树遍历、独立进程入口）。Python 那半边只有 939 行。
文本和逻辑混在一个文件里，改 Python 时要在 1600 行 PS 里翻，反之亦然。

拆出来之后这个模块只被三处使用：``desktop_control_windows.worker_script()``、
独立进程模式的 ``CONTROL_SCRIPT``、以及 ``scripts/profile_desktop.py`` 的探测。
改 PS 只动这里，改逻辑只动那边。

约定
----
- 三个常量按 worker 的拼接顺序排列：``CONTROL_SETUP``（装配，代价只付一次）
  → ``CONTROL_BODY``（动作分发）→ ``CONTROL_MAIN``（独立进程入口）。
- 文本里出现的中文是**给用户看的文案**，必须 GBK 可编码；符号只用 ``√ × ·``。
- 改完必须让 PowerShell 解析一遍（``ParseFile``），语法错不会在 import 时暴露。
"""
from __future__ import annotations

# worker 模式下只执行一次的装配部分（Add-Type 编译在这里，代价只付一次）。
CONTROL_SETUP = r'''
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
    # 这几个不常见但确实会出现（滚动条滑块、提示气泡、标题栏…）。
    # 没登记的类型会原样输出英文程序名，模型读到 `Thumb` 不知道是什么。
    'Thumb'='滚动滑块'; 'ScrollBar'='滚动条'; 'Separator'='分隔条'
    'ToolTip'='提示'; 'TitleBar'='标题栏'; 'AppBar'='应用栏'
    'Paragraph'='段落'; 'Sound'='声音'; 'SemanticZoom'='缩放视图'
}
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
# 单个父节点贡献的子项上限。列表类控件一屏几十行，不设限会把整个预算吃光。
$script:TREE_CHILDREN_PER_PARENT = 14
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
        [System.Windows.Automation.ExpandCollapsePattern]::Pattern,
        # ScrollPattern 必须在这里：漏了的话 Get-CachedPattern 恒为空，
        # 于是「可滚动」永远不显示，而动作侧用 live 探测又能滚——看得见用不上的反面。
        [System.Windows.Automation.ScrollPattern]::Pattern
    )) { $cache.Add($pat) }
    # 默认 TreeScope=Element：只缓存这一个元素的属性，不牵连子树。
    return $cache
}

function Get-CachedNode($el, $cache) {
    # 兜底路径。主路径是「激活 CacheRequest + FindAll(Children)」——FindAll 返回的
    # 子节点自带缓存值，逐节点读 .Cached 即可（A/B 实测属性阶段 211ms -> 117ms，
    # WindowsTerminal 51ms -> 11ms）。
    # 主路径是「激活 CacheRequest + FindAll」：返回的子节点自带缓存值。
    # 个别 provider 下 .Cached 会是空，这时才补一次 GetUpdatedCache
    # （一次跨进程调用换回全部属性）。
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
        # 只有空格的名字在输出里看着是空的，但 `-ne ''` 会把它当"有名字"放进来。
        $name = ([string]$c.Name).Trim()
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
    # 状态：模型要靠它判断"勾没勾""开没开""选中了哪个"，否则每次都得靠截图。
    $stateFlags = New-Object System.Collections.ArrayList
    $toggle = Get-CachedPattern $cached ([System.Windows.Automation.TogglePattern]::Pattern)
    if ($null -ne $toggle) {
        $patterns.Add('toggle') | Out-Null
        $toggleState = ''
        try { $toggleState = [string]$toggle.Current.ToggleState } catch { }
        if ($toggleState -eq 'On') { $stateFlags.Add('checked') | Out-Null }
        elseif ($toggleState -eq 'Off') { $stateFlags.Add('unchecked') | Out-Null }
        elseif ($toggleState) { $stateFlags.Add('indeterminate') | Out-Null }
    }
    $expand = Get-CachedPattern $cached ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
    if ($null -ne $expand) {
        $state = ''
        try { $state = [string]$expand.Current.ExpandCollapseState } catch { }
        if ($state -eq 'Expanded') {
            $patterns.Add('collapse') | Out-Null
            $stateFlags.Add('expanded') | Out-Null
        } elseif ($state -eq 'PartiallyExpanded') {
            $patterns.Add('collapse') | Out-Null
            $stateFlags.Add('partially_expanded') | Out-Null
        } else {
            $patterns.Add('expand') | Out-Null
            if ($state -eq 'Collapsed') { $stateFlags.Add('collapsed') | Out-Null }
        }
    }
    $selection = Get-CachedPattern $cached ([System.Windows.Automation.SelectionItemPattern]::Pattern)
    if ($null -ne $selection) {
        $patterns.Add('select') | Out-Null
        try {
            if ([bool]$selection.Current.IsSelected) { $stateFlags.Add('selected') | Out-Null }
        } catch { }
    }
    # 可滚动：长列表里"滚这个列表"和"滚整个窗口"是两件事。
    if ($null -ne (Get-CachedPattern $cached ([System.Windows.Automation.ScrollPattern]::Pattern))) {
        $patterns.Add('scroll') | Out-Null
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
            if ($readOnly) { $stateFlags.Add('readonly') | Out-Null }
            else { $patterns.Add('setvalue') | Out-Null }
        }
    }

    $leaf = $script:TREE_LEAVES -contains $type
    # 一行值得列出来，是因为模型能对它做点什么：有元素级操作，或者有坐标可以点。
    # 两者都没有的行是纯噪声——实测出现过「#5 面板 · 只能按坐标点」这种既没坐标
    # 也没操作的条目，"只能按坐标点"纯属空话。
    if ($patterns.Count -eq 0 -and $bounds.Count -ne 4) {
        return @{ info = $null; descend = $true; doc = ($type -eq 'Document') }
    }
    # 光有坐标还不够：无名、无操作、不可聚焦的容器（实测「自绘控件」占满整屏）
    # 列出来只是占位。要有名字/值/操作/可聚焦之一才值得占一行。
    $interesting = ($name -ne '') -or ($value -ne '') -or
                   ($patterns.Count -gt 0) -or $focusable
    $isDoc = ($type -eq 'Document')
    if (-not $interesting) { return @{ info = $null; descend = (-not $leaf); doc = $isDoc } }

    # 坐标离谱的节点通常是虚拟化容器的占位（实测见过 top=-470309、
    # 高 55340 的"聊天记录"容器）。给了模型也没法点，还占掉一个名额。
    if ($bounds.Count -eq 4 -and (
            [Math]::Abs([int]$bounds[0]) -gt 20000 -or
            [Math]::Abs([int]$bounds[1]) -gt 20000 -or
            [int]$bounds[2] -gt 20000 -or [int]$bounds[3] -gt 20000)) {
        $bounds = @()
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
        state = $stateFlags.ToArray()
    }
    return @{ info = $info; descend = (-not $leaf); doc = $isDoc }
}

function Get-TreeFingerprint($items) {
    # 结构指纹：只覆盖「编号 -> 类型/名字」这条对应关系。动作前重算一遍，
    # 不一致就拒绝——界面变了还按旧编号点，点中的是另一个控件。
    #
    # 刻意**不包含 bounds**：元素动作走 UIA 按编号定位，坐标对它没有意义，
    # 而把坐标算进哈希会在窗口移动、窗口缩放、列表滚动、状态栏文字变化时
    # 让所有编号一起失效——那些情况下控件根本没换，只是位置动了。
    # 也不包含 value / enabled：输入框内容变化、按钮从灰变亮都不该让编号失效。
    $sb = New-Object Text.StringBuilder
    foreach ($item in $items) {
        [void]$sb.Append($item.id).Append('|').Append($item.label).Append('|')
        [void]$sb.Append($item.name).Append([char]10)
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
    # 同一个东西常常被走两遍：ComboBox 展开后，弹出窗口的子树里有一份，
    # ComboBox 自己的 items 里还有一份，坐标完全相同、但只有一份带「可选中」。
    # 模型大概率挑第一个（没操作的那份），所以要按「类型|名字|坐标」合并。
    $seen = @{}
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
                $key = [string]$node.info.label + '|' + [string]$node.info.name + '|' +
                       ($node.info.bounds -join ',')
                if ($seen.ContainsKey($key)) {
                    # 合并到先出现的那一条：并操作、并状态，取焦点/可用性的或。
                    $slot = $seen[$key]
                    $existing = $items[$slot]
                    foreach ($op in @($node.info.operations)) {
                        if ($existing.operations -notcontains $op) { $existing.operations += $op }
                    }
                    foreach ($st in @($node.info.state)) {
                        if ($existing.state -notcontains $st) { $existing.state += $st }
                    }
                    if ($node.info.focused) { $existing.focused = $true }
                    if ($node.info.value -and -not $existing.value) { $existing.value = $node.info.value }
                    # **元素也要换**：列出来的能力是合并后的，执行时却用原来那个
                    # 没有 pattern 的元素，就会报"不能选中；它支持：select"这种自相
                    # 矛盾的话（实测 ComboBox 展开后就是这样）。能力更全的那个胜出。
                    if (@($node.info.operations).Count -gt @($existing.operations).Count -or
                        ($node.info.name -and -not $existing.name)) {
                        $elements[$slot] = $el
                    }
                } else {
                    $node.info.id = $items.Count + 1
                    $seen[$key] = $items.Count
                    $items.Add($node.info) | Out-Null
                    $elements.Add($el) | Out-Null
                }
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
            # FindAll 本身是**一次**跨进程调用，不受上面那个 deadline 约束：
            # 病理性 provider 能在这一个调用里卡十几秒（实测 PDF 文档的子树
            # 26154 个节点要 30 秒）。回来之后立刻检查一次，超预算就不再下探。
            if (([DateTime]::UtcNow - $started).TotalMilliseconds -gt $maxMs) {
                $truncated = $true
                continue
            }
            if ($null -eq $found -or $found.Count -eq 0) { continue }
            # 单个父节点最多贡献这么多子项。文件夹列表、邮件列表这类容器一屏
            # 几十行，每行又拆成「名称/日期/类型」三个单元格——不设限的话 120 个
            # 名额会被一张表格吃光，而「文件名」「打开」这些真正要点的控件排在后面
            # 根本进不来（实测文件对话框就是这样）。
            $count = $found.Count
            $pushed = 0
            for ($i = $count - 1; $i -ge 0; $i--) {
                if (($count - $i) -gt $script:TREE_CHILDREN_PER_PARENT) {
                    $truncated = $true
                    break
                }
                $stack.Push(@($found[$i], ($depth + 1), $underDoc))
                $pushed++
            }
        }
    } catch {
        $truncated = $true
    } finally {
        try { $activation.Dispose() } catch { }
    }
    # 只保留最深的那个「当前焦点」。
    # UIA 的 HasKeyboardFocus 在祖先链上也会为真（实测 WindowsTerminal 给
    # 窗口/面板/文本三行都打了标记），模型会以为窗口本身有焦点、不知道该往哪儿输入。
    # 前序遍历里祖先先出、后代后出，所以最后一个带标记的就是最深的那层。
    $lastFocused = -1
    for ($i = 0; $i -lt $items.Count; $i++) {
        if ($items[$i].focused) { $lastFocused = $i }
    }
    if ($lastFocused -ge 0) {
        for ($i = 0; $i -lt $items.Count; $i++) {
            if ($i -ne $lastFocused -and $items[$i].focused) { $items[$i].focused = $false }
        }
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
    # 按名字 / 值找子串，和元素动作的 expect 核对用同一套判据。
    # 刻意**不搜类型标签**：那样 `wait(text='按钮')` 会命中任意一个按钮，
    # 而 schema 里写的是「控件名或值」。两边口径不一致时，模型的猜测成本最高。
    if ($null -eq $tree) { return $null }
    $lower = $needle.ToLowerInvariant()
    foreach ($item in $tree.items) {
        $haystack = ([string]$item.name + ' ' + [string]$item.value).ToLowerInvariant()
        if ($haystack.Contains($lower)) { return $item }
    }
    return $null
}

function Wait-ForControl($payload, $state, $budget, $fullBudget, $deadline) {
    # 前台窗口里等一个控件出现/消失。每次轮询都重新遍历一次，因为「出现」
    # 本身就是界面变化——拿旧的树判断等于没等。
    #
    # 轮询用缩小预算（快），但**命中之后要用 $fullBudget 再走一遍**：指纹是
    # 「编号 -> 类型/名字/位置」的哈希，节点数不同哈希就不同。用缩小预算的指纹
    # 去调 desktop_action，必然撞 tree_changed（实测：83 个控件的窗口，
    # 120/2500 得 4b16b723…，80/900 得 7e9c15ec…）。
    $hwnd = [IntPtr][Convert]::ToInt64([string]$state.window_id, 16)
    $wantAbsent = ([string]$payload.state -eq 'absent')
    $polls = 0
    $last = $null
    $lastMatch = $null
    $started = [DateTime]::UtcNow
    # 自适应退避：前 1 秒 120ms 一轮（大多数界面在 1 秒内就有反应），之后放到
    # 400ms。等 30 秒时轮询次数从 250 降到约 80，每次轮询都是一次跨进程遍历。
    $intervalMs = 120
    while ($true) {
        $polls++
        # 每次遍历的预算不能超过「剩余时间」：一次遍历最多能烧 $budget.ms（900ms），
        # 而 deadline 只在两轮之间检查，于是「等 600ms」实测会跑 4.4 秒。
        # 按剩余时间收紧之后，超时才是真的超时。
        $remaining = [int](($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        if ($remaining -lt 50) { $remaining = 50 }
        $pollMs = $budget.ms
        if ($pollMs -gt $remaining) { $pollMs = $remaining }
        $last = Get-UiTree $hwnd $budget.nodes $budget.depth $pollMs
        $lastMatch = Find-TreeMatch $last ([string]$payload.text)
        $hit = ($null -ne $lastMatch)
        if ($wantAbsent) { $hit = (-not $hit) }
        if ($hit) {
            $elapsed = [int](([DateTime]::UtcNow - $started).TotalMilliseconds)
            if ($wantAbsent) {
                return [ordered]@{
                    matched = $true
                    polls = $polls
                    elapsed_ms = $elapsed
                    element = $null
                    fingerprint = ''
                    elements = 0
                    tree_truncated = $false
                    authoritative = $false
                }
            }
            $final = Get-UiTree $hwnd $fullBudget.nodes $fullBudget.depth $fullBudget.ms
            $finalMatch = $null
            if ($null -ne $final) { $finalMatch = Find-TreeMatch $final ([string]$payload.text) }
            return [ordered]@{
                matched = $true
                polls = $polls
                elapsed_ms = $elapsed
                element = $finalMatch
                fingerprint = $(if ($null -ne $final) { $final.fingerprint } else { '' })
                elements = $(if ($null -ne $final) { $final.items.Count } else { 0 })
                tree_truncated = $(if ($null -ne $final) { $final.truncated } else { $false })
                # 权威遍历里没找到 = 它落在这轮预算之外，编号不可用于动作。
                authoritative = ($null -ne $finalMatch)
            }
        }
        if ([DateTime]::UtcNow -ge $deadline) { break }
        $waited = ([DateTime]::UtcNow - $started).TotalMilliseconds
        if ($waited -gt 1000) { $intervalMs = 400 }
        if ($waited -gt 5000) { $intervalMs = 800 }
        Start-Sleep -Milliseconds $intervalMs
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
        # 睡到 deadline 为止，别整睡 100ms 再发现已经超时：实测 300ms 的预算
        # 会跑成 455ms。
        $left = [int](($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        if ($left -le 0) { break }
        if ($left -lt 100) { Start-Sleep -Milliseconds $left } else { Start-Sleep -Milliseconds 100 }
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

function Get-FreshElement($hwnd, $node, $budget) {
    # 重新定位一个元素。ComboBox 的弹出项每次展开都会重建，遍历时拿到的引用
    # 在动作时就失效了（缓存里有 pattern、live 里没有）。按「类型|名字|坐标」
    # 重新走一遍树找回来，比让模型重新 inspect 便宜得多。
    $tree = Get-UiTree $hwnd $budget.nodes $budget.depth $budget.ms
    if ($null -eq $tree) { return $null }
    $wantLabel = [string]$node.label
    $wantName = [string]$node.name
    $wantBounds = ($node.bounds -join ',')
    for ($i = 0; $i -lt $tree.items.Count; $i++) {
        $item = $tree.items[$i]
        if ([string]$item.label -ne $wantLabel) { continue }
        if ([string]$item.name -ne $wantName) { continue }
        if (($item.bounds -join ',') -ne $wantBounds) { continue }
        return $tree.elements[$i]
    }
    return $null
}

function Get-OperationHint($node) {
    # 「它其实支持什么」——比一句"不支持"有用得多。
    $ops = @($node.operations)
    if ($ops.Count -eq 0) { return '它没有可用的元素级操作，请按坐标点' }
    return '它支持：' + ($ops -join ', ')
}

function Find-ScrollablePattern($el) {
    # 找到「真正能滚的那个东西」，返回 @{pattern; note}：
    # 1) 自己；2) 最近的可滚祖先（列表项 -> 列表）；3) 内部可滚的后代
    #    （XAML 的 ListBox 把 ScrollViewer 藏在内部，祖先链上没有）。
    $pattern = Get-LivePattern $el ([System.Windows.Automation.ScrollPattern]::Pattern)
    if ($null -ne $pattern) { return @{ pattern = $pattern; note = '' } }
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $up = $null
    try { $up = $walker.GetParent($el) } catch { }
    $hops = 0
    while ($null -ne $up -and $hops -lt 12) {
        $candidate = Get-LivePattern $up ([System.Windows.Automation.ScrollPattern]::Pattern)
        if ($null -ne $candidate) {
            $containerName = ''
            try { $containerName = [string]$up.Current.Name } catch { }
            return @{ pattern = $candidate; note = ('滚动的是外层容器「' + $containerName + '」') }
        }
        try { $up = $walker.GetParent($up) } catch { $up = $null }
        $hops++
    }
    $all = $null
    try {
        $all = $el.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Automation]::ControlViewCondition)
    } catch { }
    if ($null -ne $all) {
        $limit = [Math]::Min($all.Count, 60)
        for ($i = 0; $i -lt $limit; $i++) {
            $candidate = Get-LivePattern $all[$i] ([System.Windows.Automation.ScrollPattern]::Pattern)
            if ($null -ne $candidate) {
                return @{ pattern = $candidate; note = '滚动的是它内部的滚动区域' }
            }
        }
    }
    return @{ pattern = $null; note = '' }
}

function Invoke-ElementOperation($el, $node, $operation, $text, $amount, $scrollLimit) {
    # 返回 $null 表示成功，否则返回可读的拒绝理由（{code,message}）。
    # 先确认元素还活着：控件在遍历之后被销毁时，下面的 GetCurrentPattern 会抛异常，
    # 被吞掉之后报成"这个控件不支持该操作"——方向完全错了，模型会去试别的操作。
    try { $null = $el.Current.ControlType } catch {
        $why = '这个控件已经不在了（界面变了）；'
        $why += '重新调 desktop_inspect'
        return [ordered]@{ code = 'element_gone'; message = $why }
    }
    if ($operation -eq 'setvalue') {
        if ([bool]$node.password) {
            $why = '拒绝写入密码框；这类输入必须用户自己敲'
            return [ordered]@{ code = 'password_field'; message = $why }
        }
        $pattern = Get-LivePattern $el ([System.Windows.Automation.ValuePattern]::Pattern)
        if ($null -eq $pattern) {
            $why = '这个控件不接受写入内容；' + (Get-OperationHint $node)
            return [ordered]@{ code = 'operation_unsupported'; message = $why }
        }
        $readOnly = $false
        try { $readOnly = [bool]$pattern.Current.IsReadOnly } catch { }
        if ($readOnly) {
            return [ordered]@{ code = 'operation_unsupported';
                message = '这个控件是只读的' }
        }
        try { $pattern.SetValue([string]$text) } catch {
            return [ordered]@{ code = 'operation_failed'; message = $_.Exception.Message }
        }
        return $null
    }
    if ($operation -eq 'invoke') {
        $pattern = Get-LivePattern $el ([System.Windows.Automation.InvokePattern]::Pattern)
        if ($null -eq $pattern) {
            $why = '这个控件不能点击；' + (Get-OperationHint $node)
            return [ordered]@{ code = 'operation_unsupported'; message = $why }
        }
        try { $pattern.Invoke() } catch {
            return [ordered]@{ code = 'operation_failed'; message = $_.Exception.Message }
        }
        return $null
    }
    if ($operation -eq 'toggle') {
        $pattern = Get-LivePattern $el ([System.Windows.Automation.TogglePattern]::Pattern)
        if ($null -eq $pattern) {
            $why = '这个控件不能切换；' + (Get-OperationHint $node)
            return [ordered]@{ code = 'operation_unsupported'; message = $why }
        }
        try { $pattern.Toggle() } catch {
            return [ordered]@{ code = 'operation_failed'; message = $_.Exception.Message }
        }
        return $null
    }
    if ($operation -eq 'select') {
        $pattern = Get-LivePattern $el ([System.Windows.Automation.SelectionItemPattern]::Pattern)
        if ($null -eq $pattern) {
            $why = '这个控件不能选中；' + (Get-OperationHint $node)
            return [ordered]@{ code = 'operation_unsupported'; message = $why }
        }
        try { $pattern.Select() } catch {
            return [ordered]@{ code = 'operation_failed'; message = $_.Exception.Message }
        }
        return $null
    }
    if ($operation -eq 'expand' -or $operation -eq 'collapse') {
        $pattern = Get-LivePattern $el ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
        if ($null -eq $pattern) {
            $why = '这个控件没有可展开的内容；' + (Get-OperationHint $node)
            return [ordered]@{ code = 'operation_unsupported'; message = $why }
        }
        try {
            if ($operation -eq 'expand') { $pattern.Expand() } else { $pattern.Collapse() }
        } catch {
            return [ordered]@{ code = 'operation_failed'; message = $_.Exception.Message }
        }
        return $null
    }
    if ($operation -eq 'scroll') {
        # 按控件滚动：长列表里"滚这个列表"和"滚整个窗口"是两件事，
        # 物理滚轮只能滚光标底下的窗口。
        $found = Find-ScrollablePattern $el
        $pattern = $found.pattern
        $script:OperationNote = [string]$found.note
        if ($null -eq $pattern) {
            $why = '这个控件没有可滚动的内容；' + (Get-OperationHint $node)
            $why += '；要滚窗口请用 action=scroll'
            return [ordered]@{ code = 'operation_unsupported'; message = $why }
        }
        # 参数名是 $amount（第五个位置参数）。写成 $step 时它在函数作用域里
        # 不存在，[int]$step 恒为 0，于是不管传几都只滚一步——真机对不出来，
        # 因为"滚了一点"看起来是对的。
        $steps = [int]$amount
        if ($steps -eq 0) { $steps = 1 }
        $direction = if ($steps -gt 0) {
            [System.Windows.Automation.ScrollAmount]::LargeIncrement
        } else {
            [System.Windows.Automation.ScrollAmount]::LargeDecrement
        }
        # 上限由调用方下发（Python 的 MAX_SCROLL），这里不写死数字：
        # 两处各有一个 20 时，"改了 Python 没改 PS"会静默地少滚几格。
        $limit = 20
        if ($null -ne $scrollLimit -and [int]$scrollLimit -gt 0) {
            $limit = [int]$scrollLimit
        }
        if ([Math]::Abs($steps) -gt $limit) { $steps = $limit * [Math]::Sign($steps) }
        $beforePercent = -1
        $beforeScrollable = $true
        try {
            $beforePercent = [double]$pattern.Current.VerticalScrollPercent
            $beforeScrollable = [bool]$pattern.Current.VerticallyScrollable
        } catch { }
        try {
            for ($i = 0; $i -lt [Math]::Abs($steps); $i++) {
                $pattern.Scroll(
                    [System.Windows.Automation.ScrollAmount]::NoAmount, $direction)
                Start-Sleep -Milliseconds 30
            }
        } catch {
            return [ordered]@{ code = 'operation_failed'; message = $_.Exception.Message }
        }
        # 到顶/到底要说出来，否则模型会一直滚（每次都是一轮往返 + 一次审批）。
        try {
            $afterPercent = [double]$pattern.Current.VerticalScrollPercent
            if (-not $beforeScrollable) {
                $script:OperationNote = '这个区域本来就不需要滚动'
            } elseif ([Math]::Abs($afterPercent - $beforePercent) -lt 0.5) {
                if ($steps -gt 0 -and $afterPercent -ge 99.5) {
                    $script:OperationNote = '已经滚到底了'
                } elseif ($steps -lt 0 -and $afterPercent -le 0.5) {
                    $script:OperationNote = '已经滚到顶了'
                }
            }
        } catch { }
        return $null
    }
    if ($operation -eq 'focus') {
        try { $el.SetFocus() } catch {
            return [ordered]@{ code = 'operation_failed'; message = $_.Exception.Message }
        }
        return $null
    }
    $why = 'operation 只能是 invoke/setvalue/toggle/select/expand/'
    $why += 'collapse/scroll/focus'
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
            return [ordered]@{ code='out_of_window'; message='坐标落在目标窗口之外' }
        }
        $vs=[System.Windows.Forms.SystemInformation]::VirtualScreen
        if ($x -lt $vs.X -or $y -lt $vs.Y -or $x -ge ($vs.X+$vs.Width) -or $y -ge ($vs.Y+$vs.Height)) {
            return [ordered]@{ code='out_of_bounds'; message='坐标超出虚拟桌面范围' }
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
                return [ordered]@{ code='invalid_button'; message='button 只能是 left、right 或 middle' }
            }
            $count = 1
            if ($null -ne $step.count) { $count = [int]$step.count }
            if ($count -lt 1 -or $count -gt 2) {
                return [ordered]@{ code='invalid_count'; message='点击次数只能是 1 或 2' }
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
            return [ordered]@{ code='invalid_amount'; message='滚动格数必须在 -20 到 20 之间' }
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
            return [ordered]@{ code='text_too_long'; message='输入文本超过 1000 个字符' }
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
        else { return [ordered]@{ code='invalid_key'; message='不认识的键名' } }
        foreach ($mod in $mods) {
            if (-not $modMap.ContainsKey($mod)) {
                return [ordered]@{ code='invalid_key'; message="不认识的修饰键：$mod" }
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
            return [ordered]@{ code='invalid_wait'; message='wait 步骤必须是 50-3000 毫秒' }
        }
        Start-Sleep -Milliseconds $ms
    } else {
        return [ordered]@{ code='invalid_action'; message='不支持的动作' }
    }
    return $null
}
function Invoke-ElementAction($payload, $state) {
    # 元素级动作：按编号找控件，走 UIA 的 Invoke/SetValue 等，不移动鼠标。
    # 好处是遮挡、DPI、动画时序都不影响，而且能被读回验证。
    # 单独成函数是因为它是分发器里最复杂的一支（约 200 行），内联进去
    # Invoke-HarnessOp 就没人读得动了。
    # 病灶进程名单同样生效：它拒绝的是「进入这个进程的无障碍树」，
    # 而不是某一种读法——只对 inspect/wait 生效等于留了个后门。
    if (Test-ExcludedProcess $state $payload.exclude) {
        $why = "$($state.process) 在排除名单里，读取无障碍树被禁用"
        $why += '（HARNESS_DESKTOP_EXCLUDE_PROCESSES），因为它可能卡住'
        Fail 'process_excluded' $why $state
    }
    $budget = Get-TreeBudget $payload
    $target = [IntPtr][Convert]::ToInt64([string]$state.window_id, 16)
    $tree = Get-UiTree $target $budget.nodes $budget.depth $budget.ms
    if ($null -eq $tree) {
        Fail 'tree_unavailable' '读不到这个窗口的界面结构' $state
    }
    if ([string]$tree.fingerprint -ne [string]$payload.tree) {
        $why = '界面在那次检查之后变了；重新调 desktop_inspect '
        $why += '再用新的编号'
        Fail 'tree_changed' $why $state
    }
    $index = [int]$payload.element - 1
    if ($index -lt 0 -or $index -ge $tree.items.Count) {
        Fail 'element_missing' "那个窗口现在只列出 $($tree.items.Count) 个控件" $state
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
            # 名字**和**值都要看：有些控件没有 Name，只有 Value（或者反过来）。
            # 只匹配 name 的话，「无名字的支付按钮」这种就漏了。
            try {
                $hit = [bool]([string]$node.name -match $expr) -or
                       [bool]([string]$node.value -match $expr) -or
                       [bool]([string]$node.label -match $expr)
            } catch { $hit = $false }
            if ($hit) {
                $why = '这个控件属于只能用户自己做的事'
                $why += '（密码、验证码、支付或权限）；'
                $why += '告诉他点什么，然后停下'
                Fail 'must_do_self' $why $state
            }
        }
    }
    # 模型声明的控件名要和实际控件对得上。指纹管「结构没变」，这条管
    # 「编号没写错」——两者互补，缺一个都可能点错东西。
    if ($null -ne $payload.expect -and [string]$payload.expect) {
        $want = ([string]$payload.expect).ToLowerInvariant()
        $actual = (([string]$node.name) + ' ' + ([string]$node.value)).ToLowerInvariant()
        if (-not $actual.Contains($want)) {
            $why = "编号和名字对不上：#" + $payload.element + " 是 "
            $why += "「" + $node.name + "」，不是「" + $payload.expect + "」；重新调 desktop_inspect"
            Fail 'element_mismatch' $why $state
        }
    }
    # 记录"这次没按批准的方式做完"：元素动作降级成物理点击时，用户批的
    # 「按控件编号操作」与实际发生的「动鼠标」不是一回事，审计要留痕。
    $degraded = ''
    $problem = Invoke-ElementOperation $el $node ([string]$payload.operation) `
        ([string]$payload.text) $payload.amount $payload.max_scroll
    if ($null -ne $problem -and $problem.code -eq 'operation_unsupported' -and
        @($node.operations) -contains [string]$payload.operation) {
        # 节点声称支持这个操作，live 上却没有 —— 多半是元素被重建了。
        # 重新定位一次再试；还不行才如实报不支持。
        $fresh = Get-FreshElement $target $node $budget
        if ($null -ne $fresh) {
            $el = $fresh
            $problem = Invoke-ElementOperation $el $node ([string]$payload.operation) `
                ([string]$payload.text) $payload.amount $payload.max_scroll
            if ($null -eq $problem) {
                $script:OperationNote = '控件被界面重建过，已重新定位后执行'
                $degraded = 'relocated'
            }
        }
    }
    if ($null -ne $problem -and $problem.code -eq 'operation_unsupported' -and
        [string]$payload.operation -in @('invoke', 'select', 'toggle', 'expand') -and
        @($node.bounds).Count -eq 4) {
        # 有些控件（WPF 的 ComboBox 弹出项是典型）根本不暴露 SelectionItemPattern，
        # 但用户就是「点它一下」。此时按坐标点中心是等价动作，比报错有用得多。
        $cx = [int]$node.bounds[0] + [int]([int]$node.bounds[2] / 2)
        $cy = [int]$node.bounds[1] + [int]([int]$node.bounds[3] / 2)
        $clickStep = @{ action = 'click'; x = $cx; y = $cy; button = 'left'; count = 1 }
        $clickError = Invoke-Step $clickStep $state
        if ($null -eq $clickError) {
            $problem = $null
            $script:OperationNote = '这个控件不支持元素级操作，已改为点击它的中心'
            $degraded = 'click'
        }
    }
    if ($null -ne $problem) { Fail $problem.code $problem.message $state }
    Start-Sleep -Milliseconds 40
    # 先走树再读状态：两者要反映同一个时刻。反过来的话，动作弹出一个对话框时
    # 会得到「状态=旧窗口、控件=新对话框」的矛盾回执（实测 WPF MessageBox 就会）。
    $afterTree = $null
    # 动作之后再走一遍树：告诉调用方界面变没变、新的指纹是什么。
    # 展开/点击这类动作最常问的下一个问题就是"现在有什么"，顺手给出来，
    # 省掉一次 desktop_inspect（一次往返 + 一次审批）。
    #
    # 例外：`focus` 只改键盘焦点，控件集合与名字都不会变，而我们已经有一份
    # 刚走的树（$tree）——直接复用它，省掉一次 600ms 级的遍历。
    $after = Get-WindowState
    $isFocus = ([string]$payload.operation -eq 'focus')
    # 前台换了（点了按钮弹出对话框）就按**新**窗口重新遍历：
    # 否则会给出"pid/窗口号是新的、指纹和编号却是旧窗口的"这种自相矛盾的回执，
    # 模型拿它去操作必然 tree_changed（实测 WPF MessageBox 就是这样）。
    $walkTarget = $target
    if ($after.available -and [string]$after.window_id -ne [string]$state.window_id) {
        try {
            $walkTarget = [IntPtr][Convert]::ToInt64([string]$after.window_id, 16)
        } catch { $walkTarget = $target }
    }
    if ($isFocus -and $walkTarget -eq $target) { $afterTree = $tree }
    else { $afterTree = Get-UiTree $walkTarget $budget.nodes $budget.depth $budget.ms }

    # 目标控件的状态变了没有？指纹刻意不含 state（见 Get-TreeFingerprint），
    # 所以「勾选一个复选框」时指纹必然不变——但确实有变化，不该被当成"没反应"。
    $targetKey = [string]$node.label + '|' + [string]$node.name
    $targetChanged = $false
    if ($null -ne $afterTree) {
        foreach ($item in $afterTree.items) {
            if (([string]$item.label + '|' + [string]$item.name) -ne $targetKey) { continue }
            if (($node.state -join ',') -ne ($item.state -join ',') -or
                [string]$node.value -ne [string]$item.value) { $targetChanged = $true }
            break
        }
    }

    # 「没有变化」是最该复核的答案：模型读到它会以为动作没生效然后重试。
    # 而菜单/弹出层关闭带一点延迟，40ms 时常常还没关（实测右键菜单偶尔如此）。
    # 只在**真的一点变化都看不出**时才多等一次：
    #   指纹变了 / 目标状态变了 / focus 只动焦点 —— 都不需要复核。
    # 否则每次 toggle 都白付 140ms 加一次遍历（指纹不含 state，必然相同）。
    if (-not $isFocus -and -not $targetChanged -and $null -ne $afterTree -and
        [string]$afterTree.fingerprint -eq [string]$tree.fingerprint) {
        Start-Sleep -Milliseconds 140
        $retry = Get-UiTree $walkTarget $budget.nodes $budget.depth $budget.ms
        if ($null -ne $retry -and
            [string]$retry.fingerprint -ne [string]$tree.fingerprint) {
            $afterTree = $retry
        }
    }
    $afterItems = @()
    $afterFingerprint = ''
    $afterTruncated = $false
    $added = @()
    $removed = 0
    $targetAfter = $null
    if ($null -ne $afterTree) {
        $afterItems = @($afterTree.items)
        $afterFingerprint = [string]$afterTree.fingerprint
        $afterTruncated = [bool]$afterTree.truncated
        # 新增/消失的控件：「展开之后多了什么」是最常问的下一句。
        # 用计数而不是集合：列表里常有同名控件（"确定"/"更多"），
        # 按集合算会少报。
        $beforeCounts = @{}
        foreach ($item in $tree.items) {
            $key = [string]$item.label + '|' + [string]$item.name
            if ($beforeCounts.ContainsKey($key)) { $beforeCounts[$key]++ }
            else { $beforeCounts[$key] = 1 }
        }
        $afterCounts = @{}
        foreach ($item in $afterTree.items) {
            $key = [string]$item.label + '|' + [string]$item.name
            if ($afterCounts.ContainsKey($key)) { $afterCounts[$key]++ }
            else { $afterCounts[$key] = 1 }
        }
        # 每个键「多出来几个」，再按遍历顺序取前几个作为新增。
        $surplus = @{}
        foreach ($key in $afterCounts.Keys) {
            $before = 0
            if ($beforeCounts.ContainsKey($key)) { $before = $beforeCounts[$key] }
            if ($afterCounts[$key] -gt $before) { $surplus[$key] = $afterCounts[$key] - $before }
        }
        foreach ($item in $afterTree.items) {
            $key = [string]$item.label + '|' + [string]$item.name
            if ($surplus.ContainsKey($key) -and $surplus[$key] -gt 0) {
                $added += $item
                $surplus[$key] = $surplus[$key] - 1
            }
        }
        foreach ($key in $beforeCounts.Keys) {
            $afterCount = 0
            if ($afterCounts.ContainsKey($key)) { $afterCount = $afterCounts[$key] }
            if ($beforeCounts[$key] -gt $afterCount) { $removed += ($beforeCounts[$key] - $afterCount) }
        }
        # 同一个控件在动作后的状态（$targetKey 上面已算好）。
        foreach ($item in $afterTree.items) {
            if (([string]$item.label + '|' + [string]$item.name) -eq $targetKey) {
                $targetAfter = $item
                break
            }
        }
    }
    return [ordered]@{
        ok = $true
        action = 'element'
        operation = [string]$payload.operation
        element = [int]$payload.element
        target = $node
        steps_done = @()
        state_before = $state
        state_after = $after
        fingerprint_before = [string]$payload.tree
        fingerprint_after = $afterFingerprint
        items_after = $afterItems
        items_after_truncated = $afterTruncated
        items_added = @($added)
        items_removed = [int]$removed
        target_after = $targetAfter
        note = [string]$script:OperationNote
        degraded = [string]$degraded
    }
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

CONTROL_BODY = r'''
function Invoke-HarnessOp($req) {
    # worker 是常驻进程：每个请求开始前把「上一轮遗留的说明」清掉，
    # 否则一次滚动留下的 note 会挂到后面某次点击的回执上。
    $script:OperationNote = ''
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
            return New-Failure 'invalid_window' 'expected_window_id 必须是一个窗口号' $state
        }
        $procId = [uint32]0
        [HarnessDesktopNative]::GetWindowThreadProcessId($target, [ref]$procId) | Out-Null
        if ([int]$procId -ne [int]$payload.expected_pid) {
            return New-Failure 'window_gone' '那个窗口已经不属于这个进程了' $state
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
            $why = 'Windows 拒绝把这个窗口切到前台。'
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
            return New-Failure 'no_foreground_window' '当前没有可用的前台窗口' $state
        }
        if ($null -ne $payload.expected_pid -and
            ([int]$state.pid -ne [int]$payload.expected_pid -or
             [string]$state.window_id -ne [string]$payload.expected_window_id)) {
            Fail 'foreground_changed' '前台窗口在开始等待之前就变了' $state
        }
        if (Test-ExcludedProcess $state $payload.exclude) {
            $why = "$($state.process) 在排除名单里，读取无障碍树被禁用"
            $why += '（HARNESS_DESKTOP_EXCLUDE_PROCESSES），因为它可能卡住'
            return New-Failure 'process_excluded' $why $state
        }
        # 和 inspect 同一条防线：敏感窗口连控件名都不给（名字本身也是隐私）。
        # 实测踩过：这里漏掉判断，就能用「猜名字 + 等它出现」把密码窗口里的
        # 输入框内容原样读出来，绕过 inspect 的拒绝。
        if ($state.sensitive_target) {
            $why = '这个窗口看起来是密码、支付或验证码页面，'
            $why += '所以不提供控件信息；请让用户自己操作'
            return New-Failure 'sensitive_target' $why $state
        }
        # 轮询用缩小预算（等待要跑很多轮），命中后再按完整预算走一遍，
        # 保证返回的指纹和 desktop_inspect 的含义一致。
        $fullBudget = Get-TreeBudget $payload
        $budget = @{ nodes = $fullBudget.nodes; depth = $fullBudget.depth; ms = $fullBudget.ms }
        if ($budget.nodes -gt 80) { $budget.nodes = 80 }
        if ($budget.ms -gt 900) { $budget.ms = 900 }
        $result = Wait-ForControl $payload $state $budget $fullBudget $deadline
        $result.action = 'wait'
        $result.target = 'control'
        $result.state = Get-WindowState
        return [ordered]@{ ok = $true; result = $result }
    }
    if ($payload.action -eq 'inspect') {
        # 只读：给模型一份裁剪过的控件清单。敏感窗口连结构都拒绝——「支付密码」
        # 「验证码」这些字段名本身也是隐私，不是只有值才算。
        if (-not $state.available) {
            return New-Failure 'no_foreground_window' '当前没有可用的前台窗口' $state
        }
        if ($state.sensitive_target) {
            $why = '这个窗口看起来是密码、支付或验证码页面，'
            $why += '所以不提供界面结构；请让用户自己操作'
            return New-Failure 'sensitive_target' $why $state
        }
        if (Test-ExcludedProcess $state $payload.exclude) {
            $why = "$($state.process) 在排除名单里，读取无障碍树被禁用"
            $why += '（HARNESS_DESKTOP_EXCLUDE_PROCESSES），因为它可能卡住'
            return New-Failure 'process_excluded' $why $state
        }
        $budget = Get-TreeBudget $payload
        $target = [IntPtr][Convert]::ToInt64([string]$state.window_id, 16)
        $tree = Get-UiTree $target $budget.nodes $budget.depth $budget.ms
        if ($null -eq $tree) {
            return New-Failure 'tree_unavailable' '读不到这个窗口的界面结构' $state
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
    if (-not $state.available) { Fail 'no_foreground_window' '当前没有可用的前台窗口' $state }
    if ([int]$state.pid -ne [int]$payload.expected_pid -or
        [string]$state.window_id -ne [string]$payload.expected_window_id) {
        Fail 'foreground_changed' '前台窗口和快照时不一样了；重新调一次 desktop_snapshot' $state
    }
    if ($state.sensitive_target) {
        Fail 'sensitive_target' '这是密码或支付类敏感窗口，桌面输入被拒绝' $state
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
        $why = '往终端窗口键入等于执行命令；确认窗口后带上 '
        $why += 'allow_terminal=true 再试'
        Fail 'terminal_input' $why $state
    }
    if ($payload.action -eq 'element') {
        return Invoke-ElementAction $payload $state
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
                Fail 'foreground_changed' "$label`: 前台窗口变了；重新快照" $current $index $done
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

CONTROL_MAIN = r'''
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
