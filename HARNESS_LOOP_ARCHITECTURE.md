# Harness Loop 架构与循环逻辑

本文以当前仓库代码为准，说明 Harness 从收到用户消息到最终回答、工具执行、压缩、恢复、后台任务、cron、MCP、子代理、teammate 协作、任务认领和 worktree 隔离的完整循环。

## 1. 代码入口与状态边界

主入口是 [`harness.py:330`](harness.py:330) 的 `agent_loop(messages, session_store=None)`。

相关模块职责如下：

| 模块 | 职责 |
| --- | --- |
| `harness.py` | 主 agent loop、请求构造、工具编排、cron 注入、会话恢复入口 |
| `agent_core.py` | hooks、记忆、上下文压缩、transcript 快照、记忆注入 |
| `tools.py` | OpenAI tool schema、工具注册表、任务/cron/团队工具实现 |
| `session_store.py` | 追加式 session JSONL、OpenAI 历史重放、pending tool 检测 |
| `recovery.py` | 429/overload 重试、备用模型、输出截断和上下文超限判断 |
| `background_task.py` | 慢工具后台执行和完成通知收集 |
| `cron_scheduler.py` | cron 时间匹配、持久化、队列生产和队列处理线程 |
| `mcp_bridge.py` | MCP server 连接、异步事件循环、动态工具注册 |
| `subagent.py` | `task` 工具启动的短生命周期子代理循环 |
| `agent_teams.py` | 常驻 teammate 线程、邮箱通信、计划审批、任务工具和 worktree 上下文 |
| `task_system.py` | 持久化任务板、依赖、认领、完成和解锁 |
| `autonomous_agent.py` | teammate 空闲轮询、收消息、自动认领任务 |
| `team_protocols.py` | shutdown/plan approval 协议状态机 |
| `skill_system.py` | skill 扫描、目录展示和按需加载 |
| `worktree.py` | git worktree 创建、任务绑定、变更保护和保留/删除 |
| `hooks.py` | 工具权限、日志、大输出、用户提交和停止 hook |

主循环中的 `messages` 是 OpenAI canonical history；system prompt 和相关记忆通过请求副本注入，不应混入 canonical history。

## 2. 主 agent loop

### 2.1 初始化

调用 `agent_loop()` 时：

1. 如果没有传入 `SessionEventStore`，创建新的 session 日志对象。
2. 将初始消息写入 `.cache/sessions/session_<id>.jsonl`。
3. 调用 `update_context()` 收集工作目录、工具、记忆索引和 skills。
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

## 6. LLM 调用与错误恢复

API 调用在 [`harness.py:386-409`](harness.py:386) 中执行。

### 6.1 429/overload

[`recovery.py:32`](recovery.py:32) 的 `with_retry()` 对 overload、429、rate limit 使用指数退避：

- 最多 10 次；
- 初始约 0.5 秒；
- 最大约 32 秒；
- 加随机 jitter；
- 连续 overload 达阈值时切换 `FALLBACK_MODEL_ID`。

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

`compact` 在 [`tools.py:670-680`](tools.py:670) 暴露给模型，但不放入普通 `tool_registry` 执行。

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
- `compaction`：历史已被摘要替换；
- `turn_completed`：一轮正常完成。

每条事件包含版本、序号、session id、时间戳，并使用追加写入、flush、fsync。

### 11.2 重放

`rebuild_openai_history()` 从事件流重建 canonical history；遇到 compaction 时以摘要替换此前运行历史。`validate_openai_history()` 检查 assistant tool calls 与 role=tool 的配对。

### 11.3 恢复入口

[`harness.py:498`](harness.py:498) 的 `resume_agent_loop(session_path)`：

1. 打开 session JSONL。
2. 重建 OpenAI 消息。
3. 检查 pending tool calls。
4. 添加 recovery 提示。
5. 继续进入 `agent_loop()`。

现阶段恢复会识别 pending tool，但不会无条件自动重跑副作用工具；恢复提示要求先检查当前状态。

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

subagent 使用 `SUB_TOOLS` 和 `SUB_HANDLERS` 快照，当前实现没有复用主循环的完整 session event 记录、recovery retry 和记忆提取。

## 14. 常驻 teammate loop

teammate 由 [`agent_teams.py:215`](agent_teams.py:215) 的 `spawn_teammate_thread()` 创建 daemon 线程。

### 14.1 启动

1. 检查同名 teammate 是否已存在。
2. 创建线程和 teammate system prompt。
3. 构建 teammate 专用工具集。
4. 初始化 worktree 上下文 `wt_ctx`。
5. 召回记忆，但不提炼全局记忆。
6. 进入常驻循环。

### 14.2 每轮 teammate 循环

```text
读取自己的 mailbox
  ↓
处理 shutdown / plan approval 协议
  ↓
普通消息包装成 <inbox>
  ↓
执行和主循环相同的预压缩
  ↓
注入记忆并调用 LLM
  ├─ 普通文本：进入 idle_poll
  └─ tool_calls：执行工具，继续下一轮
```

代码位置：[`agent_teams.py:237-297`](agent_teams.py:237)。

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

`send()` 追加 JSONL；`read_inbox()` 读取后删除文件，实现简单的消费队列。

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
```

每个任务保存为 `.cache/tasks/task_<id>.json`。

### 16.2 创建

`create_task()` 生成唯一 id，状态设为 `pending`，写入任务文件。

任务可指定 `blockedBy`，表示必须等待前置任务完成。

### 16.3 认领

`claim_task(task_id, owner)` 的流程：

1. 加载任务。
2. 检查状态必须是 `pending`。
3. 检查所有 `blockedBy` 任务存在且为 `completed`。
4. 写入 owner。
5. 状态改为 `in_progress`。
6. 立即持久化。

代码位置：[`task_system.py:54-64`](task_system.py:54)。

### 16.4 完成

`complete_task()`：

1. 将状态设为 `completed`。
2. 持久化任务。
3. 扫描所有 pending 任务。
4. 找出依赖已满足的任务。
5. 在返回信息中报告被解锁任务。

代码位置：[`task_system.py:67-76`](task_system.py:67)。

### 16.5 teammate 自动认领

teammate 在普通回答后进入 `idle_poll()`，默认每 5 秒检查一次，最长等待 60 秒：

1. 读取 mailbox。
2. 处理 shutdown/plan 协议。
3. 普通消息唤醒工作。
4. 扫描未认领且依赖满足的任务。
5. 选择第一个可认领任务。
6. 调用 `claim_task(task_id, name)`。
7. 如果任务绑定 worktree，更新 `wt_ctx['path']`。
8. 注入 `<auto-claimed>...</auto-claimed>`，回到 LLM 工作轮。

代码位置：[`autonomous_agent.py:29-78`](autonomous_agent.py:29)。

当前自动分配策略是“扫描排序后的第一个可用任务”，没有公平调度、租约、抢占或失联超时回收。

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

启动时 `scan_skills()` 扫描 `skills/*/skill.md`，解析 frontmatter，构建 `SKILL_REGISTRY`。

system prompt 通过 `list_skills()` 展示技能目录；模型需要具体规则时调用 `load_skill(name)` 获取 skill body。

代码位置：[`skill_system.py:13-40`](skill_system.py:13)。

这是一种“目录预加载 + 内容按需加载”模式：技能索引进入 system prompt，完整技能内容不默认塞入上下文。

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

## 22. 重要实现边界

1. 主 agent 有 session event 记录；subagent 和 teammate 目前没有完全接入同一套 session event store。
2. `resume_agent_loop()` 能识别 pending tool，但副作用工具的自动恢复策略仍应按工具类型细分。
3. mailbox 的 `read_inbox()` 是读完即删，适合简单协作，不是可靠事件日志。
4. 任务认领没有跨进程锁，多个进程同时认领同一 pending task 可能发生竞态。
5. teammate 自动认领采用第一个可用任务，没有公平分配策略。
6. MCP 工具调用是同步等待异步桥结果，超时会转成普通工具错误。
7. daemon 线程在进程退出时会被直接终止；cron 通过 `wait_for_cron_idle()` 延长主进程生命周期。
8. 后台任务完成不会主动打断当前 LLM 调用，只能在后续工具轮被收集。
9. `write_transcript()` 是快照，不应作为 session crash recovery 的唯一来源。

## 23. 推荐的维护方向

后续如果继续增强，优先级建议为：

1. 为 pending tool 增加 `read-only / idempotent / destructive` 恢复策略。
2. 为任务 claim 增加文件锁或原子状态更新。
3. 将 teammate 和 subagent 的关键事件也接入独立 session store。
4. 为 mailbox 增加消息 id、ack 和未确认重放能力。
5. 给 MCP wrapper 增加工具调用耗时、server 名称和错误类型事件。
6. 为自动认领加入 owner lease、超时回收和公平调度。

