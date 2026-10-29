# 桌面操控能力调研（2026-10）

问题：我们的电脑操控工具是不是实现得太基础了？

结论：**感知层太基础，动作层不基础。** 动作面（坐标点击、键入、组合键、滚动、序列、
敏感窗口拦截、终端保护、pid+hwnd 双因子、每步前台复核）已经跟主流持平甚至更严；
差距全部集中在"模型怎么知道点哪儿"——我们只暴露**当前焦点元素**，没有界面元素树，
点击要靠截图 + OCR 自己算坐标。这是 2023 年的做法。

---

## 1. 三条技术路线

所有 GUI agent 都是同一个循环：**观察 → 定位（ground）→ 动作 → 验证**。工程难度
几乎全在"定位"和"验证"，不在"动作"。

| 路线 | 观察输入 | 定位方式 | 代表 | 强项 | 弱点 |
| --- | --- | --- | --- | --- | --- |
| 视觉坐标 | 截图 PNG | 模型直接吐 `(x, y)` | Anthropic computer use、OpenAI CUA、Gemini Mariner、Self-Operating Computer | 通用、跨平台、对自绘 UI 也有效 | 每次 1k+ tokens，小目标/高分屏定位差，不能读精确值 |
| 无障碍树 | UIA / AX / AT-SPI 树 | 模型选节点，或选节点 ID | Microsoft UFO、Windows-MCP、Playwright MCP、browser-use | 精确、便宜、能读控件值、能验证 | 自绘/游戏/部分 Electron 树为空；跨进程读取慢，可能卡死 |
| 纯视觉解析 | 截图 + 检测模型 | SoM 编号框 | OmniParser / UI-TARS | 补足无障碍树拿不到的图标 | 需要额外模型，标签会认错 |

2026 年的共识是**混用**：WindowsAgentArena 上表现最好的配置就是
"Set-of-Marks + UIA 无障碍树 + 像素级元素检测器"三者叠加，纯截图或纯树都不够。

---

## 2. 主流实现逐个看

### 2.1 Anthropic computer use toolset（`computer_toolset_20260801`）

纯视觉坐标路线的现行标准，工具定义内建在模型里（schema-less）。

- **17 个成员工具**：`screenshot`、`zoom`、`left_click`、`right_click`、`middle_click`、
  `double_click`、`triple_click`、`left_click_drag`、`mouse_move`、`left_mouse_down`、
  `left_mouse_up`、`cursor_position`、`scroll`、`type`、`key`、`hold_key`、`wait`。
- **批量动作**：一轮返回多个 `tool_use`，按顺序执行，第一个失败即停，后续回
  `Not executed: an earlier computer action in this turn failed.`，模型据此重规划。
  模型习惯以 `screenshot` 收尾，否则宿主可以在最后一个结果上补一张图省一次往返。
- **token 账**：工具定义 + 系统提示约 **4500 input tokens**（关掉 zoom 少约 410）；
  一张截图 **1000~1800 tokens**。单请求超过 **20 张图**会触发更严的单边限制，
  所以要么压到 2000px 以内，要么按批剪枝（推荐保留最近 3 张、每 25 轮剪一次，
  避免每轮都改前缀导致 prompt cache 全失效）。
- **分辨率**：桌面任务 1024x768 / 1280x720，网页 1280x800 / 1366x768；
  不建议超过 1920x1080。超限时按 `min(1, 1568/长边, sqrt(1_150_000/总像素))` 缩放，
  并把模型返回的坐标**反向放大**回真实屏幕。Retina/高分屏要算 2x 设备像素比。
- **`zoom`**：裁一块区域按原分辨率返回，用来读小字、图标、表格单元格。
  坐标始终是全屏截图坐标系，不是 zoom 图坐标系。
- **提示词工程**（有实测收益的三条）：
  1. "每步之后截图，用一句话说出截图显示了什么、这步是否成功，没成功就重试。"
  2. 指令文本放在图片**之前**，点击准确率更高。
  3. 下拉框/滚动条改用键盘快捷键更稳。
- 官方自认的短板：延迟偏高；坐标会幻觉；滚动在部分应用无效；表格要用
  `left_mouse_down`/`left_mouse_up` 逐格选。

### 2.2 Windows-MCP（CursorTouch，10.6k stars，MIT）

和我们的技术栈最接近（Windows + UIA），工程细节参考价值最大。

工具面：`Screenshot`、`Snapshot`、`Click`、`Type`、`Scroll`、`Move`、`Shortcut`、
`Wait`、`WaitFor`、`ControlStatus`、`DisplayInventory`、`App`、`MultiSelect`、
`MultiEdit`、`Scrape`、`Clipboard`、`FileSystem`、`Registry`、`Process`、
`PowerShell`、`Notification`。

值得抄的四个设计：

1. **`Screenshot` 与 `Snapshot` 分工**。`Screenshot` 只给图 + 光标位置 + 窗口列表，
   跳过 UIA 提取，"当你主要需要视觉上下文时这是默认第一调用"；`Snapshot` 给
   完整树 + 可交互元素 id + 可滚动区域，支持 `use_dom=True` 只取网页正文
   （Chrome/Edge 走 `RootWebArea`，Firefox 无此节点，退回 IAccessible2）。
   一句话：**图像便宜快速，树精确但贵，让模型自己选**。
2. **`WaitFor`**：一次调用内部轮询等待文本/窗口/元素/焦点出现，而不是让 agent
   发 `Wait` + 重新快照。动作间典型延迟 0.2~0.5 秒，但没有 `WaitFor` 时一轮
   异步加载要烧掉好几轮往返。
3. **UIA 卡死防御**（真实生产教训）：连接超时默认 **2000ms**、单次事务超时
   **5000ms**，某窗口超时一次就跳过并把该窗口标成"采集失败"，其余桌面照常返回；
   另有 `WINDOWS_MCP_EXCLUDE_PROCESSES` 按进程名**完全跳过**无障碍树遍历
   （典型是 `Code.exe` / `Cursor.exe`）——因为一个病理性的 a11y provider 会把
   整个采集卡住（FlaUI 上有实例：某窗口单次遍历 2500ms+）。
4. **桌面所有权 / 物理输入保护**：三档 gate（`relaxed`/`balanced`/`strict`）决定
   哪些工具需要"桌面租约"；AI 控制期间用户晃鼠标（累计 500 设备单位、间隔
   0.5s 分段）或按 `Ctrl+Alt+Shift+Backspace` 即抢回控制权，空闲 10s 后归还。
   官方明确写"用户接管是尽力而为，不是安全边界"。

其自认短板：无法选中段落中的部分文字（依赖 a11y 树）；`Type` 是整段灌入，
不适合在 IDE 里写代码；不能玩游戏。

### 2.3 OmniParser / UI-TARS（纯视觉解析）

补无障碍树拿不到的东西（图标、自绘控件、游戏）。

- 做法：微调 YOLOv8 检测可交互区域 + OCR 文本框 + 微调 BLIP-2 给图标生成功能
  描述，合并去重后给每个框编号，走 Set-of-Marks 让模型**选编号**而不是吐坐标。
- 关键收益是"局部语义"：只给带编号的截图，GPT-4V 在 SeeAssign 上准确率
  **0.705 → 0.938**；ScreenSpot 总体 **16.2% → 73.0%**，其中桌面图标类
  **11.8% → 63.6%**。V2 把 ScreenSpot-Pro 做到 **39.6%**（GPT-4o 原生 0.8%）。
- 反面教训：重复图标/重复文本会选错；图标描述模型只看裁剪图，会把"三个点"
  认成"加载中"；OCR 框的中心点可能落在可点区域外。

### 2.4 其它值得知道的

- **Microsoft UFO / UFO2**：走 UIA 树 → 序列化成 JSON 喂 GPT-4o。缺点是全量
  dump 5k~15k tokens，上下文很快被吃光。
- **browser-use**：Playwright + 把页面压成带编号的可交互元素列表，模型选编号。
  比裸坐标便宜且稳，但出不了浏览器。Playwright MCP 同理。
- **DirectShell（2026-02）**：把 UIA 树持续落进 SQLite，让 agent 用 SQL 查。
  它给的 token 对照值得记：截图 1.2k~5k、全量树 dump 5k~15k、
  快照式文本 50~200、SQL 查询 10~50。核心论点一句话：
  **"截图范式是在对一个本来就用文本描述自己的界面做计算机视觉"**。
- **AskUI / Cua / OpenAdapt / Skyvern**：分别是视觉 RPA、沙箱 VM 底座、
  录制-回放、网页流程化，路线不冲突。

---

## 3. 当前水平（别被分数骗）

| 基准 | 数字 | 说明 |
| --- | --- | --- |
| OSWorld-Verified（369 任务，~30 步/任务） | 头部 82%~90%，人类 72.36% | 各源口径不一（自报 vs 官方榜），且已达饱和区，差 1~2 分无意义 |
| OSWorld 2.0（108 条长流程，~318 步/任务） | 最好 partial 81.8%，binary 44.3% | **才是真实水位**：一小时级流程能完整做完的不到一半 |
| WindowsAgentArena（2024，GPT-4V 时代） | 最好 19.5%，人类 74.5% | 结论仍然是"UIA + SoM + 检测器"三者叠加最好 |

对我们的启示：短任务（点按钮、填表单）靠工具面设计就能做得不错；**长流程的
瓶颈在验证和恢复**，不在动作。这也是我们已经在做"每步前台复核"的方向。

---

## 4. 我们现在的实现（`src/tool_desktop.py` + `src/desktop_control_windows.py`）

> **2026-10 更新：差距 1、2、5 已经补上（`desktop_inspect` + 元素级动作），
> 详见第 8 节。下面这段是补齐之前的基线，保留它才能看出改了什么。**


4 个工具，930 行 PowerShell + UIA：

| 工具 | 返回 | 备注 |
| --- | --- | --- |
| `desktop_snapshot` | 前台窗口 title/process/pid/window_id/rect + **焦点元素** name/ControlType/IsPassword | 只有焦点元素，没有树 |
| `desktop_windows` | 顶层窗口列表（pid、hwnd、标题、是否前台/最小化） | 只读，免审批 |
| `desktop_action` | move/click/type/key/scroll/sequence/focus | 绝对屏幕坐标，校验落在窗口 rect + 虚拟屏内 |
| `focus` | 切前台 | pid+hwnd 双因子 |

`desktop_inspect` 的控件坐标写成 `(左,上 宽x高)`，并在输出里说明"按坐标点就点
左+宽/2, 上+高/2"。最初是四个裸数字 `(左,上,宽,高)`，我自己按 `(x1,y1,x2,y2)`
读成了中心，点到标题栏上——模型没有理由比我更懂这个顺序。实测 150% 缩放下按
物理像素算出的中心点能精确命中控件（点复选框中心的焦点就是那个 CheckBox），
说明 worker 的 DPI 感知与截图/OCR 是同一套坐标空间。

另有 `screen_capture`（PNG 存 cache 并附到下一轮）和 `screen_ocr`（WinRT OCR，
**带每行 bbox**，图像像素坐标）。

动作层已经做到位的（比多数开源工具严）：每步动作前重取前台窗口并比对 pid+hwnd；
敏感窗口（密码框、支付、验证码关键词）直接拒；终端窗口键入需显式
`allow_terminal`；`sequence` 单次审批内执行但逐步复核；DPIAware 保证坐标空间一致。

---

## 5. 差距清单（按"卡住任务"的概率排序）

### 差距 1：没有界面元素树——这是根本差距

模型只知道"焦点在哪个控件"。要找到"保存"按钮，只有两条路：
截图后自己估像素，或 `screen_capture` → `screen_ocr` → 从 bbox 反算坐标。
后者要 3 次工具调用 + 多次审批，而且 OCR 只覆盖文字，图标按钮完全无解。
菜单、下拉、列表项、Tab 页、树节点**都无法枚举**，只能盲点。

主流做法（Windows-MCP `Snapshot`、UFO、Playwright MCP、browser-use）都是：
返回一棵**裁剪过的、带编号的元素树**，模型说"点 #7"。

### 差距 2：没有元素级动作（Invoke / SetValue / Select）

我们只有物理输入（SendInput）。元素级动作不需要抢焦点、不怕遮挡、不受 DPI
和动画时序影响，而且**能读回控件值做验证**。Windows-MCP、UIA 客户端、RPA 都用它。
我们的 930 行 PS 只用了 `AutomationElement.FocusedElement` 一处 UIA，等于把
UIA 当"取焦点名字"的工具在用。

### 差距 3：没有等待/条件轮询

界面异步加载时 agent 只能 `sleep` + 重新 `desktop_snapshot`（还要重新审批）。
Windows-MCP 的 `WaitFor` 在一次调用里轮询文本/窗口/元素/焦点，省掉整轮往返。

### 差距 4：动作后没有验证信号

`desktop_action` 返回的是"动作已发出"，不是"目标状态变了"。截图路线的工具都会
在动作后附一张新截图（Claude 的提示词直接要求"每步之后截图并说明是否成功"）。
我们现在靠模型自己记得再调一次 `desktop_snapshot`，而它只看到焦点名字。

### 差距 5：UIA 健壮性

`Get-WindowState` 里 `FocusedElement` 包在 try/catch 里，但没有连接超时/
事务超时，也没有"病灶进程"排除名单。Windows-MCP 为此专门加了
2000ms/5000ms 双超时 + `EXCLUDE_PROCESSES`，因为一个卡住的 provider 会让
**整个采集**挂死。我们的常驻 PS worker 一旦被卡，后续所有桌面调用都要等超时。

### 差距 6：坐标契约没写清

`desktop_action` 的 `x/y` 描述是"inside the target window"，但实现里是
**绝对屏幕坐标**（校验用绝对 rect 区间）。`screen_ocr` 给的是图像像素坐标。
模型要在"截图区域偏移 → 屏幕坐标"之间手动换算，文档里没有任何一处说明，
这是必然踩的坑。

### 差距 7（次要）：没有多显示器信息

只有 `VirtualScreen` 级校验，没有每屏 work area / DPI / 缩放比。Windows-MCP
专门有 `DisplayInventory`；Claude 文档把"坐标偏移"列为点击失败第一原因。

---

## 6. 建议（分三期，先做便宜的）

### 第一期：把感知补上（收益最大，风险最低，不动动作层）

1. **`desktop_snapshot` 加元素树**（新增 `desktop_inspect`，保留现有语义不变）：
   - 取前台窗口子树，`ControlViewWalker`（不是 RawView，RawView 会被布局元素淹没），
     限深度与条数（例如深 12、最多 120 个节点），只保留"有趣"节点：
     有 Name / 是 Controller / 可 Invoke / 可设值 / 是 ListItem-TreeItem-TabItem。
   - 每个节点分配稳定序号，暴露 `role`、`name`、`value`（密码框一律
     `[redacted]`）、`enabled`、`offscreen`、`bounds`。
   - 输出成**紧凑文本**（不是 JSON）：`#7 Button "保存" [enabled] (420,318,88,30)`。
     目标 50~200 tokens，别走全量 dump 那 5k~15k 的路。
   - 必须走 `CacheRequest` 批量取属性，否则每个属性一次跨进程 COM 往返，
     120 个节点就是几千次调用。

2. **元素级动作**：`desktop_action` 增加 `element=<编号>` 目标，内部走
   Invoke / Value.SetValue / SelectionItem.Select / ExpandCollapse。物理坐标点击
   保留为兜底（自绘 UI、树为空时）。

3. **UIA 超时与排除名单**：连接 2000ms / 事务 5000ms，超时的窗口单独标记跳过；
   `HARNESS_DESKTOP_EXCLUDE_PROCESSES` 跳过病灶进程。

### 第二期：把循环补上

4. **`desktop_wait`**：等文本出现 / 窗口标题变化 / 元素出现 / 焦点变化，
   内部轮询，超时可配（默认 5s，上限 30s）。
5. **动作后自动带验证**：`desktop_action` 结果里附"前台窗口是否变化 +
   焦点元素是否变化"，一次调用完成"动作 + 该观察的那部分"。
6. **坐标契约写清 + 自动换算**：`screen_ocr` 的 bbox 提供"换算成
   `desktop_action` 可用坐标"的辅助字段，或者让 `desktop_action` 接受
   `origin="screenshot:<path>"` 直接吃区域偏移。

### 第三期：视觉兜底（可选，成本高）

7. 截图缩放 + 坐标反算（按 `1568` 长边 / `1.15M` 像素上限），以及区域放大
   （对应 Claude 的 `zoom`）。这块要改 `screen_capture` 与动作层的坐标系，
   必须先有差距 6 的契约。
8. SoM 编号叠图（截图 + 元素框编号）属于"树为空时的补丁"，只有在确实遇到
   自绘 UI 频繁失败时才值得引入检测模型。

### 明确不做

- **不做全量树 dump**：5k~15k tokens 会把上下文吃干，且模型在几百个节点里
  选错的概率高于在几十个节点里选错。
- **不做"AI 桌面所有权/抢回控制权"那套**：我们的模型是"用户在场、逐次审批"，
  不是长时间无人值守，现有审批闸门已经覆盖；加租约只会增加状态机复杂度。
- **不引入视觉检测模型**（OmniParser/UI-TARS）：本地推理成本、模型分发、
  版本漂移都不划算，除非差距 1、2 做完后仍有明确失败样本。

---

## 7. 哪种方式效果最好（成功率与稳定性证据）

一句话：**无障碍树为主、像素为补**。但这个结论有条件——它取决于模型会不会用树，
也取决于界面是不是标准控件。

### 7.1 成功率：同一模型、同一任务，只换观察方式

OSWorld 论文的原始消融（Ubuntu 桌面，2024 年模型，上限 15 步，总成功率）：

| 观察方式 | 模型 | 总成功率 |
| --- | --- | --- |
| 只有无障碍树 | GPT-4 | **12.24%** |
| 只有无障碍树 | GPT-4o | **11.36%** |
| 截图 + 无障碍树 | GPT-4V | 12.17% |
| 截图 + 无障碍树 | GPT-4o | 11.21% |
| Set-of-Mark 叠框 | GPT-4V | 11.77% |
| Set-of-Mark 叠框 | GPT-4o | 4.59% |
| 只有截图 | GPT-4V | 5.26% |
| 只有截图 | GPT-4o | 5.03% |
| 只有截图 | Gemini-ProV | 5.80% |
| 人类 | | 72.36% |

三个可以直接用的结论：

1. **无障碍树是截图的两倍多**（11.4%/12.2% 对 5.0%/5.3%）。
2. **树 + 截图 约等于 只有树**（12.17 对 12.24）。也就是说，对这批通用模型，
   **信息量在树里，截图几乎没加东西**。
3. 分应用看差距更极端：OS 类任务上，截图+树 41.67% 对纯截图 8.33%。

Windows 上（WindowsAgentArena，GPT-4V-1106，总成功率）：

| 观察方式 | 总成功率 |
| --- | --- |
| 像素 OCR + 检测器，无 UIA | 8.6% |
| 同左 + UIA 树 | 13.1% |
| UIA 树 + OmniParser（含图标语义） | **19.5%** |
| 人类 | 74.5% |

论文原话：只依赖像素 OCR 与图标检测的 agent，表现低于同时使用 UIA 树的。
但注意同一张表里的反例——GPT-4o-mini 加了树反而从 7.2% 掉到 2.6%。
**树只对"会用树"的模型有用**，塞进弱模型等于塞噪声。

### 7.2 关键前提：结论取决于"模型会不会用树"

这一条必须写在前面，否则上面那张表会被误读：

- 上表是**通用模型**（GPT-4/GPT-4o）的结果，它们没有为 GUI 做过专门训练，
  所以需要树把"屏幕上有哪些可点的东西、各在哪儿"显式喂进去。
- **专门训练过的 computer-use 模型已经不吃这一套了**。现在 OSWorld 上排在前面的
  都是纯视觉：Surfer 2 60.1%、Step-GUI-8B 48.5%、GTA1-7B 45.2%，全都不在推理时
  读无障碍树——它们的接地能力是训练进模型权重里的。

所以正确的说法不是"树比截图好"，而是：

| 模型类型 | 最优观察 |
| --- | --- |
| 通用模型 + 工具循环（我们的情况） | 无障碍树（2 倍以上差距） |
| 专用 computer-use 模型 | 截图 + 训练进来的接地 |

**我们属于第一类**：一个通用模型加一堆工具，没有 GUI 预训练。对这类系统，
树的可接地性优势是决定性的，而且**不要求模型具备视觉能力**。

### 7.3 稳定性之一：坐标在布局扰动下会崩

OSWorld 做过一个扰动实验：挑 28 个"用 SoM 表现不错"的任务，只改窗口位置、
窗口大小、加点无关窗口和杂物。结果：

| 条件 | 成功率 |
| --- | --- |
| 原始 | 50.79% |
| 窗口位置变了 | 36.5% |
| 窗口大小变了 | 15.04% |
| 加了杂物 | 25.39% |

**窗口大小一变，成功率掉到不到原来的三成。** 这是"按像素坐标点"的固有脆弱性：
坐标是相对布局的绝对值，布局一动就全错。而按元素 id 定位（UIA 的 Invoke，
或叠框编号）不受这类扰动影响——元素还在，编号还在。

这条比成功率数字更有工程意义：它说明**元素级定位不只更准，还更稳**。

### 7.4 分场景的最优组合不一样

| 场景 | 最优观察 | 证据 |
| --- | --- | --- |
| 桌面标准控件 | 树（或树 + 叠框） | OSWorld 12% 对 5%；WAA 13.1% 对 8.6% |
| 桌面自绘 / 图标密集 | 叠框 + 图标语义 | OmniParser 把 ScreenSpot 从 16.2% 拉到 73.0%，桌面图标类 11.8% → 63.6% |
| 网页 | 图 + DOM 叠框（纯文本 DOM 最差） | Mind2Web：纯 DOM 文本 24.6% 步骤成功率，图+叠框 45.2% |
| 网页（生产框架） | 混合 | Browser-Use 混合 89.1% 对 Agent-E 纯无障碍 73.1%（WebVoyager，非同环境慎比） |

网页为什么反过来？因为纯文本 DOM 丢掉了**位置和空间关系**，模型只能靠顺序猜；
桌面 UIA 树自带 bounding box，空间信息已经在树里。这是两条路线的关键差别。

### 7.5 稳定性之二：两种通道的失败模式正好互补

| | 无障碍树 | 截图 |
| --- | --- | --- |
| 可用性 | 自绘 / Canvas / 部分 Electron / 游戏可能为空或残缺 | **永远可用** |
| 定位精度 | 高（元素级） | 低（失败的步骤里 75% 以上是点错位置） |
| 单次耗时 | **3 秒到 26 秒**（OSWorld-Human 实测，随元素数增长） | 毫秒级，只占任务总时长 0.4%~1.7% |
| token | 过滤后 50~200；不过滤是几十万到上百万 | 1000~1800 / 张 |
| 对步数的影响 | 可能变多（LibreOffice Writer 21 → 50 步） | 不增加 |
| 最坏情况 | 病灶 provider 让遍历卡死（单窗口 2500ms+，全桌面枚举 3~10 秒） | 高分屏下小目标看不清 |

两条硬教训：

- **必须裁剪**。OSWorld 论文明确说原始 XML 是"上百万 tokens"，他们的做法是按
  标签 / 可见性 / 有无文本过滤，再改成**紧凑的制表符表格**。全量 dump
  （5k~15k tokens）是公认的下策。
- **必须设超时**。Windows 上一个病理性的 a11y provider 能把整个采集挂死，
  Windows-MCP 为此加了连接 2000ms / 事务 5000ms 双超时 + 进程排除名单。

### 7.6 对我们的具体结论

我们的处境和论文里的 agent 不一样：**我们用的不是 computer-use 专用模型**，
而是通用模型加工具。这让结论反而更明确：

1. **先做无障碍树，收益最大。** 对通用模型，结构化文本的可接地性远高于像素
   （同一模型 11~12% 对 5%），且**不要求模型有视觉能力**，这是关键。
2. **元素级动作顺带消灭头号失败模式。** 75% 以上的失败步骤含鼠标点击不准，
   按元素 id 走 Invoke 直接绕开坐标这一环。
3. **截图不做默认观察，只做兜底与验证。** 树为空、自绘 UI、需要确认视觉结果
   （图表、颜色、布局是否变了）时才上截图。Claude 官方那句"每步之后截图确认"
   是因为截图是它唯一的观察通道，我们不是。
4. **不要因为"混合最好"就两条通道常开。** 混合的收益来自像素补树的盲区
   （图标、Canvas、视觉验证），不是图像普遍更强；平时挂着只是烧 token，
   并且把每步延迟从毫秒推到秒级（树生成 3~26 秒这笔账是实打实的）。
5. **别指望树覆盖一切。** 自绘界面、游戏、部分 Electron 应用上树会残缺甚至为空，
   所以坐标点击不能删，只能降为兜底。
6. **将来若换成 computer-use 专用模型，这条结论要重估。** 那时纯视觉可能反超，
   而我们现在的树 + 元素 id 会变成"便宜的确定性通道"，仍然有用但不再是主力。

工程顺序：`desktop_inspect`（裁剪树 + 编号 + CacheRequest 批量属性）
→ `desktop_action element=<id>`（Invoke / SetValue）→ UIA 双超时与排除名单
→ 截图兜底。这条顺序和上一节一致，现在有数字支撑。

---

## 8. 落地结果（三期都已完成）

补上的六件事，对应第 5 节的差距清单：

| 差距 | 做法 | 状态 |
| --- | --- | --- |
| 1. 没有界面元素树 | `desktop_inspect`：裁剪 + 编号 + 指纹 | 完成 |
| 2. 没有元素级动作 | `desktop_action` 加 `action=element`（Invoke / SetValue / Toggle / Select / Expand / Collapse / Focus） | 完成 |
| 5. UIA 健壮性 | 三层预算（节点 / 深度 / 时间）+ 病灶进程排除名单 + 第四档界面规则 | 完成 |
| 3. 没有等待轮询 | `desktop_wait`：控件/窗口出现或消失，轮询在一次调用里 | 完成 |
| 4. 动作后没有验证信号 | 比对动作前后：前台窗口 / 焦点 / 标题，并说明「没变也不代表没生效」 | 完成 |
| 6. 坐标契约 | 截图写边车记原点，`screen_ocr` 每行直接给屏幕坐标；schema 写明「绝对屏幕像素」 | 完成 |
| 7. 多显示器信息 | `desktop_displays`：每块屏的物理矩形 / 工作区 / DPI，并点明前台窗口在哪块、要不要乘缩放 | 完成 |

第二期实测（自建 WPF 窗口）：

| 场景 | 结果 |
| --- | --- |
| 等窗口出现（已存在） | 命中，1 次轮询，58ms |
| 等不存在的标题，预算 600ms | 未命中，5 次轮询，668ms |
| 点按钮后等旧标题消失 | 命中，2 次轮询，183ms |
| 等不存在的控件，预算 500ms | 未命中，4 次轮询，526ms |
| OCR 行坐标 | `@(180,109)`（bbox 中心 + 截图原点），可直接给 `desktop_action` |

### 实测数据（本机，2026-10）

| 窗口 | 控件数 | 遍历耗时 | 备注 |
| --- | --- | --- | --- |
| 自建 WPF 测试窗 | 4 | 211ms | 类型/名字/可用操作全对 |
| WindowsTerminal | 5 | 113ms | |
| Chrome（真实网页） | 79 | 1208ms | 含页面链接、复选框、Tab |
| Edge（PDF，子树 26154 节点） | 40 | 1595ms | 文档节点单独限额生效 |
| 资源管理器（zip 视图） | 1 | 48ms | 该窗口确实不暴露 UIA 项 |

端到端：`setvalue` 写入中文读回一致；`invoke` 触发按钮回调；界面变化后指纹变化；
未知操作被拒；密码框拒绝写入。

### 性能 profile（`scripts/profile_desktop.py`）

三处热点，两处是固定开销、一处是白做的缓存：

| 位置 | 问题 | 改动 | 效果 |
| --- | --- | --- | --- |
| `Get-WindowState` | 每次调用重新编译两条判定正则（9ms）、用 `Get-Process` 取进程名（14ms）、窗口列表每次重建进程名表（20ms） | 正则预编译成 `[regex]`；换 `Process.GetProcessById`（6ms）；进程名表 2 秒 TTL 共享 | 44ms → **15ms** |
| `Get-UiTree` 属性读取 | 每个节点各调一次 `GetUpdatedCache`，但缓存请求激活后 `FindAll` 返回的子节点**本来就带缓存值** | 遍历期间保持激活、直接读 `.Cached`，只给读不出类型的个别元素留兜底 | 属性阶段 2-5x（Edge 211→117ms、Terminal 51→11ms），逐节点比对零差异 |
| `Get-VisibleWindows` | 独立重建整张进程名表 | 复用同一张缓存表 | 21-24ms → 13ms |

固定开销的收益要乘以调用次数：12 步序列要读 13 次状态（570ms → 195ms），
等待轮询（100ms 一轮）同理。

**注意首次触摸**：同一个 Chrome 页面第一次遍历 500-900ms、之后 90-130ms，差的是
Chromium 自己把无障碍树建起来的时间（provider 侧），不是我们的代码。单次基准会骗人。

Python 侧很干净：120 个控件 payload 34.6KB、解析峰值 137KB（1171 字节/控件）、
300 次循环后增长 2.75KB（无泄漏）。

token 成本要说实话：120 个控件渲染后 6284 字符 ≈ 3142 tokens，全量 JSON 8075 tokens，
而一张截图是 1000-1800 tokens——**拉满的控件列表比截图还贵**，所以默认上限 120 个
（`HARNESS_DESKTOP_TREE_NODES` 可调低），并且只在需要精确操作时才调它。

### 显示器与缩放（第三期）

这台开发机就是 150% 缩放。用**不做 DPI 感知**的探针读，得到 `1440x960 @ 96dpi`；
走真实 worker（`SetProcessDPIAware()`）读，得到 `2160x1440 @ 144dpi`——**同一台机器、
同一时刻，差 1.5 倍**。不做感知的那一版如果被用来告诉模型"坐标"，每个点都会偏 33%。
这就是 Windows-MCP 有 `DisplayInventory`、Anthropic 把"点击整体偏移"列为头号排障项的原因。

`desktop_displays` 输出（实测）：

```
显示器 1 块 · 虚拟桌面 0,0 2160x1440
坐标一律是物理像素，和截图、desktop_action 同一套。
  #1 \\.\DISPLAY1（主屏） 0,0 2160x1440 · 工作区 0,0,2160,1368 · 缩放 150%（144 dpi）
前台窗口「Windows PowerShell」在 #1 上。
这块屏是 150% 缩放：网页/设计稿给的逻辑坐标要乘 1.5 才是这里用的物理像素。
```

实现上刻意**不用** `Screen.AllScreens`（那是逻辑矩形）：走 `EnumDisplayMonitors` +
`GetMonitorInfo` 拿物理矩形，`GetDpiForMonitor` 拿每屏 DPI，工作区单独给
（任务栏占掉的部分不该被点）。`Get-WindowState` 也带上 `dpi`，
`desktop_inspect` 在缩放不是 100% 时加一行提示——那是模型准备点坐标的地方。

### 已知限制（实测过的，不是推测）

| 限制 | 表现 | 处理 |
| --- | --- | --- |
| 走 MSAA 兼容层的应用（部分 WinForms / 老程序 / 部分 Explorer 视图） | 按钮和输入框都报成 `Pane`，没有 Invoke/Value 模式 | 只能退回坐标点击；工具会提示"能读到的控件很少" |
| 自绘界面（游戏、Canvas、部分 Electron） | 树为空或只有窗口本身 | 如实说"没有可读的控件"，建议截屏 + 坐标 |
| `powershell`/`cmd` 进程拥有的窗口 | 被判成终端，输入/写值需显式 `allow_terminal=true`；**脚本起的 GUI 程序也会命中** | 显式开关；点击类不受影响 |
| 敏感窗口（密码/支付/验证码） | `inspect` / `wait`(控件) / 元素动作全部拒绝；`snapshot` / `wait`(窗口) 只给标题 | 有意的：控件名本身也是隐私 |
| 界面在"看一眼"和"点一下"之间变了 | 结构指纹不匹配 → `tree_changed` | 要求重新 inspect（宁可多一轮，不点错） |
| 同一进程里有两个名字一样的控件 | 编号不同（按遍历顺序），`expect` 只能确认名字 | 靠编号 + 指纹区分；必要时先用 `bounds` 人工核对 |
| 命中控件落在遍历预算之外 | 返回"编号不可用"，要求先 inspect | 避免给出不可执行的编号 |

### 输出设计：每句话都要能直接照着做

三处改动都是同一个原则——**输出是给下一步用的**：

| 输出 | 问题 | 现在 |
| --- | --- | --- |
| `desktop_inspect` | 让模型用 element 模式，却不给必填的 pid / 窗口号（模型只能再调一次 `desktop_snapshot`，又一次审批） | 头部给出 `pid 23844，窗口 60D5E`，指引里直接写"expected_pid 填 23844" |
| `desktop_wait` 命中 | 同上；另外"等 600ms"实测跑 4395ms（deadline 只在两轮之间检查，单次遍历能烧 900ms） | 补 pid/窗口号；按剩余时间收紧遍历预算（600ms → 667ms）；轮询自适应退避 |
| 控件清单 | 采了 `operations` 却从不显示，模型只能试错撞 `operation_unsupported` | 只显示"不是默认点击"的：`可写入` / `可切换` / `可展开` / `只能按坐标点`（纯按钮不写，省 token） |

token 代价：85 个控件的窗口，加标记后 810 字符 ≈ 405 tokens。

### 能力补齐：状态、按控件滚动、动作回执

| 能力 | 之前 | 现在 |
| --- | --- | --- |
| 控件状态 | 只列控件，不说勾没勾 / 开没开 / 选中了哪个 | `未勾选 · 可切换`、`已收起`、`已选中`、`只读`（ToggleState / ExpandCollapseState / IsSelected / IsReadOnly） |
| 滚动 | 只能滚光标所在的窗口 | `operation='scroll'` 按控件滚（`ScrollPattern`），能滚页面里的长列表 |
| 动作回执 | 只报动作完成 | 报"界面变没变 + 新指纹 + 新增控件"，省掉一次 inspect 与审批 |
| 验证 | 动作成功 = 调用没报错 | `setvalue` 读回实际值；`scroll` 到边界会说"已经滚到底了/到顶了" |

实测（WPF 表单窗口，勾选 → 选中列表项 → 展开）：**一次 inspect + 三个动作**串起来，
每步的指纹都是上一步回执给的：

```
已完成展开「高级选项」。
现在是已展开。
界面变化：多出 1 个控件。
#11 文本 "这里是展开后才有的内容" · 只能按坐标点 (139,245,593,23)
结构指纹：fe402dc9…
```

### 与主流的对照

- **输入是裁剪后的紧凑文本**（每控件一行，几十个 token），不是全量树 dump
  （5k~15k tokens），也不是截图（1000~1800 tokens/张）。
- **动作用元素编号 + UIA 模式**，不是坐标——这一条正对着"75% 的失败是点错位置"
  和"布局一变成功率掉到 15%"两个实测结论。
- **指纹校验**在主流的开源实现里没有（Windows-MCP、UFO 都靠模型自己记得刷新），
  代价是一次重遍历，换的是"绝不点错控件"。
- **对中文正则不能用 ``**：`验证码` 在「请输入验证码」里永远不匹配
  （汉字之间都是 word 字符）。第四档的界面/输入规则因此曾静默失效，现在英文用 ``、
  中文用裸词，并有守护测试盯着。
- **`expect`（控件名）双重作用**：审批卡上写清「要点的是哪个控件」（只给编号等于没有
  知情同意），执行前再和实际控件名核对（指纹只保证结构没变，保证不了编号没被写错）。
  这两条也是开源实现里没有的——它们默认"模型说点 #3 就是 #3"。
- **保留坐标点击**作为兜底：MSAA 兼容层应用、自绘界面、游戏仍然只能这样点。

---

## 9. 参考


- Anthropic computer use toolset：<https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool>
- Windows-MCP：<https://github.com/CursorTouch/Windows-MCP>
- OmniParser：<https://arxiv.org/html/2408.00203v1>
- WindowsAgentArena：<https://arxiv.org/html/2409.08264v1>
- OSWorld 榜：<https://presenc.ai/research/osworld-computer-use-leaderboard-2026>
- UIA CacheRequest（跨进程性能）：<https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nn-uiautomationclient-iuiautomationcacherequest>
- FlaUI 顶层窗口遍历慢的实证：<https://github.com/FlaUI/FlaUI/issues/616>
- OSWorld 观察方式消融（Tab.5）：<https://arxiv.org/html/2404.07972v2>
- WindowsAgentArena 消融（Tab.4）：<https://arxiv.org/html/2409.08264v1>
- OSWorld-Human：Benchmarking the Efficiency of Computer-Use Agents（Abhyankar et al., 2025；树的 3~26 秒与延迟构成）
- Agent for Computer Use 综述（2026-03）：<https://arxiv.org/html/2501.16150v3>
