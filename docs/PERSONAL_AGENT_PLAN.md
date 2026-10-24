# Personal Agent 升级方案

状态：方案制定完成（2026-10-24），未开工。目标形态：一台常驻机器上的个人代理，手机（飞书）可以监视进度、回合中插话、审批命令并收到完成通知；空闲时可由定时任务唤醒；记忆跨会话沉淀。不做公网中继、不做自研手机端，飞书承担消息入口和通知端。

## 1. 调研依据与边界

| 机制 | Codex Remote Control | Claude Code Remote Control | OpenClaw |
|---|---|---|---|
| 连接方式 | OpenAI 云中继 | Anthropic API 中继 | Gateway 长连 IM 平台 |
| 会话控制 | 远程控制与审批 | 远程控制本地进程 | Gateway 管理会话和通道 |
| 手机能力 | 监视、steer、批准、查看 diff | 监视、steer、批准、中断 | 各通道收发与审批 |
| 主动性 | 远程控制为主 | 远程控制为主 | cron、心跳及记忆整理 |
| 守护方式 | 远程服务配合本地会话 | 本地 `claude` 进程需运行 | systemd/launchd 常驻 Gateway |

这些产品可作为单一控制入口和增量订阅的产品设计参考；不能据此证明本项目的文件锁、工作区并发或崩溃恢复实现正确。对照中的具体产品行为应以对应版本的官方文档为准，issue 和第三方源码分析只作为补充材料。

来源：[Unlocking the Codex harness](https://openai.com/index/unlocking-the-codex-harness/)、[Work with Codex from anywhere](https://openai.com/index/work-with-codex-from-anywhere/)、[Remote connections](https://learn.chatgpt.com/docs/remote-connections)、[Codex App Server](https://learn.chatgpt.com/docs/app-server)、[Codex issue #39974](https://github.com/openai/codex/issues/39974)、[Claude Code Remote Control](https://code.claude.com/docs/en/remote-control)、[Claude Code on mobile](https://code.claude.com/docs/en/mobile)、[OpenClaw 架构](https://docs.openclaw.ai/concepts/architecture)、[OpenClaw 工作机制](https://www.betterclaw.io/blog/how-does-openclaw-work)。

## 2. 现状盘点

| Personal Agent 能力 | 当前实现 | 缺口 / 说明 | 阶段 |
|---|---|---|---|
| 手机审批 | 飞书轮询待审批项、推送卡片、按钮或文本应答唤醒审批（`src/feishu_gateway.py`、`src/approval.py`） | 基础链路已就绪；需在常驻服务下验证超时、断连和服务关闭行为 | 阶段 0 验证 |
| 手机监视 | 飞书发起的回合可流式回写；会话事件 SSE 支持 `after` 增量续传（`src/api_streams.py`） | 缺少统一 watcher、持久投递状态和终态失败事件 | 阶段 1 |
| 回合中插话 | TUI 已有 steer 队列；`agent_loop` 支持 `steer_provider`（`harness.py`） | HTTP/runtime 未暴露；须定义入队与回合结束竞态语义 | 阶段 1 |
| 远程中断 | 当前 `turn_control` 使用进程级 Event（`src/turn_control.py`） | 不是按会话取消；API 回合需要有明确的回合生命周期与取消令牌 | 阶段 1 |
| 定时唤醒 | cron 表、队列和处理循环已存在（`src/cron_scheduler.py`）；CLI 启动时接线（`entrypoint.py`） | 循环不可停止；API lifespan 未接线；触发时间和投递记录不是持久状态 | 阶段 0 |
| 会话并发 | `AgentRuntime` 有进程内会话锁；`SessionEventStore` 有进程内路径锁 | TUI 与 API 跨进程没有共同执行锁；不同会话也可能并发修改同一工作区 | 阶段 0 |
| 记忆提取与合并 | 每个成功回合的后台 housekeeping 已调用 `extract_memories` 和 `consolidate_memories`；合并受阈值控制（`harness.py`、`src/memory.py`） | 无需重复添加定时合并；需解决合并原子性、并发写入和关闭时后台线程收尾 | 阶段 0/2 |
| 心跳/日报 | 尚无默认心跳任务 | 需要稳定任务 ID、时区、错过调度策略、投递语义和只读权限边界 | 阶段 2 |
| TUI 共存 | TUI 与 API 进程共享 `.cache/sessions` | 同一会话和同一工作区都需要明确的跨进程并发策略；TUI 转为 daemon 客户端仍可选 | 阶段 0/3 |

可复用底座：飞书端会话新建/绑定/切换/fork/搜索、审批卡片、`/run`、`/status`、`/logs`、`/stop`、进度卡片、跨会话记忆召回和 SSE `after` 游标。SSE 游标表示事件读取进度，不等同于飞书消息已恰好投递一次。

## 3. 并发与恢复不变量

当前回合执行仍依赖若干进程级状态，包括取消状态、指标、权限门和 cron 协调。`AgentRuntime` 的会话锁只串行同一会话，不会保护同一工作区内的不同会话。

**阶段 0 的初始策略：全局单回合。** 在这些进程级状态完成会话/回合隔离之前，TUI、API、CLI 和 cron 共用一个跨进程非阻塞回合协调器，同一时刻只允许一个 agent 回合执行。竞争者立即得到明确的忙碌结果，不排队等待一个可能很长的模型调用。此限制保守但适合单用户常驻代理，也能避免不同会话同时改同一工作区。将来需要并行时，再以工作区级锁和完全隔离的回合状态替代，不在本阶段提前并行化。

回合协调器之外，会话级变更（重命名、切换工作区、fork、归档、删除等）也必须防止与同一会话回合交错。回合后的异步标题整理及其他会写 session event 的 housekeeping 也必须持有同一会话写锁，或在释放回合协调器前完成；archive/delete 不得与其并发。所有跨进程写入入口使用统一的锁 API 和一致的获取顺序；锁竞争映射为 HTTP `409` 或 TUI 可恢复提示，不能吞掉用户输入。

使用 `exclusive_file_lock` 的 OS advisory lock，而不称作带过期时间的“租约”：锁文件存在不表示锁仍被持有，进程退出时由 OS 释放锁。保留锁文件是正常行为，不用 PID 存活检查推断锁状态。以 Windows `msvcrt` 和 POSIX `flock` 的子进程异常退出测试确认行为。

## 4. 分阶段实施

### 阶段 0：常驻服务与单回合协调（进行中，2026-10-24 完成第一批）

已完成：

- **可控的 scheduler 生命周期**：新增 `src/scheduler_service.py`；两个循环接受 `stop_event`，
  置位后立即退出等待。API lifespan 在拥有默认 runtime 且未设 `HARNESS_SCHEDULER=0` 时启动，
  关闭时停止并有界 join（超时记录 warning）；注入 runtime 的 app 不起服务；重复 start 幂等。
  CLI 入口仍用 daemon 线程直接启动（进程退出即回收），不改其测试 patch 点。
- **统一回合协调器**：新增 `src/turn_coordinator.py`（进程内 RLock + 跨进程
  `.cache/locks/agent-turn.lock`，同线程重入、跨线程/跨进程立即 `TurnBusy`）。已接入
  `AgentRuntime.send`、TUI worker、ANSI REPL 的 `_run_turn`、cron `run_agent_turn_locked`
  与 headless（走 runtime）。cron 先拿槽再消费队列，竞争时任务留在队列里。
- **API 契约**：`POST /v1/sessions/{id}/messages` 在竞争时返回 `409`；流式端点返回
  `event: error` + `code: turn_busy`。
- **TUI 拒绝体验**：拿槽失败时消息退回 Composer 并提示，不写会话事件；`_send_user` 在
  空闲时先拿槽再落块，避免“看起来已发送”。
- **测试**：新增 `tests/test_turn_coordinator.py`（子进程持锁/竞争/崩溃释放/陈旧锁文件、
  runtime 被拒绝时不写事件、cron 任务不丢、服务幂等启停、durable jobs 只加载一次）；
  API 新增 409 与流式 busy 事件、lifespan 启停/禁用/注入 runtime 不启动；
  TUI 新增保留输入与回合结束释放槽。
  原 `test_concurrent_real_turns_use_their_own_tools_prompt_and_checkpoints` 依赖两个真实回合
  并发（与全局单回合冲突），改为“A 在模型调用中时 B 被拒绝、随后 B 串行执行”，保留
  prompt/工具/checkpoint 归属断言。

第二批（2026-10-24）：

- **会话写入保护**：新增 `src/session_locks.py`（`locks/sessions/<id>.lock`）。回合
  （`AgentRuntime.send`、TUI worker、cron）整轮持有会话写锁；rename / workspace / fork /
  archive / delete 非阻塞获取，失败抛 `SessionBusy`（API 映射 409，TUI 提示，清理逐条
  记账跳过）。TUI `/rename`、共享的 `apply_session_cleanup`、回合后 title 写入（带 5s
  超时）都已接锁。
- **回合生命周期对称化**：`AgentRuntime.send` 与 cron 回合显式
  `begin_turn()/end_turn()`，进入时清掉上一个入口遗留的取消位（修复跨测试/跨入口
  的 Esc 误取消），退出时复位。
- **TUI 关闭**：`HarnessApp.wait_for_shutdown()` 置退出标志、请求取消、有界 join worker，
  再 `wait_for_housekeeping(timeout)`；`ui.harness_app.run` 在事件循环结束后调用它。
  worker 的 `call_from_thread` 收口到 `_ui_call`，退出后不再抛噪音。
- **测试**：会话锁竞争（runtime 四个元数据操作、清理记账、TUI rename 提示）、
  关闭等待、API rename/fork 409。

仍欠：

- 真实终端与真实 daemon 验收（手机/TUI 竞争、SIGTERM 关闭、手机审批端到端）。
- 阶段 1 的 turn_id/终态事件、steer/interrupt、watcher、至少一次投递。

### 阶段 1：远程控制闭环（进行中，2026-10-24 完成第一块）

已完成：

- **回合身份与终态事件**：`agent_loop` 生成 `turn_id`，会话流写入
  `turn_started{turn_id, source}`；成功时 `turn_completed` 后紧跟
  `turn_terminal{status='completed'}`，失败/取消时在 finally 记录
  `turn_terminal{status='failed'|'cancelled', error_type}`。`turn_completed` 保留给既有
  进度/测试消费者，watcher 只需消费唯一的 `turn_terminal`。来源已覆盖
  `cli / tui / api / cron / steer`。
- **按回合取消**：`turn_control` 引入 `TurnToken`（turn_id/session/source）与进程内活动
  注册表；同线程重入复用 token，`request_cancel()` 优先命中活动 token，避免旧全局位
  泄漏到下一回合；`end_turn()` 清位。TUI/CLI 的 Esc 仍走同一入口。
- **steer**：`POST /v1/sessions/{id}/steer`。活动回合返回 `queued`（带 turn_id/source），
  在下一次模型请求边界注入；空闲时降级为普通消息返回 `sent`；活动回合不属于本进程
  （例如 TUI 在另一个进程）返回 409，不假装入队。
- **interrupt**：`POST /v1/sessions/{id}/interrupt`，只取消本进程内匹配会话的活动回合，
  无活动回合返回 `idle`。`runtime.send` 把取消收敛为 `TurnCancelled`，普通消息端点
  返回 409，流式端点发 `event: cancelled`。
- **测试**：终态事件（成功/失败/取消）；steer 空闲降级与活动注入；interrupt 取消并
  返回 turn_id；API steer/interrupt/取消流事件；四个 409 路径。

仍然欠缺（阶段 1 剩余）：

- steer 的幂等键（客户端重试去重）与“回合即将结束”的原子归属。
- 跨进程 steer/interrupt（目前只覆盖同进程的 daemon 回合；TUI 回合返回 409）。
- 飞书 watcher（SSE 增量 + 持久游标 + 至少一次投递与重复窗口记录）。
- 无进展提示（只提示“可能卡住”，不推断失败）与来源去重。

### 阶段 2：主动性与安全边界

- **cron 管理**：复用现有 cron 工具，增加 API 与飞书 `/cron` 的 list/create/delete。命令只允许授权用户使用，复用当前飞书白名单和 API bearer 策略；创建、删除结果返回稳定 job ID 和明确权限错误。
- **任务触发状态**：job 配置持久化不代表触发记录持久化。为每次触发计算稳定 occurrence ID，定义本地时区或显式时区、停机期间的 misfire 策略（跳过/补跑/仅补最近一次）、并发时是否合并，以及失败重试上限。确保默认心跳使用稳定 job ID，上线/重启不会重复注册。
- **心跳摘要**：默认每日一次，初始仅只读汇总任务板、最近改动和待审批数量，不自动执行写操作；内容不包含不必要的命令正文、凭据或敏感工具参数。仅在明确授权的定时任务中开放副作用。审批等待必须有超时和通知策略，不能无限占用唯一回合槽。
- **记忆安全**：`extract_memories` 和阈值触发的 `consolidate_memories` 已随成功回合的 housekeeping 执行；不再额外加 cron 重复触发。合并流程已改为「校验 → 暂存 → 先落新文件再删旧文件」：校验 schema、类型、条数与重名，暂存文件回读校验后才替换，失败保留旧记忆；写入已加进程内 RLock 与跨进程 OS 锁，单文件与索引都是临时文件 + 原子替换（2026-10-24 完成）。仍欠：daemon 关闭时等待或记录未完成的 housekeeping。
- **测试**：稳定 job ID 重启不重复注册；occurrence 重启/错过/失败语义；心跳只读约束和副作用审批；无效或空模型输出、写入中途失败、并发提取与合并、服务关闭时 housekeeping 超时恢复。记忆部分的故障测试已完成（`tests/test_memory_safety.py`），调度与关闭部分待开工。

### 阶段 3（可选，单独评估）：TUI 转客户端

TUI attach 到 daemon（SSE 渲染、steer/审批下发），让终端和手机看到同一活动回合。阶段 0 的锁只负责避免并发，不提供 attach、共享实时视图或远程操作能力。待确有日常需求再评估。

## 5. 明确不做

- **公网中继 / QR 配对**：飞书 WS 长连接作为通道；本地 API 保持 loopback 绑定和 bearer token。需注意 loopback token 不替代飞书发送者授权。
- **自研手机 App / Web UI**：飞书卡片和命令覆盖当前需求。
- **向量记忆索引**：个人规模先沿用 Markdown、关键词打分和全文检索。
- **多端写入仲裁**：初期统一拒绝并发回合，不做冲突合并；未来并行须重新设计锁粒度。
- **HTTP 暴露 attachments**：暂无真实用例时不增加远程文件传输面。

## 6. 风险与决策记录

1. **全局单回合的吞吐限制**：这是保护现有进程级状态的初始取舍；记录忙碌拒绝率和持续时间，再决定是否需要回合状态隔离与工作区级并发。
2. **OS 锁语义**：验证 Windows 与 POSIX 的子进程异常退出释放；锁文件残留不作为陈旧锁，不做 PID 推测。
3. **cron 生命周期和协调**：scheduler/worker 必须可停止；cron、API、TUI 使用相同回合协调器，不能只靠各自进程内的 `agent_lock`。
4. **watcher 外部投递**：采用至少一次，发送确认与游标持久化之间存在重复窗口；终态事件应可重放并按 turn 去重。
5. **消息端点等待时间**：`POST /messages` 当前同步等待回合；steer 不会缩短 HTTP 等待，interrupt 仅在回合响应取消后提前返回。长回合优先使用 `/messages/stream`；另行定义客户端超时、断连后回合是否继续，以及是否需要异步 job/status API。
6. **记忆数据安全**：在合并具备验证和原子替换前，不扩大自动合并频率；关闭时需等待或记录未完成的 daemon housekeeping。
7. **产品对照适用范围**：单控制器交互是设计先例，不是本地 session/workspace 文件写入安全性的证据；本项目以自己的跨进程和故障测试为准。

## 7. 验收口径

- **阶段 0**：两个进程争抢全局回合协调器，只有一个成功；同一进程中的 API、TUI、cron 不绕过协调器；竞争者得到明确拒绝，TUI 原文仍可取回；变更会话元数据与活动回合不会交错；杀死持锁子进程后可立即重新获取；重复启动/关闭 API lifespan 不遗留 scheduler 线程。
- **阶段 1**：手机 steer 能影响后续模型轮次，竞态下不会丢失；interrupt 只影响目标活动回合；完成、失败、取消都有持久终态事件；TUI 发起的回合按来源策略通知；watcher 重启可从持久游标续传，允许并记录发送与游标提交之间的重复窗口，不承诺不重放且不漏发。
- **阶段 2**：`/cron` 权限、持久化、时区、misfire 和重启行为符合定义；默认心跳每天只创建一个逻辑 occurrence，内容只读；通知遵循至少一次语义并允许记录到的重复；记忆合并失败或中途崩溃时旧数据完整，连续多会话后索引与文件一致。
- **全程**：自动化测试与真实终端/飞书验收分开记录；按 [UI_TERMINAL_ACCEPTANCE.md](UI_TERMINAL_ACCEPTANCE.md) 留存版本、设备、逐项结果、失败和复测记录。
