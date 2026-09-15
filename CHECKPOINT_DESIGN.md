# Checkpoint / Rewind 设计方案

本文设计 Harness 的「可回溯节点」（checkpoint）能力：在循环的关键节点留下快照，之后可以把
**会话状态**和**工作区文件状态**一起回滚到该节点。

与 [`HARNESS_LOOP_ARCHITECTURE.md`](HARNESS_LOOP_ARCHITECTURE.md) 配套阅读：本文只描述新增能力，
现有循环结构以那份文档为准。

## 0. 决策摘要

| 维度                  | 决策                                                              |
| --------------------- | ----------------------------------------------------------------- |
| 落地范围              | 一期（会话回溯）+ 二期（文件回溯），不做 git 兜底层               |
| 触发方式              | 每轮自动打 turn 节点 + 首次 `write`/`edit` 前懒建 pre-edit 节点   |
| 回溯权限              | 仅用户 CLI 触发；模型只能创建 checkpoint，不能自己回滚            |
| 快照方式              | 会话侧：messages 深拷贝；文件侧：copy-on-write 保存 pre-image     |
| 与 session JSONL 关系 | 事件流只追加 `checkpoint` / `rewind` 两条小事件，快照本体独立落盘 |

## 1. 目标与非目标

### 1.1 目标

1. 每轮对话开始前自动留下一个可回溯节点。
2. 一轮内首次修改文件前，自动保存被改文件的 pre-image。
3. `rewind` 能把 canonical messages 恢复成某个节点的内容，并把该节点之后由 `write`/`edit`
   产生的文件改动一并还原（含「原本不存在 → 删除」）。
4. 回溯动作可审计、可预览（dry-run）、对无法安全恢复的情况拒绝执行。

### 1.2 非目标（本期不做）

- 不做 `bash` 产生的文件改动的精确回滚（只做漂移提示，见 §8.5）。
- 不把 restore 暴露给模型作为工具（避免模型自我回滚会话）。
- 不覆盖 subagent / teammate 会话（它们没有接入同一个 session event store，见架构文档第 23 节）。
- 不做跨机器的 checkpoint 同步、不做二进制/大文件的全量镜像。

## 2. 术语与节点模型

**节点（node）** = 一次快照对：`(messages, files)`。

| 节点          | 创建时机                                              | 内容                                     | 备注                        |
| ------------- | ----------------------------------------------------- | ---------------------------------------- | --------------------------- |
| turn 节点     | `agent_loop` 每轮 `while True` 开头                   | messages 全量深拷贝；`git status` 基线   | 每轮都产生，索引体积可控    |
| pre-edit 节点 | 同一轮内**首次**调用 `write`/`edit` 前懒建            | 该文件的 pre-image（不存在则 tombstone） | 一轮内多个文件共用同一节点  |
| 显式节点      | 模型调 `checkpoint` 工具 / 用户 `/checkpoint <label>` | 与当前活跃节点同构，额外带 label         | 模型只能创建，不能恢复      |
| seal          | 一轮 `turn_completed` 时                              | 固化 meta：改动文件清单、状态置 `sealed` | 未 seal 的节点状态为 `open` |

一个节点内部的文件快照是**累加**的：pre-edit 节点被懒创建后，同一轮内后续每次 `write`/`edit`
都把对应文件的 pre-image 补进同一个节点，保证「回滚到本轮之前」是完整的。

会话与文件必须同节点绑定，否则会出现「文件回到过去、对话没回」或反之的错位。

## 3. 数据布局

```text
.cache/checkpoints/
  index.jsonl                 # append-only 索引，每行一个节点
  state.json                  # {current_head: cp_xxx} 当前分支头
  <cp_id>/
    messages.jsonl            # canonical messages 快照（含完整 tool_calls 配对）
    meta.json                 # 节点元数据 + 每个文件的 exists / sha256 / 是否敏感
    files/
      <relpath>               # pre-image（原文件内容）；快照时不存在则只在 meta 里记 exists=false
```

`index.jsonl` 行示例：

```json
{
  "cp_id": "cp_20260915_143012_ab12",
  "label": "",
  "session_id": "...",
  "through_seq": 42,
  "parent_id": "cp_...",
  "trigger": "turn",
  "status": "sealed",
  "files": ["t.py"],
  "ts": 1757900000.1
}
```

`meta.json` 额外记录每个文件的 `sha256`（快照时）与 `restored_at`，用于冲突检测与审计。

放在 `.cache/` 下与现有 `sessions/`、`transcripts/`、`tool_results/` 一致，且 `.cache/` 已在
[`.gitignore`](.gitignore) 中忽略，不会污染工作区。

### 3.1 为什么快照本体不进会话事件流

`session_<id>.jsonl` 是崩溃恢复的唯一真相来源，逐行 fsync，体积敏感。把整份 messages 写进去会让
每轮事件量翻数倍。因此事件流只记录「指向快照的引用」，快照本体单独落盘；代价是快照文件必须与
会话文件同时保留，删除节点时要成对清理（见 §10 保留策略）。

## 4. 会话事件流扩展

在 [`session_store.py`](session_store.py) 中新增两类事件，保持追加语义不变：

```json
{"type":"checkpoint","seq":43,"checkpoint_id":"cp_xxx","through_seq":42,"label":"","trigger":"turn"}
{"type":"rewind","seq":88,"checkpoint_id":"cp_xxx","snapshot_path":".cache/checkpoints/cp_xxx/messages.jsonl",
 "scope":"session+files","restored_files":["t.py"]}
```

`rewind` 事件里带上 `snapshot_path`，让 `session_store` 不必知道 checkpoint 目录布局（避免模块耦合）。

配套改动：

1. `SessionEventStore` 新增公开属性 `last_seq`（当前实现只有私有 `_next_seq`），
   以及 `append_checkpoint(...)` / `append_rewind(...)` 两个封装方法，风格与
   [`append_message`](session_store.py#L66-L75)、[`append_compaction`](session_store.py#L77-L83) 一致。
2. [`rebuild_openai_history()`](session_store.py#L111-L127) 新增 `rewind` 分支：
   - 读取该 `checkpoint_id` 的 `messages.jsonl`；
   - 用它替换当前累积的 `messages`；
   - 追加一条 user 消息 `[Rewound to <cp_id>] Conversation history restored to this checkpoint.`，
     让模型知道中间发生过回滚；
   - 之后的事件继续按原顺序叠加。
   - 找不到快照文件时抛 `SessionReplayError`，与现有 compaction 缺 `summary` 的处理一致。
3. `validate_openai_history()` 无需改动：快照本身就是配对完整的 canonical history。
4. `pending_tool_calls()` / `recovery_message()` 自动受益——回滚到某节点后，若该节点是
   「assistant 已发 tool_call 但未收到结果」的状态，恢复提示会正常触发。

这与 [`compaction`](session_store.py#L121-L125)「替换此前历史」的语义完全同构，只是替换来源从
内联 summary 变成外部快照。

## 5. 生命周期时序

### 5.1 一轮正常流程

```text
agent_loop 进入 while True
  ↓
[新增] cp_mgr.begin_turn(messages, session_store)      # turn 节点 open
  ↓
pre_compress 快照 / pre_compact_messages
  ↓
LLM 调用
  ↓
tool_calls 分支
  ├─ write / edit
  │    ↓
  │   [新增] cp_mgr.capture_pre_image(file_path)        # 首次触发时懒建 pre-edit 节点
  │    ↓
  │   实际写盘
  └─ 其他工具：无快照开销
  ↓
turn_completed 分支
  ↓
[新增] cp_mgr.seal()                                    # 节点置 sealed，写 meta/index
```

### 5.2 回溯流程

```text
用户输入 /rewind <cp_id|last>
  ↓
获取 agent_lock（避免与进行中的 turn / cron turn 并发）
  ↓
cp_mgr.restore(cp_id, files=True, dry_run=True)         # 先打印计划
  ↓
用户确认
  ↓
文件恢复（pre-image 覆盖 / 删除新建文件 / 冲突则拒绝）
  ↓
session_store.append_rewind(...)                        # 追加 rewind 事件
  ↓
cp_mgr.set_head(cp_id)                                  # 分支头指向该节点
  ↓
messages = session_store.rebuild_openai_history()        # 得到回滚后的 history
  ↓
重新进入 agent_loop(messages, session_store=store)
```

## 6. 模块与接口

新增 `checkpoints.py`，模块级不持有可变状态（沿用 `agent_core.py` 的无状态逻辑层约定）；
目录等可变配置由 harness 持有（`CHECKPOINT_DIR`），活跃 manager 实例放在
`harness.CHECKPOINT_MANAGER`，`tools.py` 通过既有的 `_h()` 读取 —— 和 `WORKDIR` 的做法一致。

```python
class CheckpointManager:
    def __init__(self, base_dir, workspace, session_id, *, max_nodes=20, keep_labeled=True,
                 capture_files=True, ignore=None, secrets=None, max_file_bytes=5 * 1024 * 1024): ...

    # 节点生命周期
    def begin_turn(self, messages, session_store=None, label="", trigger="turn") -> str
    def capture_pre_image(self, path) -> None      # 幂等：同节点同路径只存一次；异常只告警
    def seal(self) -> dict | None                  # 写 post_sha256 + meta + index 行，状态 sealed

    # 查询
    def list(self, limit=20) -> list[dict]         # 最新在前
    def get(self, cp_id) -> dict                   # 读 meta（含每个文件记录）
    def head(self) -> str | None
    def resolve(self, ref) -> str                  # 'head'/'latest' → 最新；'last'/'undo' → 最近有文件改动的节点
    def messages_snapshot(self, cp_id) -> list[dict]

    # 回溯
    def plan_restore(self, cp_id) -> dict          # 不落盘：actions / conflicts / unverified / drift / side_effects
    def restore(self, cp_id, *, force=False, scope="all") -> dict
    def side_effects(self) -> dict                 # §8.6 的不回滚项清单
    def gc(self) -> list[str]
```

要点：

- `capture_pre_image` 接收**已解析的绝对路径**（`safe_path()` 的结果），这样 worktree 场景下
  基准目录天然正确，不需要依赖 hook（`PreToolUse` 钩子拿不到 teammate 的 `cwd`）。
- `capture_pre_image` 用「当前活跃节点」指针；若该轮还没有活跃节点（例如非主循环调用），
  直接静默跳过，保证 `write`/`edit` 的行为不因 checkpoint 未初始化而改变。
- 清理与快照写入都使用临时文件 + `os.replace` 原子替换，避免半截文件被当成有效快照。
- 目录、文件或路径不可解析（路径在工作区之外）时一律跳过该文件，不把异常抛给写盘路径。

## 7. 主循环集成点

| 位置                                                                                                  | 改动                                                                                                             |
| ----------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| [`harness.py:510`](harness.py#L510) `while True:` 开头                                                | `checkpoint_manager.begin_turn(messages, session_store)`，紧邻现有 `pre_compress` 快照之前                       |
| [`harness.py:596`](harness.py#L596) `session_store.append('turn_completed')` 之后                     | `checkpoint_manager.seal()`                                                                                      |
| [`harness.py:489`](harness.py#L489) `agent_loop`                                                      | 用传入的 `session_store` 取 manager（`get_checkpoint_manager`），按 `session_id` 复用，避免 resume 时丢状态      |
| [`harness.py:678`](harness.py#L678) `resume_agent_loop`                                               | 无需改动：manager 构造时已从 `state.json` 读回 `current_head`，跨进程重启后 `/rewind` 仍可用                     |
| [`tools.py:450`](tools.py#L450) `run_write` / [`tools.py:460`](tools.py#L460) `run_edit`              | 写盘前调用 `_capture_checkpoint(file_path)`（[`tools.py:432`](tools.py#L432)）                                   |
| [`harness.py:617`](harness.py#L617) 工具分发处                                                        | `checkpoint` 工具的特殊分支（照抄 [`compact`](harness.py#L633-L654) 的写法：不进 `tool_registry`，由 loop 处理） |
| [`tools.py:638`](tools.py#L638) `TOOLS`                                                               | `checkpoint` 的 function schema（可选 `label`）                                                                  |
| [`harness.py:438`](harness.py#L438) `handle_command` / [`harness.py:703`](harness.py#L703) `run_repl` | `/checkpoints`、`/checkpoint`、`/rewind`、`/checkpoint-gc` 命令入口                                              |
| [`harness.py:143`](harness.py#L143) 缓存目录配置区                                                    | `CHECKPOINT_DIR` 等配置 + `CHECKPOINT_MANAGER`（tools 通过 `_h()` 读它）                                         |

### 7.1 需要注意的既有行为

- `pre_compact_messages()` 在 `snip_compact` / `compact_history` 路径上会返回**新的 list 对象**
  （`harness.py` 里是 `messages, skip = pre_compact_messages(messages, ...)` 重新绑定）。
  turn 节点必须在重新绑定**之前**用 `deepcopy` 保存，不能持有 list 引用。
- `exec_tool_call` 是主循环与 teammate 共用的执行入口，但 teammate 传 `hooks=False`。
  pre-image 抓取放在 `run_write`/`run_edit` 内部而不是 hook 里，正是为了让两条路径都自动覆盖。
- `compact` 工具会在执行到一半时替换整个 `messages`（`harness.py` 的 compact 分支），
  已 seal 的 checkpoint 不受影响；compact 之后新开的 turn 节点自然以新历史为快照基线。
- `write`/`edit` 被 [`permission_hook`](hooks.py#L18-L40) 拒绝时不会进入 `run_write`，
  因此不会产生 pre-image —— 这是正确的：文件没被改，无需快照。
- 工具调用可能被 `should_run_background()` 转成后台任务，但 `write`/`edit` 不在后台关键词集合内，
  仍走前台同步路径，pre-image 与写盘顺序有保证。

### 7.2 worktree 与 teammate 场景

- 快照键统一是「主工作区相对路径」（`path.resolve().relative_to(WORKDIR)`），落在工作区之外的路径
  直接跳过，不参与快照与恢复。
- `.worktrees/` 在忽略名单里，因此 teammate 在 worktree 内的改动**不会**被抓 pre-image，回滚不动
  worktree —— 这是有意的：worktree 有自己的生命周期与变更保护（见 [`worktree.py`](worktree.py#L92-L116)），
  不与应用层回滚混在一起。
- teammate 写入的 pre-image 会挂到「当前同进程活跃的 turn 节点」上；由于 teammate 是常驻线程、
  没有自己的 turn 节点，这类 pre-image 会挂到主会话最近的一个 open 节点。若挂不上（无 open 节点），
  按 §6 的约定静默跳过，不阻塞 teammate。

## 8. 回溯算法与安全规则

### 8.0 恢复的是「节点开始时的状态」

节点链是线性的：从 `head` 沿 `parent_id` 回退到目标节点，得到 `[head, ..., target]`。
回滚到 `target` = 撤销这条链上**所有**节点的写入，因此同一个文件在多轮都被改过时，
最终生效的是链上最旧那份 pre-image（链上最早、即 target 那一侧的记录）。

只应用目标节点自己的 pre-image 是不够的：之后几轮改动过的、而目标节点没碰过的文件不会被还原。
`restore()` 因此按链合成，而不是只看单个节点。

目标节点不在 head 祖先链上时（例如它属于被放弃的分支）退化为「只用目标节点自己的 pre-image」，
并在报告里说明链长。

### 8.1 文件恢复动作

对节点内记录的每个路径：

| 记录形态     | 当前磁盘状态                          | 动作                                           |
| ------------ | ------------------------------------- | ---------------------------------------------- |
| 有 pre-image | 存在、hash 与 `post_sha256` 一致      | 用 pre-image 覆盖                              |
| 有 pre-image | 存在、hash 不一致（用户或 bash 改过） | **冲突**：默认拒绝，`--force` 才覆盖           |
| 有 pre-image | 不存在                                | 恢复 pre-image（文件被删了，回滚到「还存在」） |
| tombstone    | 存在                                  | 删除该文件（原本由 agent 创建）                |
| tombstone    | 不存在                                | 无需动作                                       |

### 8.2 冲突检测

`meta.json` 记录每个文件快照时的 `sha256`；`seal()` 时额外记录 `post_sha256`（该轮结束时的内容）。
`plan_restore()` 逐个比对当前磁盘内容：

- 与 `post_sha256` 一致 → 是 agent 自己改的，可安全回滚；
- 与 `post_sha256` 不一致 → 期间有人（用户 / 其他进程 / `bash`）动过，标记为冲突；
- 存在冲突且未传 `force` → 整个 restore 中止，只返回冲突清单，一个文件都不改；
- 节点还没 seal（该轮尚未结束）时没有 `post_sha256`，无法校验 → 记为 `unverified` 并照常恢复，
  报告里单独列出，不当作冲突处理（否则「回滚进行中的一轮」会被无谓拦住）。

这是「宁可不动，不可错删」的默认取向。

### 8.3 排除规则

以下路径不抓 pre-image，也不参与恢复：

- `.cache/`（checkpoint 自身所在，防止自我嵌套）
- `.git/`、`.worktrees/`、`.mailbox/`、`__pycache__/`、`.pytest_cache/`
- `.env` 等敏感文件：只记录路径与 hash，**不保存内容**，恢复时报告「无法恢复（敏感文件）」而不覆盖

### 8.4 dry-run 优先

CLI 默认先输出计划（将覆盖 / 将删除 / 冲突 / 跳过），用户确认后才落盘。
`restore(dry_run=...)` 与 `plan_restore()` 共用同一套计算逻辑，避免「预览和实际不一致」。

### 8.5 bash 漂移提示（不做精确回滚）

turn 节点记录一次 `git status --porcelain` 与 `git rev-parse HEAD` 作为基线。回溯时：

- 对比基线与当前 `git status`，列出**不在 checkpoint 文件清单里**但发生了变化的文件；
- 这些文件标记为 `unrestorable`，在报告里显著提示「由 bash 或外部改动产生，未自动恢复」；
- 不自动执行 `git checkout`（避免动用户的 working tree 与未跟踪文件）。

这保证了能力的诚实边界：能恢复的说能恢复，不能恢复的明确列出来。

### 8.6 不受回滚影响的副作用（必须显式告知用户）

回滚只覆盖两件事：**canonical messages** 与**被 `write`/`edit` 改过的文件**。以下副作用不会回滚：

| 副作用          | 落盘位置                       | 影响                                                    |
| --------------- | ------------------------------ | ------------------------------------------------------- |
| 记忆提炼        | `.cache/memories/*.md`         | 回滚前一轮的 `extract_memories` 结果仍然保留            |
| todo 状态       | 会话内的 `todo_write` 工具结果 | 随 messages 快照一起回滚（属于会话状态）                |
| cron 任务       | `.cache/cron/*`                | 已注册的定时任务仍会触发                                |
| 任务板          | `.cache/tasks/task_*.json`     | `create_task`/`claim_task`/`complete_task` 的结果不回滚 |
| worktree        | `.worktrees/`、git 分支        | 已创建的 worktree/分支不回滚                            |
| 后台任务        | 进程内 `background_results`    | 已启动的后台命令无法取消                                |
| bash 的文件改动 | 工作区                         | 只提示，不恢复（§8.5）                                  |

`/rewind` 的输出报告必须列出这些「不回滚项」的当前状态，避免用户误以为回到了完全干净的过去。
这一条是二期实现的硬性验收点。

### 8.7 边界情况

| 情况                                     | 行为                                                                                                                 |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| 没有任何 checkpoint（老会话）            | `/rewind` 提示无可用节点，不做任何改动                                                                               |
| 目标是 `open` 状态的节点（该轮还没结束） | 允许，但报告里标注「该轮未完成」，文件清单以当前已抓取的 pre-image 为准                                              |
| 重复 rewind 到同一节点                   | 幂等：第二次执行时磁盘内容已等于快照，冲突检测全部通过，不产生额外改动，但仍追加一条 `rewind` 事件用于审计           |
| rewind 后想回到更晚的节点                | **支持前滚**：节点未被删除且 head 可指向它，等价于「回滚到那个节点」。语义是「恢复到该节点状态」，不是严格的时间倒流 |
| 目标节点的快照文件缺失                   | 拒绝执行并报错（与 [`SessionReplayError`](session_store.py#L11-L12) 一致的失败方式），不猜测历史                     |
| rewind 期间有 cron turn 在跑             | 阻塞等待 `agent_lock`，不并发修改 messages                                                                           |

## 9. CLI 设计

```text
/checkpoints                         列出节点：id、label、触发方式、文件数、时间、是否当前 head
/checkpoint [label]                  手动打一个节点（等价于模型调用 checkpoint 工具）
/rewind last                         预览 → 确认 → 回溯到「最近一个有文件改动的节点」
/rewind head                         回溯到最新节点（可能只回滚会话）
/rewind <cp_id>                      回溯到指定节点
/rewind <cp_id> --session            只回滚会话，不动文件
/rewind <cp_id> --force              冲突时强制覆盖
/rewind <cp_id> --yes                跳过确认（脚本化用）
/checkpoint-gc                       按保留策略清理旧节点
/exit, /quit                         结束会话
```

### 9.1 命令入口

原来的 [`__main__`](harness.py#L738) 只执行**固定的一条 user 消息**再等 cron 收尾，没有交互循环，
命令无处安放。因此新增了最小 REPL [`run_repl`](harness.py#L703)：

1. 整个 REPL 跑在 `with agent_lock:` 里，回溯命令天然与 cron turn 互斥；
2. 一个 `SessionEventStore` 贯穿整个会话，普通输入以 user 消息追加后调用 `agent_loop(messages, session_store=...)`；
3. 以 `/` 开头的输入交给 [`handle_command`](harness.py#L441)，不进入 LLM；
4. `agent_loop` 每轮结束就返回，REPL 随后用 `rebuild_openai_history()` 刷新自己持有的 messages ——
   这样即使循环内发生过压缩或回溯，下一轮看到的仍是 canonical history；
5. `/rewind` 在命令分支里完成「预览 → 确认 → 恢复文件 → 追加 rewind 事件 → 重建 messages」，
   然后回到提示符继续对话。

替代方案（独立入口 `python checkpoints_cli.py list|rewind <cp_id>`，只做回溯不承载对话）没有采用：
回溯之后立刻接着对话才是主场景，而它同样需要 `agent_lock`。

### 9.2 `last` 与 `head` 的区别

节点是**按 loop 迭代**生成的，一个用户回合往往有两个节点：改文件的那次迭代，和只输出最终
回答的那次迭代。后者的快照里「文件已经是改完的状态」，它自己也没有 pre-image —— 所以
「最新节点」并不能撤销上一轮的文件改动。

| 引用                     | 指向                                                                | 适用                                     |
| ------------------------ | ------------------------------------------------------------------- | ---------------------------------------- |
| `last`（`/rewind` 默认） | 最近一个 `files` 非空的节点；没有任何节点记录过文件时退化为最新节点 | 「把刚才改坏的文件还原」，日常用法       |
| `head` / `latest`        | 最新节点，不保证带文件改动                                          | 想精确回到「最后一次回答之前」的会话状态 |

`/checkpoints` 输出里的 `files=N` 就是判断依据：`files=0` 的节点不带任何 pre-image，
回滚它只影响会话。

`/rewind` 一律先跑 `plan_restore()` 打印计划（覆盖 / 删除 / 冲突 / 未校验 / 不可恢复 / 不回滚项），
用户确认后才落盘；执行后只额外打印一行实际改动（`applied: ...`），不重复整份报告。
有冲突且未加 `--force` 时直接停止，一个文件都不改。

## 10. 配置与保留策略

沿用 [`harness.py`](harness.py#L141-L148) 的全局配置集中区风格，新增：

```python
CHECKPOINT_DIR  = WORKDIR/'.cache'/'checkpoints'
CHECKPOINT_MAX_NODES = 20          # 保留最近 N 个节点
CHECKPOINT_KEEP_LABELED = True     # 带 label 的显式节点不被 GC
CHECKPOINT_CAPTURE_FILES = True    # 关闭后退化为纯会话回溯
CHECKPOINT_IGNORE = ['.cache', '.git', '.worktrees', '.mailbox', '__pycache__', '.pytest_cache']
CHECKPOINT_SECRETS = ['.env', '*.pem', '*_rsa']
CHECKPOINT_MANAGER = None          # 活跃 manager；tools 的 write/edit 通过 _h() 读它
```

- 回收只删「最旧、非当前 head、非 label」的节点，由 `/checkpoint-gc` 显式触发（不在每轮自动跑）。
  代价是删除会切断更早节点的回溯链，之后回滚到更早节点时文件侧退化为单节点语义（§12.8）。
- 删除节点目录后，往 `index.jsonl` 追加一行 `{"cp_id":..., "gc":true}`；`list()` 折叠索引时按标记过滤，
  保持 append-only 不变。

## 11. 测试计划

新增 `test_checkpoints.py`，风格对齐 [`test_session_store.py`](test_session_store.py)
（pytest + `tmp_path`，通过 `monkeypatch` 改 `harness.WORKDIR` 等可变全局）。

| 用例                 | 断言                                                                                                 |
| -------------------- | ---------------------------------------------------------------------------------------------------- |
| turn 节点落盘        | `messages.jsonl` 内容与传入 messages 深相等；index 行 `status=open`                                  |
| pre-image 幂等       | 同一节点内对同一路径调用两次，`files/` 下只有一个副本                                                |
| 懒建节点             | 一轮内改两个文件，两个 pre-image 落在同一个 `cp_id`                                                  |
| 新建文件回滚         | tombstone 记录的文件在 restore 后被删除                                                              |
| 覆盖回滚             | 被 `edit` 修改的文件在 restore 后内容与原文按字节一致                                                |
| 冲突拒绝             | restore 前手工改文件 → `plan_restore` 报冲突，`restore` 在无 `force` 时不改任何文件                  |
| dry-run 无副作用     | 调用后磁盘 hash 全部不变                                                                             |
| 排除规则             | 写 `.cache/x.txt`、`.env` 不产生 pre-image                                                           |
| rewind 重放          | 追加 `rewind` 事件后 `rebuild_openai_history()` 等于快照内容 + `[Rewound ...]`                       |
| 跨重启               | 新 manager 从 `state.json` 恢复 `current_head` 后 `/rewind last` 可用                                |
| `last` 跳过空节点    | 末尾是「只回答、没改文件」的节点时，`last` 指向更早那个有文件改动的节点；`head` 仍指最新             |
| 无节点               | 空索引下 `/rewind` 返回「无可用节点」且磁盘零改动                                                    |
| 重复 rewind          | 连续两次回滚到同一节点，第二次磁盘 hash 不变，但追加了第二条 `rewind` 事件                           |
| 前滚                 | rewind 到较早节点后再 rewind 到较晚节点，messages 与文件都等于较晚节点的快照                         |
| 不回滚项报告         | 报告含 §8.6 的 cron / 任务板 / worktree 等条目（至少列出非空清单）                                   |
| GC                   | 节点数超过上限时最旧节点目录被删且 index 出现 `gc` 行                                                |
| 端到端（agent_loop） | 两轮迭代产生 2 个 sealed 节点、`write` 前抓到 pre-image；`rewind` 后文件与 messages 同时回到节点状态 |
| `checkpoint` 工具    | 模型调用后生成 `trigger=manual` 的带 label 节点，且 history 里无孤儿 tool 消息                       |
| 命令层               | `/checkpoints` 列出节点、`/checkpoint` 生成 manual 节点、`/rewind --yes` 回滚文件并追加 rewind 事件  |

端到端两条用例把 `harness.WORKDIR` / `CHECKPOINT_DIR` / `SESSIONS_DIR` 指向 `tmp_path`，
并打桩 `load_memories` / `extract_memories` / `consolidate_memories`（否则记忆系统会额外调用 LLM，
吃掉脚本化的假响应）。

回归：现有 `test_session_store.py` 必须保持全绿（新事件类型不得影响既有重放语义）。

## 12. 边界与已知限制

1. `bash`、MCP 工具、外部进程产生的文件改动无法精确回滚，只做漂移提示（§8.5）。
2. 二进制文件按字节保存 pre-image，超大文件（默认 > 5 MB）只记录 hash 与路径，标记为不可恢复。
3. 同一 `session_id` 的多进程并发写入未加跨进程锁；回溯依赖 `agent_lock` 在进程内串行化。
4. subagent / teammate 的文件改动会被 pre-image 覆盖（同进程共享 manager），但它们不产生会话节点。
5. 回滚是「恢复内容」，不恢复文件 mtime/权限位。
6. `index.jsonl` 与 `session_*.jsonl` 需成对保留；单独删除 `session_*.jsonl` 会使 rewind 事件失去引用。
7. 记忆、cron、任务板、worktree、后台任务等副作用不回滚（§8.6），回滚后工作区并非「完全干净的过去」。
8. 「前滚」依赖目标节点未被 GC；被回收的节点只能重新开始，不能跳回。

## 13. 实施步骤与验收

实施顺序（1 → 6，每步都带测试）：

1. `checkpoints.py` + `test_checkpoints.py`（纯文件逻辑，不接触主循环）。
2. `session_store.py` 扩展：`last_seq`、`append_checkpoint`、`append_rewind`、rewind 重放分支 + 测试。
3. 主循环接入：`begin_turn` / `seal` / `run_write`+`run_edit` 的 pre-image 抓取。
4. `checkpoint` 工具 schema 与 loop 特殊分支。
5. 命令入口（§9.1 的最小 REPL）+ `/checkpoints` `/checkpoint` `/rewind` `/checkpoint-gc`。
6. 更新 [`HARNESS_LOOP_ARCHITECTURE.md`](HARNESS_LOOP_ARCHITECTURE.md)（新增一节说明 checkpoint 生命周期）。

验收标准：

- 一轮对话结束后，`/checkpoints` 能看到本轮节点及改动文件清单；
- 对一次「改坏文件」的操作执行 `/rewind last`，文件内容与对话历史同时回到上一轮开始的状态；
- 冲突场景下不传 `--force` 时零文件被修改；
- `/rewind` 报告包含 §8.6 的「不回滚项」清单；
- `test_checkpoints.py`（23 例）与 `test_session_store.py`（7 例）全绿，且新事件类型不影响既有重放。

## 14. 风险与缓解

| 风险                     | 缓解                                                                  |
| ------------------------ | --------------------------------------------------------------------- |
| 快照占用磁盘             | 只存被改文件的 pre-image（非全量工作区）+ 默认 20 节点上限 + GC       |
| 敏感内容落盘             | 敏感文件只存 hash 与路径，不存内容                                    |
| 误删用户文件             | `exists=false` 记录 + `post_sha256` 冲突检测 + 预览确认后才落盘       |
| 模型自我回滚导致状态混乱 | restore 不暴露为工具，仅 CLI + `agent_lock`                           |
| 快照半写导致恢复出错     | 临时文件 + 原子替换；恢复前校验 hash                                  |
| 与 compaction 语义冲突   | 两者都是「替换此前历史」，rewind 事件在重放中按顺序生效，顺序天然正确 |
