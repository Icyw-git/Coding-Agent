# Personal Agent 升级方案

状态：方案制定完成（2026-10-24），未开工。目标形态：一台常驻机器上的个人代理——
手机（飞书）随时监视进度、回合中插话、审批命令、收到完成通知；空闲时能被定时
唤醒自主干活；记忆跨会话沉淀。不做公网中继、不做自研手机端——飞书就是本项目
的 "relay + App"。

## 1. 调研依据（外部实现）

三家参照实现（2026 年在产）的机制对照：

| 机制 | Codex Remote Control | Claude Code Remote Control | OpenClaw |
|---|---|---|---|
| 连接方式 | OpenAI 云中继，不走直连 | Anthropic API 作 TLS 中继 | 单 Gateway 进程长连各 IM 平台 |
| 配对 | QR + `codex remote-control pair` 手动码 | claude.ai 全量登录（API key 不行） | 新设备 ID 需配对批准，默认拒绝未配对发送者 |
| 会话所有权 | 单 controller 响应审批 | 同一时刻只有一台设备拥有会话 | 一个 Gateway 独占一个会话 |
| 手机端能力 | 监视/steer/批准/看 diff | 监视/steer/批准/中断 | 各通道收发 + 审批 |
| 断线 | 重连需全量 hydration（历史 >45MB 失败） | 会话在断网中存活，需本地进程活着 | busy 时队列排队 |
| 主动性 | 无 | 无 | 30 分钟 cron 心跳 + 记忆固化 |
| 守护方式 | 常驻 daemon | 本地 `claude` 进程必须活着 | systemd/launchd |

关键结论：

1. 监视/插话/审批三家做法一致；真正的分界线是**主动性**（定时心跳 + 记忆沉淀）——
   这是 "personal agent" 与 "远程控制编码会话" 的区别。
2. 三家都把**单写者**当铁律，不做多端并发仲裁。
3. Codex 断线重连对整个 thread 做全量 hydration，大历史直接失败——控制面状态
   必须小、订阅必须增量。

来源：[Unlocking the Codex harness](https://openai.com/index/unlocking-the-codex-harness/)、
[Work with Codex from anywhere](https://openai.com/index/work-with-codex-from-anywhere/)、
[Remote connections](https://learn.chatgpt.com/docs/remote-connections)、
[Codex App Server](https://learn.chatgpt.com/docs/app-server)、
[App-Server 协议层源码分析](https://github.com/xiaonancs/codex-source-analysis/blob/main/Part%20II%20Source%20Analysis/21-AppServer%E5%8D%8F%E8%AE%AE%E5%B1%82.md)、
[issue #39974](https://github.com/openai/codex/issues/39974)、
[Claude Code Remote Control](https://code.claude.com/docs/en/remote-control)、
[Claude Code on mobile](https://code.claude.com/docs/en/mobile)、
[OpenClaw 架构](https://docs.openclaw.ai/concepts/architecture)、
[OpenClaw 工作机制](https://www.betterclaw.io/blog/how-does-openclaw-work)。

## 2. 现状盘点（证据）

| personal agent 能力 | 现状 | 缺口 | 阶段 |
|---|---|---|---|
| 手机审批 | 完整：3s 轮询→推送卡片→按钮/y-n→Event 唤醒阻塞回合（`feishu_gateway.py:1246`、`approval.py:246`） | 无 | 已就绪 |
| 手机监视 | 只有"自己发起的回合"有流式回写（`feishu_gateway.py:458`）；别的入口跑的回合看不见 | stream watcher 主动推送 | 阶段 1 |
| 回合中插话 steer | 机制已在 harness 内部（轮间注入 `harness.py:917`），HTTP/runtime 不暴露 | `/steer` 端点 + 每会话队列 | 阶段 1 |
| 远程中断 | `turn_control.py` 是进程内 Event，HTTP 够不到 | `/interrupt` 端点 | 阶段 1 |
| 回合完成通知 | 飞书主动发消息能力齐备（`feishu_gateway.py:176`），但无组件消费事件流去推 | watcher + 游标持久化 | 阶段 1 |
| 定时唤醒 | `cron_scheduler.py` 完整（1s 扫描、忙时轮间注入、空闲代跑），但只在交互 CLI 进程启动（`entrypoint.py:89`），API server lifespan 没有 | lifespan 接线 | 阶段 0 |
| 跨进程会话锁 | 全部锁是进程内的（`agent_runtime.py:310`、`session_store.py:90`） | 文件锁租约 | 阶段 0 |
| 记忆沉淀 | 提取/合并/逐轮召回/跨会话检索都有（`memory.py`） | daemon 模式下定时合并 | 阶段 2 |
| 心跳/日报 | 无 | cron 心跳任务 | 阶段 2 |
| TUI 共存 | TUI 与 API 进程共享 `.cache/sessions`，并发写无仲裁（README:94 自认） | 租约互斥；TUI 转客户端可选 | 阶段 0/3 |

已就绪、可直接吃到的底座：飞书端会话新建/绑定/切换/fork/搜索、审批卡片+按钮、
`/run /status /logs /stop`、流式进度卡片、记忆跨会话召回、SSE `after` 游标断线
续传协议（`api_streams.py:29`，`gateway_sim.py:184` 有现成 follower 实现）。

## 3. 核心架构决定：单写者

三家参照无一例外把「单写者」当铁律（Claude：一台设备拥有会话；OpenClaw：一个
Gateway 独占一个会话；Codex：单 controller 响应审批）。本项目现状是 TUI 进程与
API server 进程共享 `.cache/sessions`，无跨进程仲裁。

**决定：用现有 `exclusive_file_lock`（`file_lock.py:61`）给每个会话加跨进程租约，
而不是发明多端并发仲裁。** 两进程争同一会话时后到者直接拒绝（复用 `set_workspace`
忙时 RuntimeError 的既有模式，`agent_runtime.py:148`）。比"先到先得+消费"的仲裁
简单一个数量级，且三家实践证明够用。

## 4. 分阶段实施

### 阶段 0：常驻化 + 单写者（地基，改动小）

- **API server 启动 cron**：把 `entrypoint.py:89` 的 `cron_scheduler_loop` +
  `queue_processor_loop` 接入 `harness_api.py:71` 的 lifespan（`owns_runtime`
  时才启动，加环境变量开关）。调度设施全在，只是没通电。
- **会话租约**：`AgentRuntime.send` 与 TUI 回合开始时获取会话级
  `exclusive_file_lock`（非阻塞，失败即拒绝），锁文件放 `.cache/locks/sessions/`。
  TUI 与 API 争用立即显式失败，消灭静默并发写。
- **测试**：两进程争锁互斥、daemon 内 cron 到点跑回合、`wait_for_cron_idle`
  在 server 模式下的行为。

### 阶段 1：远程控制闭环（监视 / 插话 / 停止 / 通知）

- **steer 端点**：`POST /v1/sessions/{id}/steer`。`AgentRuntime.send` 增加每会话
  steer 队列并接 `steer_provider`（注入点现成：`harness.py:917` 的轮间注入通道）。
  语义取 OpenClaw：空闲时 steer 自动降级为普通消息（不返回 409，减少客户端分支）。
- **interrupt 端点**：`POST /v1/sessions/{id}/interrupt`。daemon 成为单写者后，
  `turn_control` 的 Event 由 runtime 按会话登记即可，无需跨进程 trick。
- **飞书 watcher**：网关加一条跟随线程，复用 `gateway_sim.py:184` 已验证的 SSE
  follower 模式（`after` 游标 + 断线重连），游标持久化进 `feishu_bindings.json`。
  检测 `turn_completed` / `error` / 长时间无进展 → 主动发卡片。只拉增量，游标
  就是状态（Codex 45MB hydration 教训）。
- **测试**：steer 注入轮间时序、interrupt 取消流式回合、watcher 断线续传
  （mock SSE server）、双入口（cron + 手机）同会话交错。

### 阶段 2：主动性闭环（personal agent 的分界线）

- **cron 管理**：cron 工具已有（`tool_tasks.py:10`），补 HTTP 端点 + 飞书
  `/cron` 命令（list/create/delete）。
- **心跳摘要**：默认每日一次（OpenClaw 30 分钟太吵）cron 任务：汇总任务板 +
  最近改动 + 未处理审批 → 主动发卡。
- **记忆闭环**：补 daemon 常驻下按 cron 触发 `consolidate_memories`
  （`memory.py:318`），把高频事实沉淀进 MEMORY.md（OpenClaw "Dreaming" 的克制版）。
- **测试**：心跳只发一次、合并幂等、飞书命令权限走现有白名单。

### 阶段 3（可选，单独评估）：TUI 转客户端

TUI attach 到 daemon（SSE 渲染 + steer/审批下发），实现"终端和手机看到同一个
会话"。成本高（TUI 刚完成 mixin 化），默认不做——阶段 0 的租约已保证 TUI 本地
独占时行为不变。等真出现"两处同时用一个会话"的日常需求再上。

## 5. 明确不做（及理由）

- **公网中继 / QR 配对**：飞书 WS 长连接就是现成的穿透通道；本地 API 只绑
  127.0.0.1 + token（`harness_api.py:110` 已有 bearer + 限速）。
- **自研手机 App / Web UI**：飞书卡片流已覆盖监视/审批/插话。
- **向量记忆索引**：markdown + 关键词打分 + 全文检索对个人规模够用。
- **多端并发仲裁**：用单写者租约替代。
- **HTTP 暴露 attachments**：远程场景暂无真实用例。

## 6. 风险清单

1. **cron 搬进 API server 后与 agent_lock 的交互**：`queue_processor` 的非阻塞
   抢锁（`cron_scheduler.py:211`）语义在 server 里不变，但要补"cron 回合与手机
   回合交错"的回归。
2. **watcher 误推**：回合失败重试时避免卡片轰炸——按会话合并推送 + 抖动。
3. **租约陈旧锁**：进程崩溃留下锁文件——验证 `exclusive_file_lock` 在 Windows
   （msvcrt）下的崩溃释放行为，必要时加持有者 PID 存活检查。
4. **`POST /messages` 600s 挂死**：steer/interrupt 落地后大幅缓解；远程长回合
   建议走 `/messages/stream`。

## 7. 验收口径

- 阶段 0：手机发起回合期间，TUI 打开同一会话被显式拒绝；daemon 进程内 cron
  到点自主跑完回合并落会话事件。
- 阶段 1：手机在回合运行中发消息能改变后续轮次（steer）；能远程停掉一个流式
  回合；TUI 启动的回合完成后飞书收到主动卡片；watcher 断电重启后从游标续传，
  不重放不漏事件。
- 阶段 2：飞书 `/cron` 增删任务即时生效；每日心跳只发一次；连续多会话后
  MEMORY.md 出现合并沉淀。
- 全程：真实终端/手机端逐项验收记录沿用 [UI_TERMINAL_ACCEPTANCE.md](UI_TERMINAL_ACCEPTANCE.md)
  的留痕方式。
