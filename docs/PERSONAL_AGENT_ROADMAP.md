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
| A4 | 鼠标键盘控制 | 已支持：`desktop_action`（P3b，2026-10-26） | 已补齐（默认关、逐次审批、终端保护） |
| A5 | 窗口管理 | 已支持：`desktop_windows` + `focus`（2026-10-26） | 窗口已补齐；进程仍靠 bash |
| B1 | 开机自启 / 守护 | 已支持：`scripts/install_autostart.py` + `agent_service`（P2b） | 已补齐 |
| B2 | 本地通知 | 能力层 `src/notify.py` + 网关失败降级（2026-10-26） | 已补齐（含空闲门控定时任务） |
| B3 | 空闲/锁屏感知 | 已支持：`src/idle.py` + `GET /v1/system/state` + 定时任务空闲门控（2026-10-26） | 已补齐（P2d 接线） |
| C1 | 凭据存储 | 已支持：DPAPI 加密（2026-10-26） | 已补齐（静态保护，非运行时隔离） |
| C2 | 浏览器登录态 | 已支持：独立 profile + MCP（P5，2026-10-26） | 已补齐（凭据库已加 secret 名单） |
| D1 | 目录级持久授权 | 已支持：`src/grants.py` + `/grants`（P4.1，2026-10-26） | 已补齐（仅放宽工作区外写审批） |
| D2 | 删除可撤销 | 已支持：`delete_paths` 走系统回收站（2026-10-26） | 已补齐（bash 的 del/rm 未接管） |
| E1 | 语音输入/输出 | 只保存不转写；无 TTS | 缺能力（需外部服务，暂缓） |
| F1 | 浏览器自动化 | 已支持：MCP 接入 + `browser_setup.py`（P5） | 已补齐 |
| G1 | 多机协同 | 单机单实例 | 缺能力（个人不需要） |
| H1 | 资源限额 | 已支持：limits.py 的 Job Object（不依赖沙箱，2026-10-27） | 已补齐（无磁盘配额） |
| H2 | 自动复核 | 审查官默认开启，且两条自动放行路径都写审计（P4.3） | 已补齐 |

一句话（2026-10-26 更新）：**桌面观察与键鼠控制、剪贴板、删除可撤销、权限分层
（目录/域名/规则）、审计查询、凭据加密、浏览器 MCP 均已落地。剩余缺口只有语音（E1，
按需求暂缓）。**

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

**P3b 键鼠控制（高风险，基础版+序列已实现，默认关闭）**

- 新增 `desktop_snapshot` / `desktop_action`。前者只读前台窗口标题、进程/PID/HWND、窗口矩形和 UIA 焦点控件；
  后者执行一项 `move` / `click` / `type` / `key` / `scroll`，Windows PowerShell + user32 `SendInput`，
  Unicode 文本支持中文。
- **动作序列**：`action=sequence` 携带 1–12 个步骤（每步 `move/click/type/key/scroll/wait` 及其参数），
  一次审批覆盖整组计划，解决"每步都审批"的割裂感。脚本逐步执行，每步执行前重新读取前台窗口
  （身份 + 敏感词校验），任何一步命中即中止，错误信息带步骤序号与已完成步数，模型可据此重拍
  snapshot 再续。
- **流畅度（2026-10-26 第二轮）**：`click` 支持 `button=left/right/middle` 与 `count=1/2`（双击，
  两次点击间隔 40ms，按下前停留 20ms 让菜单/提示条稳定）；`key` 支持修饰键组合
  （`ctrl+v`、`ctrl+shift+s`、`alt+f4`、`win+d`），最多四个修饰键、按住后敲基础键再逆序释放；
  序列可插入 `wait` 步骤（50–3000 ms），用于等动画、菜单展开或页面加载。序列超时按步数
  与 wait 总时长自动放宽（上限 60s）。
- 开关 `HARNESS_DESKTOP_CONTROL=1`；关闭时策略硬拒。`never` 档位也硬拒，不走全自动放行；
  `danger-full-access` 不能绕过本能力的显式逐次审批。会话白名单、定时任务、后台回合和子代理均不能放行。
- 动作需要同时回传 `desktop_snapshot` 的 PID + HWND；窗口切换则拒绝。鼠标坐标必须落在该窗口矩形内，
  键名、滚轮幅度和输入文本长度均有限制；滚轮执行前把光标收拢进目标窗口，避免滚错窗口。
  UIA `IsPassword` 与标题/进程/焦点的敏感词启发式命中时拒绝。
- 每个动作结果返回新窗口状态（序列返回逐步结果），便于用户/模型再观察；审计只记动作、步数、
  PID/HWND、标题哈希与耗时，不记录键入文本。失败动作同样记录失败类型与短原因。
- 工程修复（2026-10-26 复审）：PowerShell 脚本内 `$pid` 与只读自动变量冲突导致每次调用必失败的
  缺陷已修（`$procId`）；结果改为 base64(UTF-8) 单行回传，绕开 GBK 控制台对中文标题的破坏；
  `SetProcessDPIAware` 与 `screen_capture` 坐标系对齐；PowerShell 解析器静态校验脚本语法，
  真机只读 snapshot 冒烟通过（3.3s）。
- **安全边界说明**：Windows 桌面是共享交互会话，进程/HWND 检查不能构成 OS 隔离；窗口可在检查与
  输入之间变化，支付页面也可能没有可识别标记。敏感窗口检查只是防误操作启发式，不是保证。不要把
  它用于自动登录、付款、改密码、2FA 或无人值守工作流；先单步观察，敏感操作由人执行。
- 自动化测试覆盖参数校验、序列组合边界、策略矩阵、窗口身份绑定、后台/子代理拒绝和审计脱敏；
  **尚未执行真实桌面输入验收**，第一次真机试用应仅在记事本中输入无敏感内容，并确认当前窗口确为记事本。

**原修订验收**：在记事本输入一段无敏感文字（显式开启、逐步审批），验证切换窗口与密码控件被拒绝。


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
2. **网络域名策略**（`src/net_policy.py` + `src/net_grants.py`）— **已完成（2026-10-26）**
   三态 `allow` / `deny` / 需审批，接在 `exec_policy._evaluate_web` 里。匹配只有两种形态：
   精确主机与 `*.` 通配子域（不含根域自身），不接受 `*` 或正则。deny 先于 allow，
   且在任何档位下都是硬拒（`danger-full-access` 也绕不过）；allow 只免除出口审批，
   仍受 URL 语法与私网拦截。账本在 `paths.cache_path('net_grants.json')`，写入走文件锁 +
   原子替换，损坏/未知版本按“无规则”处理（回到逐个审批）。
   入口：飞书 `/domains [allow|deny|remove] <主机>` 与卡片撤销按钮（10 分钟 token）、
   `GET/POST /v1/net-grants` 与 `/v1/net-grants/remove`、`/status` 与控制台计数；
   模型没有新增入口，`revoke_permissions` 只能收回本会话创建的规则。
   审计事件 `domain_added` / `domain_removed`。
3. **用户规则文件**（`src/rules.py`）— **已完成（2026-10-26）**
   YAML，`allow`/`ask`/`deny` 三态 + 命令词前缀匹配。实现上是**白名单**：只有
   `destructive` / `opaque_command` / `external_tool` 三类「噪音」允许被规则改写，
   以后新增决策码默认受保护。`allow` 在 `read-only` / `untrusted` 档下不生效（档位是
   用户的显式选择），`deny` 永远生效；多段命令要求每段都命中。加载失败不影响启动，
   打印一行后按「无规则」处理。入口：`/rules`（CLI 与 TUI）、`/status` 计数。
4. **自动复核**（`auto_review`）— **已完成（2026-10-26）**
   现状确认：审查官**默认已开启**（`HARNESS_LLM_APPROVAL` 不设即启用，`0/false/off/no`
   显式关闭），已有缓存、连续 3 次失败熔断、批级合并、独立超时，且只对
   `destructive` / `opaque_command` / `network_egress` 三类询问生效，非交互线程不旁路。
   真正缺的是**可审计**：放行只打印到控制台，没进事件流。现在两条自动放行路径都写
   `approval_auto{tool, by, code, reason, command, args_keys}`：
   - `by=llm-judge`：审查官放行；
   - `by=rule`：用户规则文件 `allow` 放行。
   参数只记摘要与键名（`approval_view.summarize` 口径），不复制整份参数；无活动会话、
   文件不存在或写入失败都静默跳过——审计不能反过来让一次已放行的调用失败。
   审计卡与 `/v1/audit` 已收录该类型（`🤖` 图标）。

### P5：浏览器与登录态 — **已完成（2026-10-26）**

采用方案 A：浏览器作为**可替换的外部 MCP server**，不进内核。

- **`src/browser_profile.py`**：独立 profile 目录 `<home>/.cache/browser-profile`，
  可复用登录态但不碰日常 Chrome/Edge；放在 `.cache` 下顺带获得 `DEFAULT_IGNORE`
  保护；POSIX 权限收到 0700。指向日常 profile 时明确警告（Windows）。
- **`scripts/browser_setup.py`**：`snippet`（生成 `--isolated` 或
  `--user-data-dir` 两种配置）/ `check`（按 name/command/args 识别浏览器 server，
  提示「既没 isolated 也没 user-data-dir」这类会踢掉用户窗口的配置）。
- **凭据保护补齐**：`Cookies`、`Login Data`、`Web Data`、`Local State`、
  `key4.db`、`logins.json` 等加入 `checkpoints.DEFAULT_SECRETS`，文件工具读写直接
  硬拒；`History`/`Bookmarks` 之类不挡，避免正常排障无从下手。
- 审批沿用既有语义：MCP 工具是第三方工具，三档下都默认审批，`never` fail-closed。
- 硬约束（使用约定而非配置）：登录、支付、2FA、改密码一律转人工。

### P6：审计与可观测（贯穿，P1 就开始）— **已完成（2026-10-26）**

| 事件 | 字段 |
|---|---|
| `screen_captured` / `screen_ocr` | path、sha1、bytes、duration_ms |
| `approval_auto` | request_id、tool、decision、reason、model |
| `grant_added` / `grant_removed` | path/domain、scope、by |
| `notify_sent` / `notify_failed` | channel、level、title_sha1 |
| `policy_denied` | tool、code、zone |

**落地**：`src/audit.py` 是纯投影层——只认白名单事件类型，并把字段裁到固定子集
（例如 `desktop_action` 不返回键入文本，`screen_ocr` 不返回识别文字）。跨会话按文件
修改时间从新到旧扫，命中 `since` 或条数上限即停，损坏行逐条跳过，单文件失败不影响其它。
`GET /v1/audit?since=&limit=&types=&session_id=` 暴露同一份结果，未知类型直接 400，
拼错不会静默返回空列表。飞书 `/audit [类型|all]` 与卡片翻页（8 条/页、10 分钟 token）。
`/status` 与控制台显示域名/目录授权计数。

- 新增 `GET /v1/audit?since=` 与飞书「审计」卡片（最近 100 条，按类型过滤）；
- 可选 OTel 导出（`HARNESS_OTEL_ENDPOINT`）仍为规划项。

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
| P4.2 | 域名策略（已完成，2026-10-26） | 3 天 | 无 |
| P4.2b | 用户规则文件（已完成，2026-10-26） | 2 天 | 无 |
| P4.3 | auto_review 审计化（已完成，2026-10-26） | 3 天 | `HARNESS_LLM_APPROVAL` 现有实现 |
| P5 | 浏览器（MCP 接入，已完成 2026-10-26） | 3–5 天 | P4.2 |
| P6 | 审计查询 + 卡片（已完成，2026-10-26） | 2 天 | P1 起持续 |

先做 P1，因为它是后续所有“桌面类”能力的地基（截图是证据、OCR 是低成本理解），
而且零依赖、可完全用现有 `view_image` + 审批链复用。

截至 2026-10-26，P1 / P2 / P3a / P4.1 / P4.2（域名策略）/ P6（审计查询）已完成；
P4.2b 用户规则文件与 P4.3 自动放行审计也已完成。P3b 键鼠控制基础版与序列/修饰键已
实现、真机交互待验收。P4 权限模型 v2 的四项全部完成。

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
