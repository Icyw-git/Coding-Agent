# DeepSeek `dsh-superagent-prototype` 调研：他们是怎么做 super-agent 的

日期：2026-11-01。仓库：`deepseek-ai/dsh-superagent-prototype`（MIT，2026-10-31 创建）。
本地副本：`.test-tmp/research/dsh`（`git clone --depth 1`）。

**结论先说**：它不是"一个 agent 加一堆工具"，而是**一个调度会话（main）+ 每张工单一个执行会话（worker）**
的工作系统。真正值得学的不是某个功能，而是三件事：**把人的注意力当预算来设计**、
**把授权拆成"确定性准入 + 模型研判 + 可失效的依据"**、**把所有外部副作用变成"先落库后执行"的可恢复工作**。

---

## 0. 先对齐事实（避免把它当成"官方产品"）

| 项 | 实测值 |
| --- | --- |
| 定位 | README 原话：*Research prototype for reference only, not an official DeepSeek product*；"much of the engineering done by coding agents" |
| 形态 | 不是独立框架，而是 **DeepSeek Harness (DSH) 的一个 bundle**（`cordis.patch.yml` + peer deps `@deepseek-ai/dsh-*`） |
| 规模 | 216 文件；`src` **15,205 行** TS/TSX；`tests` **12,156 行 / 689 个用例** |
| 文档 | `docs/000`–`008` 共 **1,973 行**，编号强依赖（每篇只引用它前面的篇） |
| 依赖 | 运行时只有 `zod` + `proper-lockfile`，其余全是 DSH 的包 |

主会话 14 个应用工具，执行会话 6 个；`pnpm check` = typecheck + test + build。

---

## 1. 设计哲学：把"注意力"当成最稀缺资源

`docs/000-philosophy.md` 只有 33 行，但整套设计都由它推导：

> **Take as little human attention as possible, and give as much value as possible.**

判据极具体：*"A message, a notification, a question, a setting, a panel element and a
paragraph of documentation each cost someone's attention, and each has to give more
value than that, or it is removed."*

对 agent 的五条要求（可当 checklist 用）：

1. **只在被要求、或被写下来的规则允许时才动**；无人要求的工作必须以"只记录不执行"的模式起步；
2. **只在"人必须行动"或"人能用上结果"时才开口**——请求确认一次，干活时保持安静；
3. **一次只问一个具体问题**，走结构化字段，避免问题淹没在长报告里；
4. **按影响停下，不按工作量停下**：局部改动做到可 review 为止；需要设计决策的地方停下来问；
5. **诚实交付**：草稿标未完成，失败就说失败，绝不为了不问人而绕开限制。

对设计的要求：**少概念**（业务需求要么是已有概念的属性，要么是 prompt 文本，不新增概念）、
**少设置**（一个值适用几乎所有团队就用默认值）、**面板只显示需要人的东西**（健康状态不占空间）。

这条"注意力预算"和我们"主动性能默认关、建议不是行动"是同一方向，但他们把它上升成了
**可审计的领域概念**（见第 2 节的 `Attention`）。

---

## 2. 领域模型：8 个概念，以及一份"被拒绝的概念"清单

### 2.1 数据流

```
Source ──Trigger──> Thread ──batch──> Eval ──Decision/Mandate──> Ticket ──Assignment──> Executor
  │                   │                  │                          │
  │                   └─ Subscription fan-out ──> Ticket          └── Channel ──> Source（回复）
  └─ 读位点/自身 bot 身份            Gate（确定性）+ Assessment（模型）
```

| 概念 | 职责（一句话） | 最值得学的一条规则 |
| --- | --- | --- |
| **Source** | 事件流；一种"类"可复用，一个"实例"是一份配置 | **自己保存读位点 + 自己的 bot 身份**：重启不重放事件 |
| **Trigger** | 一个不可变事件（带来源方元数据，如 actor） | **幂等单元**：同一 Trigger 被接受两次只产生一张工单 |
| **Thread** | 时间序的 Trigger 集合 | **同一 Thread 上同时最多一个 Eval**；期间到达的合并进下一批 |
| **Correlation** | 一个 Trigger 所属多个 Thread 的关系 | **只加上下文，绝不路由**（路由是 Subscription 的事） |
| **Channel** | Thread 的写侧；定义可回复的地址 | 确认、答复、PR、review 回复都是"写" |
| **Policy** | 带版本号的准入规则 + 每个 Source 的部分（白名单、bot 名单、准入表） | **版本变更 → 丢弃运行中的 Eval**，但不撤销已有 Mandate |
| **Eval** | 两层判定：Gate（确定性）+ Assessment（模型） | 输入是"Trigger 批 + Thread 快照 + 该 Thread 上所有工单 + 当前 Policy"，**无状态** |
| **Mandate** | accept 授予的东西：Policy 版本、范围、强度、命中规则、发送者身份 | **不可变**：要更宽范围就开新工单；只有"人喊停"或"Policy 不再成立"能撤 |
| **Ticket** | 授权工作单元 | **自己定义状态**，不跟随执行器（可交接、恢复、多个 Executor 先后跑） |
| **Assignment** | 工单 ↔ 一个 Executor 实例的绑定 | 一张工单可多次派工，状态独立于工单 |
| **Subscription** | 工单 ↔ Thread 的多对多（由 Thread 持有） | 匹配的 Trigger **扇出给所有订阅工单、不检查是谁发的**；但扇出不授予权限，只给上下文 |

### 2.2 真正值得抄的是"被拒绝的概念"表

`docs/001-domain.md` 有一节 **Rejected concepts**，明确写 12 条"我们不做 X，因为 Y"：

- ❌ **Principal / Membership 实体、跨 Source 身份映射**——"一个人是 Trigger 的元数据；白名单是
  Source 的一部分；跨 Source 映射是 prompt 文本"。**用 prompt 解决身份映射，不建实体。**
- ❌ **Eval 自己调度延迟触发**——"需要等待的条件本身就是一个 Source"，Eval 绝不反向依赖 Source。
- ❌ **"等待中/已撤销"的 Eval 状态**——等待属于 Source；Eval 超时就把 Trigger 重新排队。
- ❌ **工单状态跟随 DSH Session**——工单可交接/恢复/多次执行，所以它自己定义状态。
- ❌ **Policy 变更时取消运行中的工单**——"取消会留下半成品；变更是记录 + 投递通知"。
- ❌ **从报告自由文本推断"在等人"**——"分类需要结构化事实，执行器用结构化字段提问"。
- ❌ **给订阅加准入检查**——"订阅覆盖整个 Thread；Mandate 把授权和上下文分开"。

> 这一节的工程价值高于前面所有定义：**成熟系统的标志是能说清"我们不做什么、为什么"**。

---

## 3. 授权：确定性 Gate + 模型 Assessment 的两级分离

### 3.1 Gate：纯代码，只看事实

`src/policy.ts` 的 `gate(trigger, thread, tickets)` 是纯函数，输出
`{admissible, strength, basis, reason}`，只回答**"谁、以什么强度、能否发起工作"**：

| 强度 | 触发条件 |
| --- | --- |
| `explicit` | 白名单上的人 @ 了 agent；或白名单上的人在已有工单的 Thread 上跟进；或准入表某格被设成 explicit |
| `inferred` | 白名单外的人在已有工单 Thread 上跟进；准入表设成 inferred 的格；Source bot 名单里的 bot（需 @ 或该范围收全部消息） |
| `not admitted` | 其他 bot、准入表 ignore、以及 `accept mode = off` 时的所有推断情形 |

**准入表**是精妙处：行 = 接收范围（Feishu 每个群/私聊、GitHub 的 Issue/PR/评论/主分支 CI），
列 = 三种组合：

| 格 | 可选值 | 默认 |
| --- | --- | --- |
| 白名单内、未 @ | inferred / ignore | inferred |
| 白名单外、@ 了 | explicit / inferred / ignore | **ignore** |
| 白名单外、未 @ | inferred / ignore | ignore |

只有"该范围收全部消息"的来源才开放"未 @"的格；CI 结果没有发送者，只开放最后一格。

### 3.2 accept mode：三档，默认 observe

| 模式 | 效果 |
| --- | --- |
| `off` | 任何推断都不成为候选 |
| **`observe`（默认）** | 推断候选照常评估，正向结论**记录成 "would accept" 的 ignore**，什么都不接 |
| `auto` | 正向评估直接接下 |

> 比"主动模式默认关"更细致：**默认不是"不评估"，而是"评估了但不执行，并留下 if-would-accept
> 的证据"**。运营者看几天记录再切 auto。这个"观察期"设计我们没有。

### 3.3 Mandate + Policy 版本 + 再校验

- 每个 Mandate 记录接受时的 **Policy 版本**（`policyVersion()` = 规则 + mode + 各 Source 部分的
  哈希，前缀 `mandate-v3`）。
- Policy 变了：**丢运行中的 Eval 重排队**；给受影响的活跃工单投递 `policy-changed` Input（说明
  哪条规则变了 / 请求人不再有权限 / 主动不再被接受）；**不撤销已有 Mandate**。
- 恢复已结束工单时**按当前 Policy 再校验**（`revalidate`）：explicit 跟进型直接过；inferred 需
  `auto` 且命中规则仍启用；准入表型需"今天这张表给的强度 ≥ 原强度"；bot 型需它仍在名单里。
- 再校验失败 → 工单保持结束，消息当作"没到达任何工单"重新进 Eval。

**这解决的是我们"审批作用域"没解决的一个问题**：我们的四档（once/times/until/session）作用域是
"这条命令/这个域名"；他们的是"**这份授权的依据是什么，依据变了授权就失效**"。后者对长期运行的
后台 agent 更正确。

### 3.4 评估由主会话自己做（没有独立裁判模型）

`prompts/admission.md` 第一句：*"Assess proactive candidates yourself, in this Session"*。
`MasterAssessor`（`src/app/assessment.ts`）只做一件事：把候选卡片投递给主会话，并记录"候选已送达"
——评估超时**从送达才开始算**（会话忙不消耗评估时间）。

判定通过三个工具完成：`orchestrator_eval`（拿上下文）→ 需要时 `orchestrator_eval_read`（展开某条
消息 / 仓库）→ `orchestrator_eval_decide`（记录）。

**返回的 `rule_assessments` 必须对每条启用规则各给一条（matched true/false）**，代码校验"恰好一次、
不许禁用/未知/重复 id"，正向命中由代码从布尔值推出，不信模型自己汇总。

> 对照我们的 LLM 审查官："必须对每条规则给结论，**包括不匹配**"这条约束值得直接搬——它把"漏判"
> 变成 schema 错误。

---

## 4. 执行层：工单、槽位、后台交接

### 4.1 工单阶段表（从人视角定义状态）

按顺序取第一个匹配行：

| 阶段 | 条件 | 面板文案 |
| --- | --- | --- |
| person / delivery | 给执行会话投递 Input 失败（2s 起、指数退避至 1 分钟） | Input undelivered |
| person / answer | 有 Attention（待回答的问题） | Awaiting answer |
| person / review | 最新报告是 `ready` | Awaiting review |
| person / blocked, failed | 最新报告 blocked/failed 且此后没有人的消息 | Blocked · Failed |
| working / queued | 无派工或派工待启动 | Queued |
| idle | 最新报告 waiting，或派工已结束 | Waiting for a Trigger |
| working | 其他 | Working |

`person/*` = "没有人的输入就走不下去"。**新的真人消息会把 blocked/failed 解开**回到 working。

### 4.2 槽位与并发

- 配置 `maxWorkers`（默认 **3**）。
- **额外留 1 个槽位给"人的消息 / 状态查询"**：正常槽满时真人跟进仍能拿到额外槽位；新任务不能
  挤掉已有工单。
- 工单从"启动（含 prepare）"或"唤醒 idle 执行器"开始持有槽位，整轮 turn 期间都持有。
- 启动并行：慢的 prepare 只拖慢自己那张工单。

### 4.3 后台交接（最实用的机制）

- 执行器 preset 把**前台 shell 命令 5 秒后自动转后台**——worker 能在下一步接收新消息，不必等长
  命令跑完。
- 一轮结束时若还有后台任务/子代理在跑：**工单保留槽位、状态仍是 working**，不报"等待"；任务完成
  会把结果送进同一槽位唤醒它。
- 一轮结束后后台最多再跑 **30 分钟**：到点杀 shell 任务、报 waiting，并附"执行器最后说的话 +
  哪些任务被杀 / 哪些子代理还活着"。
- **交接给人立刻释放槽位**（ready / 提问 / blocked / failed 之后）。
- 交接时子代理停止；shell 任务（如留给 review 的服务器）可继续，不占槽位、不受 30 分钟限制、
  完成也不唤醒（下次 turn 自己读结果）。

### 4.4 工单怎么结束

- `ready` 且没有 PR → 完成。
- `dropped`（发现没活可干，如问题无法复现 / 已被取代）→ 静默结束，但**保留订阅**，后续消息可恢复。
- 有 PR：工单跟随它开的每个 PR，**所有自己开的 PR 都关闭才算完**；结局取最后一次相关关闭
  （merged / closed）。
- 结束后**保留订阅**：有人在该 Thread 留言可**在同一 Mandate 下恢复**，不需要新 Eval；但被"人喊停"
  过的工单什么都不收。

### 4.5 失败与恢复语义（写得很细）

- 过路性失败（模型/网络 408、429、5xx、连接被拒）：重试 3 次、退避增长；**失败后再也不自动重试**，
  也不自动投递 CI/bot 消息，但**工具仍可用**（人可以自己在会话里继续）。
- 重启：运行中的派工以 "host restarted" 结束，新派工带恢复提示（"先看目录 / git 历史 / GitHub
  再重复任何外部动作"），**最老的工单先起**。
- **"哪些消息被读过了"可恢复**：一步在调用模型前就声明"我认领了这些消息"，被取消/报错算未读，
  随后有报告或 turn 结束才算已读。
- **Stored work**：所有"决定之后要做的事"（转发候选、投递 Input、启动派工、发消息）**先落库、后执行**；
  失败按 2s→1min 退避；同类失败连续 3 次才升级成系统问题。

---

## 5. Prompt 体系：可覆盖、有版本、会提示"默认已更新"

### 5.1 装配顺序（8 段，最后一段是 Policy）

```
1 共享指令 + 角色
2 三个团队段（Communication / Ticket scope / Delivery requirements）
3 订阅与回复
4 分派（主会话）或 工作指令（执行器）
5 runtime/admission（仅主会话）
6 交接指令
7 runtime/environment（执行环境变量）
8 runtime/policy（仅主会话，每次模型请求都重新生成）
```

> **Policy 放最后**的理由：*"It is last so a Policy change leaves the preceding prompt text
> unchanged."* ——为 prompt cache 留出稳定前缀，且这是装配规则而非事后优化。我们做对了后半段
> （`prompt_cache_debug`），但没把"顺序"当成约束写进装配器。

### 5.2 覆盖与版本

- 可覆盖部件用 `key` 命名（`runtime/shared`、`runtime/worker`、`skill/github`…）；生成段
  （`runtime/policy`、`runtime/environment`）是**模板**，必须保留占位符 `{version}/{mode}/{rules}`、
  `{variables}`。
- 每次保存 → **不可变版本**（含修订号、前驱、时间、来源 panel/tool）；保存要声明 base revision，
  冲突就拒绝、不丢草稿。
- **每个 override 记录它基于哪版内置文本**；升级后若默认文本变了，面板标 **Default updated** 并给
  diff，团队可选"保留我的 / 用新默认 / 改一改"。没覆盖的部件自动跟随新默认。
- **每次模型请求都读最新版本**（包括同一 turn 的后续步骤）；保存不重启/不取消会话；宿主记录每个
  会话用过哪些修订。

### 5.3 覆盖不了的部分（安全边界）

| 强度 | 内容 |
| --- | --- |
| 1（最强） | 工具与宿主强制的事实：身份、权限、工具参数校验、回复用途、长度上限 |
| 2 | 内置/生成段落（按团队覆盖读） |
| 3 | 主动工单的匹配规则（仅该工单执行器） |
| 4 | 团队段与团队 skill |
| 5（最弱） | 外部文本：消息、评论、文档、引用——**永远是数据，不改规则和授权** |

> 这和我们的"第四档 `MUST_DO_SELF` 不可被任何东西绕过"是同一思想，但他们写成了**五层优先级表**，
> 并明确"prompt 文本改不了第 1 层"。

---

## 6. 工程实现：单库 + effects 队列 + 幂等 outbox

### 6.1 持久化：一个 SQLite，9 张表

`trigger` / `disposition`（每个 Trigger 去哪了：被过滤的原因码、Eval、Decision、扇出到哪些工单、
被谁交回、被谁的执行器确认过）/ `trigger_thread` / `thread` / `ticket` / `decision` / `effect`
（效果队列）/ `outbox`（消息回执）/ `kv`。

两个技巧：

1. **生成列 + 索引只覆盖"活跃工作"**：`thread.active`（有排队 Trigger 或 Eval 在跑）、`ticket.open`、
   `ticket.active`，于是每一趟 pass 只读活跃行。
2. **时间统一存 `toISOString()` 文本**，"所以时间可以直接按文本比较"。

### 6.2 Effects：先落库后执行（at-least-once）

`src/app/effects.ts`：`enqueue(kind, key, payload)` 在调用者的事务里 upsert，**同 kind+key 会合并**
（保留队列位置、attempts 归零）；handler 注册时指定 `limit`（并发上限）、`retry`（默认
2000→60000ms 退避）、`ordered`（同类串行）。

### 6.3 幂等 Outbox

`send` 在**同一事务里**"保存消息回执 + 入队 reply 效果"；同一 id 永不重复入队；消息按"同工单同
会话"的顺序发出（确认先于结果），其它工单不互相等待；只有"来源不再监听该会话"或"来源答复永远送不到"
才丢弃，并记录何时丢弃、为什么。

### 6.4 事务边界

*"All Engine methods are synchronous. Each command changes its aggregates in one transaction.
`evaluateDue` uses a separate transaction for each Eval or Ticket it changes."* ——同步方法 +
明确的事务粒度，是这套系统能写清恢复语义的前提。

### 6.5 测试

689 个用例；`tests/runtime.spec.ts` 单文件 142KB、`core-engine.spec.ts` 107KB、`sources.spec.ts`
103KB；另有 `tests/fixtures/host.ts`、`panel-server.ts` 做宿主级 fixture。

---

## 7. 与我们 harness-agent 的对照

| 维度 | dsh-superagent | 我们 | 判断 |
| --- | --- | --- | --- |
| 定位 | 单仓库的主动式 coding agent（bundle） | 通用本地 agent（TUI/API/飞书 + 桌面控制 + 记忆） | 目标不同，不必追 |
| 授权 | Gate（代码、强度分级）+ Assessment（模型、逐规则留痕）+ Mandate/Policy 版本再校验 | 四档 sandbox + 审批作用域（once/times/until/session）+ 第四档硬拒 + 域名/目录授权 | **他们的"依据会失效"比我们的"作用域会过期"更本质** |
| 主动 | `off / observe / auto`，默认 observe | 主动性默认关；建议不是行动 | **observe 档值得抄**，代价小、收益直接 |
| 并发 | 每工单一个执行会话 + 槽位 + 给人预留 1 槽 | 单会话 + 子代理/队友 + 任务系统 | "给人插队"的规则可借鉴 |
| 长命令 | 前台 5 秒自动转后台；30 分钟上限；交接释放槽位 | 后台任务 + `is_slow_operation` 关键词启发式 | 他们按**角色**定 5s/60s，比按关键词猜更稳 |
| prompt | 8 段装配、可覆盖部件、版本 + Default updated + 每次请求重读 | 模块化拼装 + cache 前缀优化 | **"覆盖文本记录它所基于的内置版本"值得抄** |
| 状态机 | 概念 + 规则 + 明确拒绝的概念，落成 `stage.ts` | 模块边界清晰，但缺"显式拒绝的概念"清单 | **补一份 Rejected concepts** |
| 持久化 | 单 SQLite、9 表、生成列做活跃索引、先落库后执行 | 会话 JSONL + cache/台账文件 | 各有取舍：我们可读性更好，他们恢复语义更强 |
| 测试 | 689 用例 / 12k 行；宿主级 fixture | 3359 用例（数量占优），缺宿主级场景夹具 | **我们缺"整机重启/断电"这类恢复测试** |

---

## 8. 建议吸收的六条（按性价比排序）

1. **accept mode 加一档 `observe`（评估但只记录）**——高价值、低成本。
   我们主动性只有"关/开"。加一档"照常评估 + 记录 if-would-act 但不执行"，运营者看几天记录再开。
   落点：`src/proactive.py`，`would` 记录进台账，`decision` 由代码算（延续"通知不是行动"）。
2. **授权记录"依据"，依据变了就让授权失效**——高价值、中等成本。
   在审批台账里加"依据快照"（规则版本、命令类、目标域名、当时的白名单哈希），恢复或重放时比对，
   不一致就要求重新批准。落点：`src/approval_scope.py` + `src/audit.py`。
3. **阴性结果也要留痕（逐项 matched=false）**——低成本。
   LLM 审查官只留"放行/升级"；改成要求它对每条判断项给布尔 + 理由，缺项即 schema 错。
   落点：`src/approval.py` 的审查官 schema。
4. **"给人插队"的并发规则**——中价值、低成本。
   "人触发的唤醒"可用额外 1 个槽位，且优先于新任务拿槽。落点：任务/子代理调度。
5. **前台命令按角色自动转后台（5s / 60s），并明确"交接即释放槽位"**——中价值。
   把"什么时候转后台"从关键词启发式（`is_slow_operation`）改成按角色定阈值。
6. **补两份文档：`Rejected concepts` 与"覆盖文本的基准版本"**——低成本、长期收益。
   前者防止概念膨胀；后者让我们改内置 prompt 时不会静默覆盖用户改过的部分。

---

## 9. 不建议照搬的

1. **9 张表的事件溯源式建模**：当前规模（会话 JSONL + 台账）够用；引入 disposition/effect/outbox
   会把复杂度提一个量级，收益主要在"多来源长期运行"，不在我们当前形态。
2. **每工单一个目录 + prepare 命令（git worktree）**：他们是"一仓库、多任务并行"，我们是"一人一机"；
   worktree 隔离的收益低于运维成本（我们已有 `create_worktree`，按需用）。
3. **主会话"不许等待"的强约束**（禁 `sleep`、shell 超时 >30s 直接拒）：适合"纯路由会话"，我们是
   交互式主循环，禁掉会碍事。
4. **评审委托给外部 reviewer bot 并限 3 轮**：依赖团队已有基础设施，没必要自己造。

---

## 10. 进一步读法

- 只想看思想：`docs/000-philosophy.md`（33 行）+ `docs/001-domain.md` 的 **Rejected concepts**
- 想看授权：`docs/002-authorization.md` + `src/policy.ts` 的 `gate` 与 `revalidate`
- 想看执行：`docs/003-execution.md` + `src/executor/dsh.ts` 的 `WorkerSlots`
- 想看 prompt：`docs/004-prompts.md` + `prompts/admission.md`（这份 prompt 本身是范本）
- 工程细节：`docs/007-architecture.md` 的 Persistence / Model-facing interfaces 两节

---

## 说明：这份调研的边界

读完全部 8 篇文档与上述关键模块（`policy.ts`、`core/evals.ts`、`core/engine.ts` 摘要、
`app/assessment.ts`、`app/effects.ts`、`app/sessions.ts`、`executor/dsh.ts` 的槽位部分、
`prompts/admission.md`），**没有**逐行读 15k 行源码，也**没有**跑它的测试（需要 DSH 运行时）。
文中数字均为实测（文件行数、用例数、schema 默认值）；"建议吸收"与"不建议照搬"是我的判断。
