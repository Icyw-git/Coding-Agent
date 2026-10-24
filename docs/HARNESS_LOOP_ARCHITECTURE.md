# Harness Loop 架构与循环逻辑

本文以当前仓库代码为准，说明 Harness 从收到用户消息到最终回答、工具执行、压缩、恢复、后台任务、cron、MCP、子代理、teammate 协作、任务认领和 worktree 隔离的完整循环。

## 1. 代码入口与状态边界

主入口是 [`harness.py:330`](harness.py:330) 的 `agent_loop(messages, session_store=None)`。

相关模块职责如下：

| 模块                  | 职责                                                                                             |
| --------------------- | ------------------------------------------------------------------------------------------------ |
| `harness.py`          | 主 agent loop、请求构造、工具编排、cron 注入、会话恢复入口                                       |
| `agent_core.py`       | hooks、记忆、上下文压缩、transcript 快照、记忆注入                                               |
| `tools.py`            | 工具接线、路径边界与 `tool_registry`                                                             |
| `tool_schemas.py`     | OpenAI tool schema（顺序是契约）；分域实现见 `tool_*.py`                                         |
| `session_store.py`    | 追加式 session JSONL、OpenAI 历史重放、pending tool 检测、checkpoint/rewind 事件                 |
| `tool_recovery.py`    | 中断工具调用的恢复分级（read-only / idempotent / destructive）与恢复提示文本                     |
| `checkpoints.py`      | 可回溯节点：messages 快照、文件 pre-image、回溯计划与执行、节点回收、旧明文快照迁移（见 `CHECKPOINT_DESIGN.md`） |
| `session_cleanup.py`  | 会话归档/恢复/永久删除，连同任务引用的子代理与 teammate 日志一起移动；孤儿子日志回收           |
| `recovery.py`         | 429/overload 重试、备用模型、输出截断和上下文超限判断                                            |
| `background_task.py`  | 慢工具后台执行和完成通知收集                                                                     |
| `cron_scheduler.py`   | cron 时间匹配、持久化、队列生产和队列处理线程                                                    |
| `mcp_bridge.py`       | MCP server 连接、异步事件循环、动态工具注册                                                      |
| `subagent.py`         | `task` 工具启动的短生命周期子代理循环                                                            |
| `subagent_events.py`  | 子代理/teammate 独立事件日志与父会话引用事件（best-effort，不改变运行结果）                      |
| `agent_teams.py`      | 常驻 teammate 线程、邮箱通信、计划审批、任务工具和 worktree 上下文                               |
| `task_system.py`      | 持久化任务板、依赖、认领、完成和解锁（跨进程文件锁）                                             |
| `file_lock.py`        | Windows/POSIX 通用独占文件锁，供任务板等小型读改写存储使用                                        |
| `autonomous_agent.py` | teammate 空闲轮询、收消息、自动认领任务                                                          |
| `team_protocols.py`   | shutdown/plan approval 协议状态机                                                                |
| `skill_system.py`     | skill 扫描、目录展示和按需加载                                                                   |
| `worktree.py`         | git worktree 创建、任务绑定、变更保护和保留/删除                                                 |
| `hooks.py`            | 工具权限、日志、大输出、用户提交和停止 hook                                                      |

主循环中的 `messages` 是 OpenAI canonical history；system prompt 和相关记忆通过请求副本注入，不应混入 canonical history。

## 2. 主 agent loop

### 2.1 初始化

调用 `agent_loop()` 时：

1. 如果没有传入 `SessionEventStore`，创建新的 session 日志对象。
2. 将初始消息写入 `.cache/sessions/session_<id>.jsonl`。
3. 调用 `update_context()`（`src/prompt_assembly.py`）收集工作目录、工具、记忆索引、skills 和项目说明（`AGENTS.md`）。
4. 创建 `RecoveryState`。
5. 触发 `UserPromptSubmit` hook。
6. 调用 `get_system_prompt()` 组装或读取缓存的 system prompt。
7. 调用 `load_memories(messages)`，只在会话开始筛选一次相关记忆。

对应代码：[`harness.py:330-348`](harness.py:330)。

### 2.2 每轮循环

主循环结构如下：

```text
while True:
  保存当前压缩前快照
  工具结果预算压缩
  消息数量裁剪
  旧工具结果微压缩
  必要时 LLM 摘要压缩
  注入 todo 提醒
  消费 cron 队列
  构造 system + memories + messages 请求副本
  调用 LLM
  处理错误/截断/普通回复/tool_calls
```

对应代码：[`harness.py:350-495`](harness.py:350)。

## 3. 上下文压缩循环

入口是 [`harness.py:267`](harness.py:267) 的 `pre_compact_messages()`，顺序固定为：

```text
tool_result_budget
    ↓
snip_compact
    ↓
micro_compact
    ↓
estimate_size > CONTEXT_LIMIT ? compact_history : 继续
```

### 3.1 工具结果落盘

[`agent_core.py:449`](agent_core.py:449) 的 `tool_result_budget()` 检查工具结果总量。大型结果由 `persist_large_output()` 写入 `.cache/tool_results/<tool_call_id>.txt`，消息内替换成路径和预览。

### 3.2 消息裁剪

[`agent_core.py:388`](agent_core.py:388) 的 `snip_compact()` 在消息数超过 50 时保留头尾，并插入 `[snipped ...]` 标记。裁剪点会避开 `role=tool`，防止产生孤儿工具结果或未闭合 tool call。

### 3.3 旧工具结果微压缩

[`agent_core.py:427`](agent_core.py:427) 的 `micro_compact()` 保留最近 `KEEP_RECENT` 个结果，更早且较长的结果改成可重新执行提示。LLM 摘要后通过 `skip_micro_rounds` 暂时跳过几轮，保护新结果。

### 3.4 LLM 摘要压缩

[`agent_core.py:498`](agent_core.py:498) 的 `compact_history()`：

1. 调用现有 `write_transcript()` 导出完整消息快照到 `.cache/transcripts/`。
2. 调用 `summarize_history()` 生成摘要。
3. 用 `[Compacted]` user 消息替换旧历史。
4. 通过 session store 追加 `compaction` 事件。

system 消息不保存在 compacted canonical history 中，由 `inject_memories()` 在每次 API 请求前统一前置。

## 4. Todo 与 cron 注入

### 4.1 Todo 提醒

主循环维护 `rounds_since_todo`。工具轮没有调用 `todo_write` 时递增，达到 8 轮后追加：

```text
<reminder>Update your todos.</reminder>
```

todo 清单按会话隔离，`todo_write` 整表替换并写入 `todos` 事件；会话恢复时
`restore_todos(session_store)` 从最后一个事件恢复内存状态，`todo_read` 读取当前清单。

代码位置：[`harness.py:359-366`](harness.py:359)。

### 4.2 会话内 cron 拉取

每轮调用 `consume_cron_queue()`，将到点任务转为普通 user 消息：

```text
[Scheduled] <prompt>
```

代码位置：[`harness.py:368-376`](harness.py:368)。

## 5. 请求构造与记忆注入

[`agent_core.py:519`](agent_core.py:519) 的 `inject_memories()` 返回副本：

```text
request_messages =
  [system prompt]
  + [第一条 user 消息前置 relevant memories]
  + [canonical messages]
```

这样可以同时保证：

- API 请求拥有 system prompt；
- 模型看到相关记忆；
- 原始消息可用于快照、压缩、重放和记忆提取；
- 不会重复叠加 system prompt。

system prompt 的分段组装（身份 / 工具 / 工作区 / 技能 / 规则 / 项目说明）与缓存入口在
[`src/prompt_assembly.py`](src/prompt_assembly.py)：`update_context()` 只采集随回合变化的值，
`get_system_prompt()` 按 context 指纹决定复用还是重拼；记忆只追加在请求末尾，不入主前缀。

## 6. LLM 调用与错误恢复

API 调用在 [`harness.py:386-409`](harness.py:386) 中执行。

### 6.1 错误分类与重试

[`recovery.py`](recovery.py) 的 `classify_error()` 先把供应商各异的异常归一为
`context_length / rate_limit / overloaded / server / timeout / network /
auth / invalid_request / unknown`，`with_retry()` 只对可重试类别做指数退避：

- 最多 10 次；
- 初始约 0.5 秒，最大约 32 秒，加随机 jitter；
- 优先使用响应的 `Retry-After`（上限 60 秒）；
- 连续 3 次 overload 或重试耗尽时，按 `fallbacks.main` 切换模型；旧
  `FALLBACK_MODEL_ID` 等价于同 provider 回退，跨 provider 需显式开启；
- 流式响应已输出文本后不再重试，避免重复输出。

### 6.2 上下文超限

如果错误匹配 `prompt_too_long`、`context_length_exceeded` 等：

1. 第一次触发 `reactive_compact()`。
2. 保存 transcript，摘要旧历史，保留最近工作现场。
3. 追加 session `compaction` 事件。
4. 重试一次。
5. 再次失败则抛出异常。

### 6.3 输出截断

如果 `finish_reason == 'length'`：

1. 将 `RecoveryState.has_escalated` 设为真。
2. 下一轮使用更大的输出 token 上限。
3. 追加 continuation prompt。
4. 回到主循环重新调用模型。

代码位置：[`harness.py:417-422`](harness.py:417)。

## 7. 普通回答结束路径

当 `finish_reason != 'tool_calls'` 时：

1. 触发 `Stop` hook。
2. 如果 hook 返回继续指令，追加 user 消息后重新循环。
3. 打印回答。
4. 用 `write_transcript()` 导出完整快照。
5. 追加 `turn_completed` session 事件。
6. 从 `pre_compress` 快照调用 `extract_memories()`。
7. 达到阈值时调用 `consolidate_memories()`。
8. 返回最终文本。

代码位置：[`harness.py:425-442`](harness.py:425)。

记忆系统本身位于 [`agent_core.py:195-342`](agent_core.py:195)，分为召回、提取、合并三步。

## 8. 工具调用路径

当 LLM 返回 `tool_calls`：

```text
assistant message 写入 canonical history
    ↓
逐个 tool_call:
  写 tool_started session event
  PreToolUse hooks
  工具权限/注册检查
  前台或后台执行
  PostToolUse hooks
  生成 role=tool 消息
  写入 session event
    ↓
收集后台结果
    ↓
tool messages 加回 canonical history
    ↓
更新 context/system
    ↓
下一轮 LLM
```

执行入口是 [`harness.py:285`](harness.py:285) 的 `exec_tool_call()`。

工具被阻止或不存在时仍返回一条 `role=tool` 错误结果，保证 OpenAI tool call/result 配对完整。

## 9. `compact` 特殊工具

`compact` 在 `tool_schemas.py` 暴露给模型，但不放入普通 `tool_registry` 执行。

在 [`harness.py:450-475`](harness.py:450) 中：

1. compact 之前的 tool call 正常执行。
2. 遇到 compact 后立即调用 `compact_history()`。
3. 已执行的工具结果转为普通文本附加到摘要。
4. 删除原 assistant tool-call 上下文，避免孤儿 tool 消息。
5. `continue` 重新让模型规划。

compact 后面尚未执行的工具不会由旧响应强行执行，而是等待模型下一轮重新产生。

## 10. 后台工具循环

[`background_task.py:18-23`](background_task.py:18) 会将显式标记或包含 install/build/test/deploy/compile 等关键词的 bash 放到后台线程。

后台执行流程：

1. `start_background_task()` 分配 `bg_<id>`。
2. daemon worker 执行实际 handler。
3. 完成后写入 `background_results`。
4. 主循环下一次工具轮调用 `collect_background_results()`。
5. 完成结果以 `<task_notification>` user 消息注入。

后台通知不能使用 `role=tool`，因为原始 assistant tool call 已经结束，会造成孤儿结果。

## 11. Session JSONL 恢复与快照

### 11.1 事件日志

[`session_store.py:15`](session_store.py:15) 的 `SessionEventStore` 是恢复来源，写入：

- `message`：OpenAI 原生消息；
- `tool_started`：工具意图已经发出；
- `mcp_call`：一次 MCP 调用的 server、工具、耗时、状态与错误类型（不含参数）；
- `subagent_started` / `subagent_finished`：子代理运行的轻量引用（id、描述、状态、轮数、
  耗时、子日志相对路径），不进入历史重放；
- `compaction`：历史已被摘要替换；
- `checkpoint`：一个可回溯节点已创建（只记录引用，快照在 `.cache/checkpoints/<session_id>/`）；
- `rewind`：用户回溯到某节点，重放时用该节点快照替换此前历史；
- `turn_completed`：一轮正常完成。

每条事件包含版本、序号、session id、时间戳，并使用追加写入、flush、fsync。

### 11.2 重放

`rebuild_openai_history()` 从事件流重建 canonical history；遇到 compaction 时以摘要替换此前运行历史。`validate_openai_history()` 检查 assistant tool calls 与 role=tool 的配对。

### 11.3 恢复入口

[`harness.py:498`](harness.py:498) 的 `resume_agent_loop(session_path)`：

1. 打开 session JSONL。
2. 在闭合 pending calls 之前收集 recovery 提示（`tool_recovery` 事件一旦写入，
   `pending_tool_calls()` 看到的就是已闭合历史）。
3. 重建 OpenAI 消息并写入 synthetic tool results。
4. 追加 recovery 提示。
5. 继续进入 `agent_loop()`。

恢复不会自动重跑任何工具，只按三类给出下一条行动建议：

- `read-only`：读文件/检索/只读 bash 等，没有持久副作用，可按需重复；
- `idempotent`：整文件 `write`、`todo_write`、带状态保护的 claim/complete 等，
  先核对状态，再决定是否重试；
- `destructive`：`edit`/`apply_patch`、发消息、创建任务、消费 inbox 及未知/MCP
  工具，可能已部分生效或重复副作用，必须先检查当前状态，禁止盲重试。

分类实现集中在 [`tool_recovery.py`](tool_recovery.py)，只读取工具名与参数，不执行工具。
synthetic tool result 仍保留 “outcome is unknown”，并附带对应类别建议。

### 11.4 快照导出

现有 [`agent_core.py:471`](agent_core.py:471) 的 `write_transcript()` 继续作为完整消息快照导出，不承担崩溃恢复职责。

```text
.cache/sessions/    追加事件流：恢复、重放
.cache/transcripts/ 完整快照：审计、导出、人工复盘
```

## 12. MCP 工具接入循环

MCP 入口是 [`mcp_bridge.py:76`](mcp_bridge.py:76) 的 `register_mcp_tools()`。

### 12.1 启动阶段

`harness.py` 的 `__main__` 在启动线程后调用：

```python
TOOLS.extend(register_mcp_tools(tool_registry))
```

MCP bridge：

1. 从 `MCP_SERVERS` 或 `MCP_SERVERS_FILE` 加载配置。
2. 为 MCP 创建常驻 asyncio event loop 线程。
3. 通过 `run_coroutine_threadsafe()` 连接 server。
4. 获取 MCP tool 列表。
5. 将 MCP `inputSchema` 转换成 OpenAI function schema。
6. 为每个工具生成同步 wrapper，注册到 `tool_registry`。

### 12.2 调用阶段

对主 agent、subagent、teammate 来说，MCP 工具和普通工具一样：

```text
LLM tool_call
  -> tool_registry[name]
  -> MCP sync wrapper
  -> asyncio event loop
  -> MCPclient.call_tool()
  -> 同步等待结果
  -> role=tool 回填
```

MCP 失败会被 wrapper 转成 `MCP error: ...` 字符串，再由主循环作为 tool result 交给模型。

### 12.3 调用遥测

每个 MCP sync wrapper 在返回前记录一条 telemetry，字段为：

```text
server        MCP server 名称
tool          MCP 工具名
duration_ms   从投递到拿到结果的墙钟耗时
status        ok | error
error_type    timeout | not_connected | server_error | 异常类名
```

主 agent 在 `_run_tool_batch()` 里通过 `_mcp_telemetry_scope()` 打开 thread-local
sink，把事件追加为 session JSONL 的 `mcp_call`；并行批次在每个 worker 线程内各自
打开 sink，避免跨线程丢失。teammate/subagent 或未加载 MCP bridge 的进程没有 sink，
调用照常执行、只是不写事件。事件不记录参数，sink 抛错也不会改变工具结果。

## 13. 短生命周期 subagent

`task` 工具由 [`subagent.py:19`](subagent.py:19) 的 `spawn_subagent()` 实现。

它不是独立线程，而是当前主 agent 工具调用期间同步执行的嵌套 loop：

```text
主 agent 调用 task
  ↓
创建 subagent system + user messages
  ↓
最多 30 轮 LLM 调用
  ↓
subagent 自己执行工具
  ↓
tool result 回填 subagent history
  ↓
subagent 返回最终文本
  ↓
主 agent 收到 task 的 role=tool 结果
```

subagent 的工具面由 `_subagent_surface()` 按当前实时注册表构造（含后注册的 MCP 工具），
没有复用主循环的 recovery retry 和记忆提取。

每次运行由 [`subagent_events.py`](subagent_events.py) 的 `SubagentRecorder` 写一份独立
事件日志：

```text
.cache/sessions/subagents/session_subagent_<id>.jsonl
  message             system / user / assistant / tool 消息，可 rebuild
  subagent_started    描述
  subagent_finished   status / turns / duration_ms / error_type
```

父会话只追加 `subagent_started` / `subagent_finished` 两个小事件并带子日志相对路径；
`/sessions` 只扫顶层 `session_*.jsonl`，内部运行不会出现在列表里。写入失败（目录不可写、
磁盘满）会被吞掉，子代理继续执行；状态取值 `completed` / `limit` / `error` / `cancelled`。
子日志不是主 agent 的 canonical history，也不会被 checkpoint/rewind 记录。

父会话归档时，`SessionArchive` 读取这些引用事件，把 `subagents/`、`teammates/` 下的日志
一并移入归档目录；恢复时移回原位，永久删除时随归档一起清理。路径必须位于会话存储
目录内，链接/重解析点和未知文件会被拒绝。

## 14. 常驻 teammate loop

teammate 由 [`agent_teams.py:215`](agent_teams.py:215) 的 `spawn_teammate_thread()` 创建 daemon 线程。

### 14.1 启动

1. 检查同名 teammate 是否已存在。
2. 在 Lead 线程创建 `TeammateRecorder`（父会话 store 显式传入，因为 ContextVar 不跨线程），
   写 `teammates/session_teammate_<name>_<id>.jsonl` 和父会话引用事件。
3. 创建线程和 teammate system prompt。
4. 构建 teammate 专用工具集。
5. 初始化 worktree 上下文 `wt_ctx`。
6. 召回记忆，但不提炼全局记忆。
7. 进入常驻循环。

### 14.2 每轮 teammate 循环

```text
claim 自己的 mailbox（不删除）
  ↓
处理 shutdown / plan approval 协议
  ↓
普通消息包装成 <inbox>，然后 ack
  ↓
执行和主循环相同的预压缩
  ↓
注入记忆并调用 LLM
  ├─ 普通文本：进入 idle_poll
  └─ tool_calls：执行工具，继续下一轮
```

代码位置：[`agent_teams.py:386`](agent_teams.py:386)。

每轮新增的 inbox 注入、assistant 消息、tool 结果和图片附件都写入独立日志；结束事件由
`finally` 保证恰好一次，状态为 `completed` / `error` / `cancelled`，并带轮数、耗时和
错误类型。记录失败只丢日志，不改变 teammate 的汇报与线程清理。

teammate 工具执行时：

- `hooks=False`，因为 teammate 线程不能安全交互询问用户；
- `background=False`，因为 teammate 本身已经是后台线程，不再嵌套后台 worker；
- bash 由 `safe_bash()` 额外拒绝 destructive 命令；
- read/write/edit 根据 `wt_ctx` 在绑定 worktree 中执行。

## 15. 消息总线与协议

### 15.1 mailbox

[`agent_teams.py:29`](agent_teams.py:29) 的 `MessageBus` 使用：

```text
.mailbox/<agent>.jsonl
```

`send()` 追加 JSONL，并为每条消息分配 `msg_<uuid>`；无 id 的旧消息在读取时按原始行内容
生成稳定的 `legacy-<hash>` id。

消费分两步，提供 at-least-once 语义：

- `claim_inbox(agent)`：返回未确认消息，不修改邮箱；重复调用会重放同一批消息；
- `ack(agent, ids)`：按 id 删除消息；重写邮箱时先写临时文件再原子替换，并用跨进程
  `exclusive_file_lock`（`<agent>.lock`）包住 send/claim/ack，避免并行会话丢消息。

`replay_unacked()` 是 `claim_inbox()` 的显式别名；`read_inbox()` 保留旧的「读取后立即
确认」语义，供不关心重放的调用方使用。teammate、idle_poll 和 Lead 的 `check_inbox`
都改为 claim 后 ack：进程在「消息已注入对话、尚未 ack」之间退出时，重启后会再次收到。
重复投递是设计代价，协议处理（shutdown/plan response）本身幂等。

### 15.2 Lead -> teammate

Lead 可通过工具：

- `send_message`：普通消息；
- `request_shutdown`：优雅关闭；
- `request_plan`：要求 teammate 提交计划。

### 15.3 teammate -> Lead

teammate 可调用：

- `send_message`：报告进展和结果；
- `submit_plan`：提交计划，生成 `plan_approval_request`。

Lead 再通过 `review_plan` 回传批准或拒绝。

协议状态存储在 [`team_protocols.py:31`](team_protocols.py:31) 的 `pending_requests` 中；审批结果通过 request id 匹配。

### 15.4 shutdown

Lead 发送 `shutdown_request` 后，teammate 收到消息：

1. 回复 `shutdown_response`；
2. 返回 `shutdown`；
3. 退出自身循环；
4. 向 Lead 发送最终 summary；
5. 从 `active_teammates` 移除。

## 16. 任务创建、分配、认领和完成

### 16.1 任务持久化

[`task_system.py:15`](task_system.py:15) 定义任务字段：

```text
id
subject
description
status: pending | in_progress | completed
owner
blockedBy
worktree
claimed_at
lease_expires
attempts
```

每个任务保存为 `.cache/tasks/task_<id>.json`。

### 16.2 创建

`create_task()` 在 `task_transaction()` 内生成唯一 id，状态设为 `pending`，写入任务文件。

任务可指定 `blockedBy`，表示必须等待前置任务完成。

### 16.3 认领

`claim_task(task_id, owner, lease_seconds=3600)` 在 `task_transaction()` 内执行：

1. 加载任务。
2. 状态为 `pending`，或 `in_progress` 且租约已过期（可回收）；其他状态拒绝。
3. 检查所有 `blockedBy` 任务存在且为 `completed`。
4. 写入 owner、`claimed_at`、`lease_expires`，`attempts += 1`。
5. 状态改为 `in_progress`。
6. 立即持久化。

没有 `lease_expires` 的旧 in_progress 任务视为失效（升级重启后原 owner 已不在），
可被重新认领，返回信息会标注 `recovered expired lease`。租约是固定的 1 小时，不做
自动续期由持有者负责：teammate 循环每轮开始调用 `renew_lease(task_id, owner=name)`
刷新 `lease_expires`（不改动 `claimed_at`/`attempts`，过期排序仍按首次认领时间）。
不续租的持有者——线程崩溃或超过 1 小时没有任何 LLM 轮——任务照常过期可回收。

`task_transaction()` 同时持有进程内 `RLock` 和 `.cache/tasks/.tasks.lock` 上的
跨进程独占锁；`save_task()` 复用外层事务，不会重复加锁。这样两个进程不会同时读到
`pending` 后各自写回。锁是 advisory 的，进程退出时由 OS 释放。

### 16.4 完成

`complete_task(task_id, owner=None)` 同样在 `task_transaction()` 内：

1. 校验任务处于 `in_progress`；传入 owner 时必须与当前 owner 一致。
2. 将状态设为 `completed`，清空租约。
3. 持久化任务。
4. 扫描所有 pending 任务。
5. 找出依赖已满足的任务。
6. 在返回信息中报告被解锁任务。

teammate 的 claim/complete handler 传入自己的名字，避免租约被回收后旧 owner 误完成
新 owner 的任务；主 agent 的工具不传 owner，保持原有行为。

### 16.5 teammate 自动认领

teammate 在普通回答后进入 `idle_poll()`，默认每 5 秒检查一次，最长等待 60 秒：

1. 读取 mailbox。
2. 处理 shutdown/plan 协议。
3. 普通消息唤醒工作。
4. `claim_next_task(name)` 在事务内重新扫描候选：
   - pending 任务按创建顺序优先；
   - 其次是与 owner 无关的过期租约任务，按 attempts、claimed_at、id 排序；
   - 逐个调用 `claim_task()`，直到某一候选认领成功。
5. 如果任务绑定 worktree，更新 `wt_ctx['path']`，并记下 `wt_ctx['task_id']`。
6. 注入 `<auto-claimed>...</auto-claimed>`，回到 LLM 工作轮。

代码位置：[`autonomous_agent.py:29-78`](autonomous_agent.py:29)。

这一步没有中央调度器：teammate 仍是 pull 模型，但候选顺序固定（FIFO + 过期任务重试），
认领在跨进程事务内完成；并发 teammate 不会同时拿到同一任务，也不会因一次竞争失败
白等一个轮询周期。当前没有按 owner 负载或任务优先级加权。

### 16.6 租约续期

teammate 循环每一轮开始（含工具轮与空闲轮）调用 `_renew_task_lease()`：`wt_ctx`
里记着当前持有任务的 id（claim 工具与 idle 自动认领都会写入，complete 清空），
非空就调 `task_system.renew_lease(task_id, owner=name)` 把租约刷满 1 小时。
续租被拒（任务被回收/已完成）或抛异常只打印告警，不打断工作；真正的保护在
`complete_task` 的 owner 校验——被抢走的任务无法被旧持有者标记完成。

## 17. Worktree 隔离循环

### 17.1 创建和绑定

[`worktree.py:54`](worktree.py:54) 的 `create_worktree()`：

1. 校验名称。
2. 执行 `git worktree add`。
3. 可选绑定 `task_id`。
4. 在 `.worktrees/events.jsonl` 记录 create。

### 17.2 teammate 认领后切换

teammate 的 `run_claim_task_t()` 认领成功后加载任务：

```python
wt_ctx['path'] = WORKTREES_DIR / task.worktree
```

之后 bash/read/write/edit wrapper 都把 `cwd` 指向该 worktree。

### 17.3 完成后清空

`complete_task` 成功后 `wt_ctx['path'] = None`，teammate 离开 worktree。

### 17.4 删除保护

`remove_worktree()` 默认检查：

- 未提交文件数；
- 未推送 commit 数。

存在改动时拒绝删除，要求 `keep_worktree` 保留审核或显式 `discard_changes=true` 强制删除。

## 18. Skills 生命周期

`scan_skills()` 扫描两处 `skills/*/skill.md` 并解析 frontmatter，合并成 `SKILL_REGISTRY`：
`APP_DIR/skills`（随发行版分发的内建技能）与 `SKILLS_DIR`（当前工作区的项目技能）。
同名技能由工作区版本覆盖；两目录解析到同一路径时只扫一次。

扫描按 manifest 的 mtime/size 做签名，签名不变时是空操作；`list_skills()` / `load_skill()`
调用前以及 CLI 切换工作区（`set_workspace`）后都会重扫，因此编辑 skill.md 无需重启。

system prompt 通过 `list_skills()` 展示技能目录；模型需要具体规则时调用 `load_skill(name)` 获取 skill body。

代码位置：[`skill_system.py:13-100`](skill_system.py:13)。

这是一种“目录预加载 + 内容按需加载”模式：技能索引进入 system prompt，完整技能内容不默认塞入上下文。

## 18.1 项目说明（AGENTS.md）

`update_context()` 额外调用 `project_prompt_section()`，把工作区根目录的说明文件交给
`project_context.load_project_instructions()`。优先级为 `AGENTS.override.md` → `AGENTS.md` →
`CLAUDE.md`（大小写不敏感），只读工作区根目录、不向上查找，逃出工作区的符号链接忽略。

读取上限 32 KiB（截断到最近换行并标记 `truncated`），缓存键是 `(解析后路径, mtime_ns, size)`，
所以编辑文件后下一轮自动生效；`HARNESS_PROJECT_CONTEXT=off` 关闭整条通道。

组装结果作为 `project` 段落追加在 `rules_main` 之后（子代理追加在 skills 段之前），
提示词里明确它不能覆盖安全与审批规则——工作区文件是仓库可控内容，不是可信指令源。
它属于 system prompt，不写入 canonical history，也不产生事件。

## 19. Hooks 生命周期

hooks 注册表在 [`agent_core.py`](agent_core.py) 中，实际注册位于 [`hooks.py:60-70`](hooks.py:60)。

主循环相关事件：

```text
UserPromptSubmit
  -> 用户输入进入 loop 前

PreToolUse
  -> 工具执行前

PostToolUse
  -> 工具执行完成后

Stop
  -> 模型准备结束会话时
```

当前 hook 包括：

- 工具是否注册检查；
- dangerous command 硬拒绝；
- destructive command 用户确认；
- workspace 越界确认；
- 工具执行日志；
- 大输出提示；
- stop 时的工具数量统计。

## 20. 进程级 cron 与主循环的并发关系

程序启动时：

```python
threading.Thread(target=cron_scheduler_loop, daemon=True).start()
threading.Thread(target=queue_processor_loop, args=(run_agent_turn_locked,), daemon=True).start()
```

### 20.1 cron scheduler

[`cron_scheduler.py:172`](cron_scheduler.py:172) 每秒检查任务是否到点，命中后放入 `cron_queue`。

### 20.2 queue processor

[`cron_scheduler.py:206`](cron_scheduler.py:206) 每 0.2 秒：

1. 检查队列是否为空。
2. 非阻塞获取 `agent_lock`。
3. 双重检查队列。
4. 调用 `run_agent_turn_locked()`。
5. finally 释放锁。

主会话使用同一个 `agent_lock`，所以 cron turn 不会与主会话并发调用 LLM。

## 21. 一次完整工具轮的时序

```text
用户消息
  ↓
UserPromptSubmit
  ↓
load_memories
  ↓
LLM assistant(tool_calls)
  ↓
session append assistant message
  ↓
tool_started event
  ↓
PreToolUse
  ↓
前台 handler / MCP wrapper / 后台 worker / teammate handler
  ↓
PostToolUse
  ↓
session append role=tool
  ↓
collect_background_results
  ↓
更新 messages/context/system
  ↓
下一轮压缩和 LLM 调用
```

## 22. Checkpoint / Rewind 节点

设计细节见 [`CHECKPOINT_DESIGN.md`](CHECKPOINT_DESIGN.md)，这里只描述它在循环里的位置。

### 22.1 节点产生

```text
while True 开头
  ↓
begin_turn(messages, session_store)     # 快照 messages + git 基线，状态 open
  ↓
...(LLM 调用、工具执行)...
  ├─ write / edit 落盘前
  │    ↓
  │  capture_pre_image(file)            # 首次修改时把 pre-image 存进当前节点
  ↓
turn_completed
  ↓
seal()                                  # 记录每个文件写入后的 hash，状态 sealed
```

上一轮如果是异常退出的（节点停在 `open`），下一次 `begin_turn` 会先把它 seal，不会留下悬挂节点。

模型显式调用 `checkpoint` 工具时，loop 会先 seal 当前节点，再以 `trigger=manual` 开一个新节点，
并把 `Checkpoint created: <cp_id>` 作为 `role=tool` 结果回填（和 `compact` 一样属于 loop 行为，
不进 `tool_registry`）。

### 22.2 事件流

| 事件         | 写入时机           | 重放语义                                                        |
| ------------ | ------------------ | --------------------------------------------------------------- |
| `checkpoint` | `begin_turn`       | 不改变历史，只记录节点引用与 `through_seq`                      |
| `rewind`     | 用户执行 `/rewind` | 用节点快照替换此前历史，并追加 `[Rewound to <cp_id>]` user 消息 |
| `compaction` | 压缩               | 用摘要替换此前历史（原有行为）                                  |

`checkpoint` 与 `rewind` 的快照本体存在 `.cache/checkpoints/<session_id>/<cp_id>/`，不进 session JSONL；
`rewind` 事件只带 `snapshot_path`，`session_store` 因此不需要知道 checkpoint 目录布局。

### 22.3 回溯入口

```text
/checkpoints                列出节点（* 标记当前 head，files=N 表示该节点是否带 pre-image）
/checkpoint [label]         手动打点
/rewind last                回溯到「最近一个有文件改动的节点」（默认引用）
/rewind head                回溯到最新节点（可能只回滚会话）
/rewind <cp_id>             回溯到指定节点
/rewind <cp_id> --session   只回滚会话
/rewind <cp_id> --force     冲突时强制覆盖
/rewind <cp_id> --yes       跳过确认
/checkpoint-gc              按保留策略回收旧节点
```

节点按 loop 迭代生成，一个用户回合常有两个节点（改文件的那次迭代 + 只输出回答的那次迭代），
后者不带任何 pre-image，所以 `last` 与 `head` 特意区分开。

这些命令由 [`harness.py`](harness.py) 的 `handle_command` 处理，入口是 `__main__` 里的最小 REPL
`run_repl`：普通输入追加 user 消息后调用 `agent_loop(messages, session_store=...)`，
`/` 开头的输入不进 LLM。REPL 整体持有 `agent_lock`，回溯因此不会与 cron turn 并发。
`/rewind` 先打印 `plan_restore()` 的计划，确认后执行，执行完只补一行实际改动（`applied: ...`）。

### 22.4 边界

1. 回滚范围是 canonical messages + `write`/`edit` 的文件 + **bash/外部进程的文件改动**：
   begin_turn 预存当时已脏文件的 pre-image（source='baseline'），seal 时从 git 基线补记
   回合内的新建/修改/删除（source='git'，干净跟踪文件取 HEAD blob 作 pre-image）；
   git 索引/暂存状态不回滚，敏感文件与超大文件仍只提示不覆盖。
2. 记忆、cron、任务板、worktree、后台任务等副作用不回滚（报告里显式列出）。
3. `.worktrees/`、`.cache/`、`.git/` 及敏感文件不抓 pre-image。
4. 恢复默认要求目标文件与节点 seal 时的 hash 一致，否则整体中止（`--force` 才覆盖）。
5. teammate / subagent 的写入会挂到主会话当前节点，但它们没有自己的 checkpoint 节点；
   两者各有一份独立运行日志，父会话归档/恢复/删除时随之移动或清理。

## 23. 重要实现边界

1. 主 agent 有 canonical session event；subagent/teammate 写独立运行日志并在父会话留
   引用事件，子日志不参与主会话恢复，但会随父会话归档、恢复和删除。
2. `resume_agent_loop()` 会按三类标注 pending tool，但仍不自动重放任何工具；即使是
   `read-only` 也只给建议，由模型决定是否再次调用。
3. mailbox 已具备消息 id、claim/ack 与未确认重放；语义是 at-least-once，崩溃窗口内可能
   重复投递，不保证 exactly-once。
4. 任务认领已使用 advisory 跨进程文件锁；只保护遵守 `task_transaction()` 的写入方，
   外部进程直接改 `task_*.json` 仍可绕过。
5. 任务租约基于墙钟，默认 1 小时；teammate 每轮自动续租，但进程挂起或系统休眠期间
   无法续租，超时后仍会被回收。回收后旧 owner 的 complete 会因 owner 不匹配被拒绝，
   但它已经产生的文件改动不会被回滚。
6. 自动认领是 FIFO + 过期租约重试，没有按 owner 负载或任务优先级加权；claim 事务是
   跨进程的，但仍是 pull 模型。
7. 归档只管理父会话引用到的子日志；父引用事件写失败或手工删除父日志后留下的孤儿
   `subagents/`、`teammates/` 文件由启动维护预览（无引用且超过 30 天才列为候选，
   默认不删；父日志读不出来时本轮整体放弃）。
8. MCP 工具调用是同步等待异步桥结果，超时会转成普通工具错误；`mcp_call` 只记录
   server/工具/耗时/错误类型，不记录参数或返回内容。
9. daemon 线程在进程退出时会被直接终止；cron 通过 `wait_for_cron_idle()` 延长主进程生命周期。
10. 后台任务完成不会主动打断当前 LLM 调用，只能在后续工具轮被收集。
11. `write_transcript()` 是快照，不应作为 session crash recovery 的唯一来源。

## 24. 推荐的维护方向

后续如果继续增强，优先级建议为：

1. 按 teammate 负载/任务优先级调整自动认领顺序（续期已完成）。
2. begin_turn 的基线捕获与 `_git_status` 各起一个 git 进程，可合并成一次调用省一半开销。

启动维护（`harness.start_storage_maintenance()`，交互入口后台线程执行一次）已覆盖：
升级前的明文 checkpoint 快照压缩迁移（校验一致、默认保留明文，幂等），以及
孤儿子日志预览（默认不删，仅列出 30 天以上候选）。删除明文须显式
`remove_plain=True`；删除孤儿日志须显式 `purge_orphans=True`。

checkpoint 对 bash 改动的覆盖（§22.4）已完成：基线脏文件在 begin_turn 预存 pre-image，
seal 时按 git 基线补记外部新建/修改/删除（HEAD blob 取基线内容），回滚、冲突检测与
回合清单（非 Git 时的 fallback）免费获得同一份覆盖。
