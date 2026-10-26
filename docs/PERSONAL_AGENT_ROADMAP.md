# Personal Agent 下一阶段改进方案（桌面 / 系统 / 账号）

状态：规划文档（2026-10-25）。前一份 [PERSONAL_AGENT_PLAN.md](PERSONAL_AGENT_PLAN.md) 覆盖的阶段 0–2
（跨进程回合协调、远程控制、主动性）已落地并在真机验收；本文只讨论**能力边界**的下一步：
从「文件 + 命令 + 文档」型 agent 扩到「桌面 + 系统 + 账号」型 personal agent。

本文顺序：① 现状与缺口 → ② 竞品怎么做的 → ③ 我们的设计原则 → ④ 分阶段方案（可直接开工）
→ ⑤ 与现有代码的映射 → ⑥ 验收口径 → ⑦ 明确不做。

---

## 1. 现状总结（对照代码，不是印象）

### 1.1 已有的权限底座

| 层 | 实现 | 现状 |
|---|---|---|
| 策略闸 | `src/exec_policy.py` | 三档 sandbox（`read-only` / `workspace-write` 默认 / `danger-full-access`）× 三种审批策略（`untrusted` / `on-request` 默认 / `never`）；路径四区：workspace / outside / secret（直接拒）/ system（默认拒写）；联网一律审批 |
| 内核层 | `src/sandbox.py` | Windows Low IL，子进程只能写工作区；**默认 `off`**，文档明确“读和网络不受限制” |
| 回合层 | `turn_coordinator` + `session_locks` | 全局单回合、同会话写锁；跨进程 steer/interrupt |
| 授权账本 | 会话级白名单 + `revoke_permissions` + `src/grants.py`（P4.1） | 目录写授权已可持久化、查看与撤销；域名/工具级仍是规划 |
| 审计 | `session_store` 事件全量落盘 | 无统一检索/导出，无“我批准过什么”的汇总 |

### 1.2 缺口清单（按个人助理日常价值排序）

| # | 能力 | 现状 | 缺口性质 |
|---|---|---|---|
| A1 | 截屏 / 看屏幕 | 已支持：`screen_capture`（P1，2026-10-26） | 已补齐（截图可回传飞书） |
| A2 | 本地 OCR | 已支持：`screen_ocr` 走 `Windows.Media.Ocr`（P1，2026-10-26） | 已补齐（仅限截图目录） |
| A3 | 剪贴板读写 | 已支持：`clipboard_read` / `clipboard_write`（P3a，2026-10-26） | 已补齐（读写均需审批） |
| A4 | 鼠标键盘控制 | 无 | 缺能力（高风险，最后做） |
| A5 | 窗口/进程管理 | 只能 `bash` 间接 tasklist/taskkill | 缺封装 |
| B1 | 开机自启 / 守护 | 无，重启后要手动拉 API + 网关 | 缺能力（决定“常驻”是否成立） |
| B2 | 本地通知 | 能力层 `src/notify.py` + 网关失败降级（2026-10-26） | 已补齐（含空闲门控定时任务） |
| B3 | 空闲/锁屏感知 | 已支持：`src/idle.py` + `GET /v1/system/state` + 定时任务空闲门控（2026-10-26） | 已补齐（P2d 接线） |
| C1 | 凭据存储 | `.env` 明文 | 缺安全边界 |
| C2 | 浏览器登录态 | `web_fetch` 无 cookie | 缺能力 |
| D1 | 目录级持久授权 | 已支持：`src/grants.py` + `/grants`（P4.1，2026-10-26） | 已补齐（仅放宽工作区外写审批） |
| D2 | 删除可撤销 | `bash del` 不进回收站 | 缺安全网 |
| E1 | 语音输入/输出 | 只保存不转写；无 TTS | 缺能力 |
| F1 | 浏览器自动化 | 无（可外接 MCP） | 缺能力 |
| G1 | 多机协同 | 单机单实例 | 缺能力（个人不需要） |
| H1 | 资源限额 | bash 有 timeout，无内存/CPU/配额 | 缺护栏 |
| H2 | 自动复核 | 有 LLM 审查官（`HARNESS_LLM_APPROVAL`），默认关闭 | 缺默认策略 |

一句话：**写有闸、读和网络近乎放开、桌面观察刚起步（截屏 + OCR，仍受逐次审批），
系统层（自启/通知/自动化）尚未碰。**

---

## 2. 竞品怎么做的（2026-10 调研）

### 2.1 Codex（OpenAI）— 与我们最接近的架构

来源：[Unlocking the Codex harness](https://openai.com/index/unlocking-the-codex-harness/)、
[Sandbox](https://learn.chatgpt.com/docs/sandboxing)、[Running Codex safely](https://openai.com/index/running-codex-safely/)。

| 设计 | 具体做法 | 对我们的启示 |
|---|---|---|
| App Server | JSON-RPC over stdio，双向；**server 可以主动发起 approval 请求并暂停 turn**；原语 = item（`started`/`delta`/`completed`）、turn、thread | 我们已有 `turn_id`/`turn_started`/`turn_terminal`/steer/interrupt，等价物齐了；差的是“稳定协议 + 客户端可生成类型” |
| 双控制 | **sandbox = 技术边界**（macOS Seatbelt / Linux bwrap / Windows 原生沙箱），**approval = 何时问人**；两者独立 | 我们两层都有，但内核层默认关。应把“默认开”做成产品决策，而不是可选实验 |
| 权限档位 | `sandbox_mode` × `approval_policy` × `approvals_reviewer`（`user` / `auto_review`）；`untrusted` 已退役 | 我们的三档三策略已对齐；缺 `auto_review`（自动复核低风险） |
| 可写根 | `sandbox_workspace_write.writable_roots = ["~/development"]` | 对应我们缺的“目录级持久授权” |
| 网络 | Proxy + `allowed_domains` / `denied_domains` / `allow_local_binding` + 未知域名需审批 | 我们现在只有“联网一律审批”，缺域名级策略 |
| 规则 | Starlark `prefix_rule(pattern=["gh","pr",["view","list"]], decision="allow", justification=...)` | 我们 `policy_bash` 是内置启发式，缺**用户可写的规则文件** |
| 凭据 | `cli_auth_credentials_store="keyring"`、`mcp_oauth_credentials_store="keyring"` | 对应我们的 `.env` 明文缺口 |
| 审计 | OpenTelemetry 导出：user prompts、approval decisions、tool results、MCP 使用、网络 allow/deny | 我们事件都在本地 jsonl，缺统一导出与“审批账本” |

### 2.2 dots（OpenAI，2026-09 发布）— 最接近“personal agent”的形态

来源：[Introducing dots](https://openai.com/index/introducing-dots/)。

- **每个 dot 有自己的云电脑和浏览器**；你的电脑默认隔离，“除非你选择连接它”。
- **后台主动性 = 只读工具**：“当你没有在和它工作时，它用已连接的、被限制为只读的工具做主动研究，
  这些工具不能发消息、改内容、控制浏览器或电脑。”
- 权限：选择它能访问哪些 app；**Custom Rules** 三态（allow / 需审批 / block）；
  影响账户或外发信息的动作走 **auto-review**；**改密码这类永远留给人**。
- 可观察：随时打开它的电脑看工作过程；Activity View 看进度（含后台）。
- 密码可由系统代填，**不暴露给模型**。

对我们最有价值的三条：
1. **“主动/后台”与“前台交互”用不同权限档**——后台只读，前台才可写；
2. **敏感操作白名单是硬编码的**（改密码永远不代做），不是靠模型自律；
3. **用户随时能“看它现在在干什么”**，这是信任的前提。

### 2.3 桌面控制（computer use）全景

来源：[AI Agents That Control Your Mouse and Keyboard (2026-10)](https://www.simular.ai/alternatives/ai-agents-that-control-your-mouse-and-keyboard)。

三类形态，别混为一谈：

| 形态 | 代表 | 特点 | 对我们的意义 |
|---|---|---|---|
| 控制**你自己的**桌面 | Claude computer use、ChatGPT Work Computer Use、Perplexity Personal Computer、UI-TARS Desktop | 用真实账号/文件；**无沙箱**；电脑要醒着；一次一个会话 | 最贴合“个人助理”，也最危险 |
| 控制**云电脑** | dots、Sai、Copilot Actions（独立会话） | 隔离好、可无人值守；拿不到本地上下文 | 长期方向，个人单机成本高 |
| 只控制浏览器 | Browser Use、Chrome Auto Browse | 覆盖网页不覆盖桌面 app | 可作为 P5 的第一步 |

安全共识（各厂商一致）：
- **prompt injection 是首要威胁**：“能读屏幕的 agent 会被屏幕指挥”；
- 无人值守优先隔离（云电脑 / 独立会话）；
- 不可逆动作必须审批（付款、发消息、删文件、改设置）；
- **全程留痕**（截图/轨迹既是审计也是调试）。

Windows 侧可参考：Copilot Actions 用**独立 Windows 会话 + 独立 agent 账号**，文件默认只能碰
`Documents/Downloads/Desktop/Music/Pictures/Videos`。

### 2.4 OpenClaw（开源，MIT）

- 消息优先（WhatsApp/Telegram/Slack…）+ gateway；桌面控制是**可选 node**。
- macOS 需要 Allow Computer Control + Accessibility + Screen Recording；Windows/Linux 实验性、仅主屏。
- **没有逐动作确认、默认没有沙箱**，官方文档自己要求“把收到的消息当作不可信输入”。

反面教材价值：我们已有的审批 + 沙箱 + 白名单，比它更严；不要为了“顺手”把这些拆掉。

### 2.5 落地技术可行性（本机已验证）

- **本地 OCR 可用**：`[Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages` 返回 `zh-Hans-CN`，
  说明 Windows 内置 OCR 引擎与中文模型都已就绪。PyWinRT 新版要求 Python ≥3.11，我们跑 3.10，
  所以**优先用 PowerShell 调 WinRT**（零 pip 依赖），未来再切 `winrt-*` 包。
  另有 NPU 版 `Microsoft.Windows.AI.Imaging.TextRecognizer`，仅 NPU 机器可用。
- **截屏**：PowerShell + `System.Windows.Forms`/`System.Drawing`（零依赖）。
- **剪贴板**：`Get-Clipboard` / `Set-Clipboard`（零依赖）。
- **Toast**：WinRT `ToastNotificationManager`（`win11toast` 展示了完整可行路径），
  可选 `BurntToast` 模块；零依赖方案是用 PowerShell 直接构造 toast XML。
- **自启**：计划任务 `schtasks /Create /SC ONLOGON /RL LIMITED`，或启动目录快捷方式。

---

## 3. 我们的设计原则（在竞品之上做取舍）

1. **双控制不合并**：`exec_policy`（该不该）与 `sandbox`（内核允不允许）继续独立；
   新增能力必须同时回答这两个问题，缺一不发布。
2. **后台比前台严**：定时/心跳/主动性回合只读（已有 `use_scheduled_turn` + read-only 复核），
   这条要继续覆盖新工具——截屏、剪贴板、通知在后台回合里默认拒绝。
3. **看屏幕 ≠ 摸键盘**：A1/A2（观察）与 A4（输入控制）分开发布，中间隔至少一个版本，
   且 A4 默认关闭、需要显式配置 + 每次会话审批。
4. **没有本地留痕就不做**：每个新工具都要落审计事件（谁、何时、参数摘要、结果摘要、截图哈希），
   并能在飞书上一键查看最近 N 条。
5. **敏感清单硬编码**：改密码、支付、2FA、输入凭据**永不代做**，写在代码里，不受配置影响
   （对标 dots 的“永远留给人”）。
6. **零依赖优先**：优先 PowerShell/.NET/WinRT；需要 pip 的能力（keyring、playwright）单独
   `requirements-*.txt`，缺依赖时工具返回明确安装提示，而不是让整个 agent 起不来（沿用现有 `read_document` 模式）。
7. **默认值偏保守**：新能力默认 `off` 或只读；开启需要用户显式动作（配置文件/按钮），并写进
   启动日志与 `/status`。

---

## 4. 分阶段方案

### P1：桌面观察（截屏 + 本地 OCR）— 1 个迭代

**状态：已实施（2026-10-26）**。能力层、工具层、策略/后台/子代理边界、保留清理、状态展示、
文档与测试均已完成；P1b（把截图原图通过飞书 `im/v1/images` 发回聊天）随后也已落地。

**目标**：飞书发“看看我屏幕上的报错”“把截图里的表格读出来”，agent 能自己取到画面。

**工具接口**（`src/tool_screen.py`，已注册进 `tools.TOOLS`，共 42 个工具）

| 工具 | 参数 | 行为 |
|---|---|---|
| `screen_capture` | `screen`（`primary`/`all`，默认 primary）、`region`（可选 `x,y,w,h`）、`delay_ms` | 截屏存到 harness 缓存 `.cache/screenshots/<sha1>.png`，返回路径 + 尺寸 + 显示器信息；`ImageResult` 自动附带图片给视觉模型 |
| `screen_ocr` | `path`（默认最新截图）、`lang`（默认 `zh-Hans-CN`，`auto` 跟随系统）、`region`（可选） | 走 PowerShell + `Windows.Media.Ocr`，返回纯文本 + 每行 bbox；无 OCR 语言包时列出可用语言并给安装提示 |

**权限与审批**（落地口径，与初稿不同）

- 三种 sandbox 档位下 `screen_capture` / `screen_ocr` 一律按 `screen_access` **逐次询问**；
  **不参与会话白名单**——批准一次截屏不该让本会话此后随时能看屏幕。`never` 策略下按
  fail-closed 直接拒绝（`danger-full-access` + `never` 才放行），与既有审批语义一致。
- **后台/定时回合直接拒绝**：`SCHEDULED_FORBIDDEN_TOOLS` 与 `enforce(interactive=False)`
  双重兜底，错误文案明确“需要交互式回合”。
- **子代理不暴露**这两个工具；`HARNESS_SCREEN_TOOLS=0` 可在策略层硬拒（默认开启）。
- 截图落盘固定在 harness 缓存 `.cache/screenshots`（不进工作区，避免被 checkpoint/补丁扫到）；
  OCR 只接受该目录内的文件，不能借它识别任意图片。
- 审计事件：`screen_captured{path,sha1,bytes,screen,region,width,height,duration_ms}`、
  `screen_ocr{path,chars,lang,region,lines,duration_ms}`；**不记录 OCR 文本内容**。
- 保留 7 天，进程启动时的 `start_storage_maintenance` 清理过期文件；`/status`、`--doctor`
  与飞书控制台显示截图数量/占用。

**实现要点**

1. `src/screen_capture.py`：PowerShell（`System.Windows.Forms.Screen` + `Graphics.CopyFromScreen`），
   单屏/多屏/区域、DPI 感知、内容寻址落盘、7 天清理与统计；错误结构化返回，不把原始异常
   直接抛给模型。
2. `src/ocr_windows.py`：`Windows.Media.Ocr`（WinRT），支持语言选择、区域裁剪、行/词 bbox 合并；
   缺语言包时返回可用语言与 `Get-WindowsCapability -Online -Name "Language.OCR*"` 提示。
3. 复用 `tool_documents.ImageResult`，截图走现有多模态链路，不新造传输路径。
4. 飞书：模型按工具描述自然调用 `screen_capture`；成功回合的截图自动回传本聊天
   （最多 4 张/回合、单张 ≤10MB，`FEISHU_SEND_SCREENSHOTS=0` 关闭）。

**测试**：fake PowerShell 后端（注入固定 PNG/JSON）；区域裁剪与越界断言；能力层失败路径；
后台回合拒绝、白名单不生效、环境开关硬拒；子代理排除；审计字段；7 天清理；`/status` 展示。

**验收**：真机截一张含中英文的窗口 → `screen_ocr` 文本与肉眼一致；说“看看我现在屏幕上的报错”
能拿到截图并回答，飞书聊天里同时收到截图图片。

**P1b 截图回传（2026-10-26，已完成）**

- API：SSE `done` 与非流式响应附带本回合 `screenshots` 元数据（`<sha1>.png`、尺寸），
  不返回绝对路径；`GET /v1/sessions/{id}/screenshots/{name}` 校验会话、严格文件名校验、
  拒绝符号链接后返回 PNG。
- 网关：`GatewayClient` 支持 `on_screenshots` 回调与受控下载，`LarkTransport.send_image`
  走 `im/v1/images` 上传后以图片消息回复；上传失败只影响图片，不回滚文字答复。
- 默认开启、每回合最多 4 张、单张 10MB；截断/超限附说明，`FEISHU_SEND_SCREENSHOTS=0` 关闭。

**风险与缓解**：截图含敏感信息 → 只存 `.cache/screenshots`、7 天清理、逐次审批、后台拒绝、
`/status` 可见；多屏 DPI 错位 → 返回里记录每屏 rect 与虚拟桌面范围。

### P2：常驻（自启 + 本地通知 + 空闲感知）— 已完成（2026-10-26）

**P2a 本地通知与空闲感知（2026-10-26，已完成）**

- `src/notify.py`：Windows Toast。PowerShell `-EncodedCommand`（UTF-16LE）里嵌 base64
  XML，标题/正文先转义并剔除控制字符与孤立代理项；借用 Windows PowerShell 的 AUMID，
  10 秒全局限频（失败尝试也计数），`HARNESS_NOTIFY=0` 关闭。所有失败收敛为
  `{sent, reason}`，不抛给回合或网关。
- `src/idle.py`：`GetLastInputInfo` 读空闲秒数，`OpenInputDesktop` 打不开输入桌面即视为
  锁屏（启发式，RDP 断开/UAC 安全桌面也会命中）；结果缓存 2 秒，`HARNESS_IDLE=0` 关闭。
  返回 `{available, idle_seconds, locked, checked_at[, error]}`，失败同样进缓存。
- `GET /v1/system/state`：`{"idle": {...}}`，走统一 Bearer 鉴权与限流。
- 测试：`tests/test_notify.py`（11）、`tests/test_idle.py`（12）、
  `tests/test_system_state_api.py`（3）。真机验证：空闲 `idle_seconds=33, locked=false`；
  实际 Toast 返回 `sent=True` 并出现在通知中心。
- 后续批次：网关发送失败降级由 P2c 完成；主动性任务按空闲触发由 P2d 完成。

**P2b 自启与守护（2026-10-26，已完成）**

- `scripts/install_autostart.py`：`install/remove/status` + `--dry-run`。任务名
  `HarnessAgent`，`/SC ONLOGON /RL LIMITED /F`，运行 `src/agent_service.py`；
  优先用同目录 `pythonw.exe`，登录时不闪控制台。安装器只构造并执行 `schtasks`，
  不会自动安装，也不提供模型工具入口；dry-run 只打印命令。
- `src/agent_service.py`：一个监督进程启动 API（`python -m uvicorn api:create_app`）
  与可选飞书网关；日志写 `.cache/logs/{api,feishu}.log`（10MB 轮转，子进程强制 UTF-8）。
  API 用 `/openapi.json` 探活（30 秒），端口占用/缺 token/API 未就绪/网关启动后退出
  都会发本地失败 Toast；成功后发“Harness Agent 已就绪”。网关缺 `FEISHU_*` 时只跑 API。
  运行期任一子进程退出 → 通知 + 结束监督进程；**默认不自动重启**。
- 测试：`tests/test_install_autostart.py`（9）、`tests/test_agent_service.py`（13），
  覆盖命令构造/quoting、dry-run 不执行、非 Windows 拒绝、启动顺序、网关 env、
  缺 token/端口占用/启动失败、子进程退出通知、日志轮转。真机只读验证：dry-run
  输出包含 `ONLOGON` 与 `pythonw.exe`，`status` 正确报告“未安装”。
- 未做：`--restart-on-crash`；Task Scheduler `/End` 强制结束监督进程时，子进程
  清理未用 Job Object 保证（普通退出/Ctrl+C 会 terminate，超时再 kill 进程树）。

**P2c 飞书失败降级本地通知（2026-10-26，已完成）**

- `FeishuGateway` 新增 `notifier` 注入（默认 `None`）与按事件键的 120 秒去重窗口。
  覆盖四处发送失败：`_reply` 文字回复、`_watch_notify` 回合终态/审批超时、`poll_heartbeat`
  心跳摘要、`poll_cron_results` 定时结果。
- 语义保持：回复与回合通知**仍向上抛**（调用方/`TurnWatcher` 依赖异常重试）；心跳与定时
  结果仍不 ack，保留 pending 下一轮补发。本地通知只做兜底，失败不影响发送路径。
- 启用条件：只有入口 `_run_gateway` 显式注入 `notify.notify` 才生效，
  `FEISHU_LOCAL_ALERTS=0` 关闭；测试直接构造网关不会弹真通知。
- 测试：`tests/test_feishu_local_alerts.py`（13），覆盖四路失败告警、成功不发、
  同键去重、不同键各一次、环境开关、无 notifier、notifier 自身抛错隔离、正文截断。
- 未做：本地通知点击后跳回飞书（仅系统 Toast）；网关断线期间没有 UI 常驻状态指示。

**P2d 定时任务空闲门控（2026-10-26，已完成）**

- `CronJob` 新增 `idle_after_s`（0 表示不门控）与 `deferred_since`（持久化到
  `.cache/crons.json`）。`tick()` 到点时先看门控：机器不空闲就记 `deferred_since`
  并延后；等机器空闲（锁屏视为空闲）后**补跑一次**，即使 cron 分钟已过去。
  等待超过 12 小时丢弃这次 occurrence（一次性任务直接移除），避免隔夜任务第二天
  突然执行。
- 探测成本：空闲快照 30 秒缓存，且只有“门控任务已到期或处于延后状态”时才探测；
  普通任务不触碰 `idle.py`。探测异常一律按“不空闲”处理，调度线程不受影响。
- 语义保持：未门控任务行为不变；`last_fired` 仍防同分钟重复触发；延后任务补跑后
  清空 `deferred_since`，不会重复补。
- 暴露面：API `idle_after_minutes`（0–1440，创建/更新可设）；模型工具
  `schedule_cron(idle_after_minutes=...)`、`list_crons` 显示 `[空闲 ≥N 分钟]`；
  gateway 客户端创建/更新透传；飞书任务卡片显示 `空闲≥N分`。
- 测试：`tests/test_cron_idle.py`（22），覆盖即时执行、忙碌延后、跨分钟补跑、
  一次性任务移除、锁屏视为空闲、探测不可用、12 小时丢弃、非门控不探测、
  缓存与显式探测、参数校验、更新清除延后、API/gateway/工具/卡片契约。
- 未做：门控只在派发前判断，入队后用户立刻回来仍可能执行（毫秒级窗口，可接受）；
  没有“只在接通电源时执行”“只在特定时间段执行”等更细条件。

**目标**：重启机器后无需人工，agent 起来并能在飞书断线时用系统通知兜底。

**能力**

| 项 | 方案 | 备注 |
|---|---|---|
| 自启 | `scripts/install_autostart.py`：写计划任务（`schtasks /SC ONLOGON /RL LIMITED /TN HarnessAgent`），启动 `src/agent_service.py`（API + 网关），日志到 `.cache/logs/` | 已完成（P2b）；当前用户权限，不提权 |
| 守护 | 进程内 watchdog：`api` 起不来/端口占用 → 通知；可选 `--restart-on-crash`（父进程守护子进程） | 已完成“起不来要吼一声”（P2b）；不做自愈重启 |
| 本地通知 | `src/notify.py`：Windows toast（WinRT），`notify(title, body, url?)`；飞书发送失败时降级 | 能力层（P2a）+ 网关降级（P2c）均已完成；`FEISHU_LOCAL_ALERTS=0` 可关 |
| 空闲/锁屏 | `src/idle.py`：`GetLastInputInfo` 读空闲秒数 + 锁屏判断；暴露 `/v1/system/state` | 已完成（P2a）；锁屏用输入桌面句柄启发式，未订阅 `SessionSwitch` 事件 |
| 空闲触发 | 定时任务带 `idle_after_s`：到点不空闲则延后，空闲/锁屏后补跑一次，超过 12 小时丢弃 | 已完成（P2d）；30 秒探测缓存，只在门控任务到期时探测 |

**权限**：`notify` 属“输出”，无需审批但限频（每 10 秒最多 1 条）；自启安装属**安装期操作**，
必须命令行显式执行，不给模型工具入口。

**测试**：计划任务命令构造（不真装）；notify 限频与降级；idle 解析；`/v1/system/state` 契约；
空闲门控的延后/补跑/过期与探测缓存（`tests/test_cron_idle.py`）。

**验收**：重启机器 → 飞书自动收到“服务已就绪”；关掉飞书网关 → 失败通知出现在 Windows 通知中心。

### P3：剪贴板与输入控制 — 2 个迭代（分两次发布）

**P3a 剪贴板（低风险，先做）— 已完成（2026-10-26）**

| 工具 | 行为 |
|---|---|
| `clipboard_read` | `Get-Clipboard -Raw`；超过 200KB 截断并提示；返回文本长度与前 N 字符 |
| `clipboard_write` | `Set-Clipboard`；**需要审批**（会覆盖用户剪贴板），内容摘要进审计 |

**落地**：`src/clipboard_windows.py` 用 PowerShell 5.1 读写，stdout 返回
`<总字节>\n<base64>`、写入经 stdin 收 base64，绕开 GBK 控制台与命令行长度限制；读取按
UTF-8 字节截到 200 KiB（截断时 `errors='ignore'`，避免半个字符）。`clipboard_read` 与
`clipboard_write` 都逐次审批、不参与会话白名单；后台/定时直接拒绝，子代理排除，
`HARNESS_CLIPBOARD_TOOLS=0` 整体关闭；`read-only` 档位同样不可读（剪贴板可能含密码）。
审计只记 bytes/chars/truncated/duration，不记内容或哈希。状态面板与控制台显示开关状态。

**测试**：`tests/test_clipboard_windows.py`（9）、`tests/test_tool_clipboard.py`（7）、
`tests/test_clipboard_policy.py`（8），覆盖截断/空文本/PowerShell 失败与超时/写入载荷/
审批矩阵/后台与定时拒绝；schema 快照更新为 44 个工具。真机只读验证：读取 218 字节、
172 字符，2.5 秒，未打印内容；未执行真实写入以免覆盖用户剪贴板。

**P3b 键鼠控制（高风险，默认关）**

- 工具：`ui_action`（`click/move/type/key/scroll`，坐标或无障碍名称）、`ui_snapshot`（截图 + 可选
  UI Automation 树，给模型更稳的定位）。
- 开关：`HARNESS_DESKTOP_CONTROL=1` **且** 每会话首次动作审批；`/status` 显示“桌面控制：开/关”。
- 硬约束：`TYPE` 只允许前台窗口；**禁止**在密码框/支付页面动作（按窗口标题 + UIA 控件类型拦截）；
  后台回合完全禁止。
- 定位优先 UIA（`UIAutomationClient`）再回退像素坐标；坐标动作前强制截图存证。

**验收**：P3a 手机“把我刚复制的发我”；P3b 打开记事本输入一段文字（需显式开启），
并验证密码框拦截生效。

### P4：权限模型 v2（把“能干什么”变成可配置、可撤销）

四个子项，建议按顺序：

1. **目录级持久授权**（`src/grants.py`）— **已完成（2026-10-26）**
   ```json
   {"version": 1,
    "paths": [{"path": "D:/work", "scope": "write", "expires": "2026-12-31",
               "created_by": "feishu:ou_xxx", "note": "项目目录",
               "session_id": null, "created_at": "2026-10-26T09:00:00+00:00"}]}
   ```
   已落地的部分只包含 `paths`（`scope` 目前只有 `write`）；账本存在
   `paths.cache_path('grants.json')`，写入走文件锁 + 临时文件原子替换，损坏时
   **策略层按“无授权”处理**（fail closed），修改操作直接报错而不是覆盖。
   生效边界：只在 `workspace-write` 档、只放宽 `PATH_OUTSIDE` 的写入；秘密文件、
   系统目录仍硬拒；`read-only` 档照旧审批；`apply_patch` 要求每个工作区外路径都被
   授权覆盖。授权只能由人创建（CLI `/grants add`、飞书 `/grants add` 或
   `POST /v1/grants`），模型没有新增授权的工具。
   查看与撤销：CLI `/grants` / `/revoke`、飞书 `/grants` 卡片（按钮 + 10 分钟 token）
   与控制台计数、`POST /v1/grants/remove`、`revoke_permissions`（只撤 `session_id`
   等于当前会话的授权，人手动加的全局授权保留）。审计事件 `grant_added` /
   `grant_removed` 写入对应会话日志。
2. **网络域名策略**（`src/net_policy.py`）：`allow` / `deny` / 需审批 三态，接在 `web_access.check_url` 前；
   默认仍是“未知域名需审批”。对标 Codex 的 `allowed_domains/denied_domains`。
3. **用户规则文件**（`src/rules.py`，YAML/TOML）：`prefix_rule([...], decision, justification)` 的简化版，
   只支持 `allow`/`ask`/`deny` 三态与命令前缀匹配；加载失败**不影响启动**，只打印并忽略。
4. **自动复核**（`auto_review`）：把现有 `HARNESS_LLM_APPROVAL` 审查官升级成默认 reviewer：
   低风险（只读、工作区内写）自动放行并记账；高风险仍问人；所有 auto 决策写审计事件
   `approval_auto{request,tool,decision,model,reason}`。**必须可一键关闭**。

### P5：浏览器与登录态（依赖外部能力，单独评估）

- 方案 A（推荐）：接 MCP（Playwright/Puppeteer MCP），把浏览器当作**可替换的外部工具**，
  避免把浏览器代码塞进内核；登录态用独立 profile 目录，不进 agent 上下文。
- 方案 B：内置 Playwright + persistent context；成本是被浏览器版本/依赖绑定。
- 硬约束：登录、支付、2FA 一律转人工（对标 dots 的“密码不暴露给模型”）；浏览器 profile 目录纳入
  secret 通配，模型不可读 cookie 文件。

### P6：审计与可观测（贯穿，P1 就开始）

| 事件 | 字段 |
|---|---|
| `screen_captured` / `screen_ocr` | path、sha1、bytes、duration_ms |
| `approval_auto` | request_id、tool、decision、reason、model |
| `grant_added` / `grant_removed` | path/domain、scope、by |
| `notify_sent` / `notify_failed` | channel、level、title_sha1 |
| `policy_denied` | tool、code、zone |

- 新增 `GET /v1/audit?since=` 与飞书「审计」卡片（最近 20 条，按类型过滤）；
- 可选 OTel 导出（`HARNESS_OTEL_ENDPOINT`），字段对齐 Codex 的 otel 事件命名习惯。

### P7：其他（明确排在后面）

语音 ASR（要 ffmpeg + key）、TTS、浏览器外设（摄像头/麦克风）、多机协同、回收站删除。

---

## 5. 与现有代码的映射

| 新增 | 复用/改动 |
|---|---|
| `src/tool_screen.py`、`src/screen_capture.py`、`src/ocr_windows.py` | `tools.tool_registry`、`tool_documents.cache_image`、`ImageResult`、`tool_schemas.TOOLS` |
| `src/notify.py`、`src/idle.py`、`scripts/install_autostart.py` | `paths.cache_path`、`api_*` 路由模式（`build_router`） |
| `src/tool_clipboard.py`、`src/clipboard_windows.py` | `tools.tool_registry`、`tool_schemas.TOOLS`、`exec_policy` 审批分支（`clipboard_access` / `clipboard_write`） |
| `src/tool_ui.py`（P3b） | `sandbox`、`approval`（新 `approval_class` 分类） |
| `src/grants.py`、`src/net_policy.py`、`src/rules.py` | `exec_policy.evaluate` 前置查表、`web_access.check_url` |
| `src/audit.py` + `api_audit.py` | `session_store` 事件、`feishu_*_card.py` 卡片风格 |

改动点（要小心，都有测试）：
- `exec_policy.SANDBOX_MODES` / policy 分支：新增 zone（如 `PATH_SCREEN`）时要保持旧行为不变；
- `tools.TOOLS` 顺序进入 system prompt 与指纹（`tests/test_tool_schemas.py::SCHEMA_SHA1`），
  新工具**追加在末尾**并更新指纹；
- 后台回合只读复核（`hooks.use_scheduled_turn`）的拒绝矩阵要加新工具，避免定时任务偷偷截屏。

---

## 6. 验收口径

每阶段都必须同时满足：

1. **自动化**：新增工具的边界测试（路径、审批、后台拒绝、审计字段）+ 全量回归通过；
2. **真机**：按 [FEISHU_ACCEPTANCE.md](FEISHU_ACCEPTANCE.md) 的模式补一份逐项表，
   记录版本、操作、预期、实际；
3. **安全复核**：`docs/DEBUG_LOG.md` 写清“这个能力最坏能做什么、谁拦住它”；
4. **可回退**：新能力都有一个环境变量或配置开关能一键关闭，关闭后行为与上线前一致。

P1 完成的最小验收（举例）：

- [ ] 飞书“看看我现在屏幕上的报错” → 收到截图 + 能描述报错内容
- [ ] 截一张中英文混排窗口 → `screen_ocr` 文本与肉眼一致（中文不乱码）
- [ ] 后台/定时回合请求截图 → 被明确拒绝
- [ ] 截图落盘在 `.cache/screenshots/`，7 天后自动清理
- [ ] `/status` 显示截图数量与占用空间

---

## 7. 明确不做

| 不做 | 原因 |
|---|---|
| 代做登录 / 支付 / 2FA / 改密码 | 收益 < 风险；对标 dots 的硬边界 |
| 默认开启键鼠控制 | prompt injection 下等于把整台机器交出去 |
| 常开摄像头/麦克风 | 隐私与合规成本过高 |
| 注册表/服务任意改 | 误操作代价不可逆，现有 `bash` + 审批已够 |
| 多机会话分配 | 个人单机不需要；单实例锁已覆盖误开双进程 |
| 自研手机端 | 飞书卡片已覆盖入口 |

---

## 8. 建议的推进顺序与工作量

| 阶段 | 内容 | 预估 | 依赖 |
|---|---|---|---|
| P1 | 截屏 + 本地 OCR + 审计事件 | 2–3 天 | 无（PowerShell 零依赖） |
| P2 | 自启 + toast + 空闲感知 | 2 天 | P1 的 notify 需求 |
| P3a | 剪贴板读写（已完成，2026-10-26） | 1 天 | P1 的审批模式 |
| P4.1 | 目录级持久授权 + 撤销 UI（已完成，2026-10-26） | 3 天 | 飞书卡片模式（已有） |
| P3b | 键鼠控制（默认关） | 5 天 | P1 截图存证 + P4 规则 |
| P4.2 | 域名策略 + 规则文件 | 3 天 | 无 |
| P4.3 | auto_review 默认化 | 3 天 | `HARNESS_LLM_APPROVAL` 现有实现 |
| P5 | 浏览器（MCP 接入） | 3–5 天 | P4.2 |
| P6 | 审计查询 + 卡片 | 2 天 | P1 起持续 |

先做 P1，因为它是后续所有“桌面类”能力的地基（截图是证据、OCR 是低成本理解），
而且零依赖、可完全用现有 `view_image` + 审批链复用。

截至 2026-10-26，P1 / P2 / P3a / P4.1 已完成；剩余按依赖关系推进：P3b（键鼠控制）
依赖 P4.1 的目录授权与撤销口，其次是 P4.2 / P4.3。

---

## 9. 参考

- Codex App Server 架构与协议：<https://openai.com/index/unlocking-the-codex-harness/>
- Codex 沙箱与审批模型：<https://learn.chatgpt.com/docs/sandboxing>
- Codex 在 OpenAI 内部的安全实践（网络策略、keyring、rules、OTel）：<https://openai.com/index/running-codex-safely/>
- dots 产品与权限模型：<https://openai.com/index/introducing-dots/>
- 桌面控制产品对比与安全共识（2026-10）：<https://www.simular.ai/alternatives/ai-agents-that-control-your-mouse-and-keyboard>
- Windows 内置 OCR（WinRT）：<https://pypi.org/project/winrt-Windows.Media.Ocr/>、
  <https://learn.microsoft.com/en-us/windows/ai/apis/text-recognition>
- Windows toast via Python/WinRT 参考实现：<https://github.com/GitHub30/win11toast>
