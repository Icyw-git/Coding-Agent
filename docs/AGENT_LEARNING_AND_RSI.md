# 越用越懂人 与 递归自我改进（RSI）：调研与设计

日期：2026-10-28。调研 Codex / dots / Muse 以及 PAHF 论文与 RSI 领域综述，
回答两个不同的问题：

1. **越用越懂人**：personal agent 如何从交互中学到偏好？
2. **RSI**：agent 如何改进自己？边界在哪？

两者常被混为一谈。实际上第一个是**学习**，第二个是**改变系统**。

---

## 一、越用越懂人

### 1.1 三家的做法

| | 机制 |
|---|---|
| **Codex** | memory 分两层：`AGENTS.md`（人写、进 git、稳定规则）+ `~/.codex/memories/`（agent 生成）。生成走**异步后台**：会话闲置 6 小时后才整理，双模型（一个决定"什么值得记"、一个合并进已有记忆），有界老化（30 天未用淘汰） |
| **dots** | 「learns your preferences, how you think, and what good looks like to you」。反馈不只是「Looks good」，而是可操作的（"优先讲用户可见的变化、内部重构别写"）。跨 ChatGPT/Slack/Teams 上下文跟着走 |
| **Muse** | 「从对话学习、反思什么重要、持续变强」。每小时更新用户与联系人档案（TIME 批评这是黑箱）；有 forget skill |

### 1.2 PAHF 论文给了最关键的设计（一手材料）

[arXiv 2602.16173](https://arxiv.org/pdf/2602.16173) 提出 Personalized Agents from
Human Feedback。核心是**三步循环 + 双反馈通道**：

1. **Pre-Action**：行动前澄清歧义（"你说的是哪个 Kate？"）
2. **Grounding**：用从记忆里检索的偏好约束行动
3. **Post-Action**：行动后，用户看到结果再给反馈，写回记忆**纠正**偏差

论文的结论非常明确：

> The final and most critical step is Post-Action Feedback Integration. This
> reactive loop is the essential mechanism for correcting miscalibrated beliefs
> and adapting to non-stationarity, **which pre-action queries alone cannot solve**.

> explicit memory combined with post-action feedback is particularly important
> for robust personalization **without pre-existing user data**

论文里的例子很说明问题：

```
Day 1: "Kate 喜欢什么饮料?" → "可乐"  → 记下「Kate 喜欢可乐」
Day 2: "我困了，给我杯饮料"  → 送上可乐 → 用户："我困的时候更想喝茶"
                                          → 记忆更新：条件性偏好
```

**关键在 Day 2**：如果只记「Kate 喜欢可乐」，第二天就永久错了。正确的记忆是
**「平时可乐，困了喝茶」**——这只能从 post-action 反馈里学到。

### 1.3 我们缺什么（实测）

查了 `src/memory.py` 的 `extract_memories`：

```python
# 记忆窗口：会话开头前 10 条（用户偏好通常最先说）+ 结尾最后 10 条
window = messages[:10] + messages[-10:]
...
dialogue_parts.append(f"{role}: {content}")   # 只拼对话文本
```

三个真实缺口：

**缺口 A：没有 post-action 反馈通道**

我们的抽取只读**对话文本**，不看「工具执行了什么、用户对它反应如何」。
模型写错了代码、用户重写了，这条信息不进记忆。

这正是 PAHF 说的"pre-action queries alone cannot solve"——我们连 pre-action
都没有，只有"事后单次总结"，而且总结的是对话而不是结果。

**缺口 B：抽取类型里没有"纠正"这一档的高优先级**

现有 type 是 `user` / `feedback` / `project` / `reference`。`feedback` 语义太宽：
既可能是"下次这样写"，也可能是"这次还行"。而 M1 我加的
`correction`（用户纠正过我）**比一条普通偏好重要得多**——它说明模型此前的
判断是错的，必须覆盖而不是并存。

**缺口 C：记忆不区分「无条件」与「条件性」偏好**

「喜欢可乐」和「困了更喜欢茶」是两种形态。PAHF 的例子说明条件性偏好才是
真实世界的主流，而我们没有结构表达它（只能写成一句话正文，召回时靠关键词碰）。

### 1.4 设计：把三条补上

**D1：post-action 反馈通道（最高价值）**

在回合结束后，除了现有的 `extract_memories(messages)`，再喂一份
**行动摘要**：本回合用了哪些工具、改了什么文件、结果如何（成功/失败/被拒）。

```python
# 新增 memory.feedback_signals(messages, tool_events) -> str
# 提炼「模型做了什么 → 用户之后说了什么」的配对
```

判据只做确定性检测，不额外调模型：

| 信号 | 判定 |
|---|---|
| 用户紧接着说「不对/不是/重来/我说的是」 | 强纠正信号 |
| 用户用 `edit` 改了模型刚写的文件 | 隐式纠正 |
| 工具返回 error 后用户换了完全不同的做法 | 失败反馈 |
| 用户对结果说「好/可以/就这样」 | 正反馈（低权重） |

这些信号拼进抽取提示词，让模型知道"刚才发生了纠正"，产出的 `correction` 类
记忆权重更高（合并时优先于普通偏好）。

**D2：`correction` 高优先级 + 覆盖语义**

合并规则加一条：`correction` 类记忆与同主题的旧记忆冲突时，**删旧留新**，
而不是并存。理由：一条"以为 A 实际是 B"的记忆和一条"是 A"的记忆同时存在，
召回时可能取到错的那个。

**D3：条件性偏好的结构化表达**

不引入复杂 schema，只在 `remember` 的 type 里加一个语义约定：
`preference` 的 description 允许写成 `when <条件>: <偏好>`。
召回是关键词匹配，条件词（"困了"/"周三下午"）本身就是好关键词。

**D4：`/why <记忆名>` 解释来源**

TIME 那篇调查的教训：不可观察的长期记忆会变成黑箱。每条记忆要能回答
"为什么记这个"。实现上在记忆 frontmatter 里加 `source_session` 与
`source_quote`（用户原话片段），`/why` 展示。

这样"它为什么觉得我周三不看代码"就有答案，而不是让人猜。

---

## 二、RSI（递归自我改进）

### 2.1 必须先分清三层（Phil Schmid 的定义，2026-08）

> **Iteration**：改代码、跑测试，输出变了，**系统不变**。
> **Self-improvement**：加一个工具、记一条技能，**系统变了，尺子不变**。
> **Recursive self-improvement**：系统**连尺子一起抬高**——而且仍然改不动那个尺子。

关键判据：

> Each loop is defined by which layer persists after the run.
> A rising score proves **self-improvement**. Recursion is a claim about
> **whether the next round faces a harder, still-honest bar**.

**目前没有任何公开系统做到第三层。** 现有系统全是第二层：agent 能改 prompt、
skill、tool、harness 代码，但**评估器在环外、agent 改不到**。

> The moment the system owns the ruler, it will get very good at looking improved.

### 2.2 三家在 RSI 光谱上的位置

| 系统 | 可改范围 |
|---|---|
| **保守**（Claude Code / Codex / Cursor） | 固定内核 + skills/hooks/plugins 扩展 |
| **Pi** | 4 个内建工具（read/write/edit/bash）+ 其余全是 TypeScript 扩展，agent 可写可重载 |
| **DeepSeek** | 极端：models/tools/sessions/sandboxes/control loop 全是可换插件 |

Muse 自述「builds its own tools, and edits itself」，属于第二层偏 Pi 那一侧。

**代价**：

> The same properties make failures persistent. 一个能改自己的 agent，它的错误
> 也会被自己固化下来。

### 2.3 我们现在的真实位置（实测）

| 能力 | 状态 |
|---|---|
| 技能系统 | 有读（`scan_skills` / `load_skill`），**没有写工具** |
| 技能目录 | `SKILLS_DIR = WORKDIR/"skills"` ——**在工作区内，agent 有写权限** |
| 动态加载 Python 扩展 | **没有**（只有 `find_spec` 检测，无 importlib 加载） |
| 评估器 | **2539 条 pytest**，agent 改不到（`tests/` 不在工作区写授权里） |
| 自我改进记录 | 无 |

**判断**：我们在**第二层的入口处**。技术上 agent 能用 `write` 往
`skills/` 写一个 SKILL.md 然后 `load_skill` 读到它——这已经是"加技能"的
自改进，只是没有专用工具、没有验证、没有记录。

**这其实是个好位置**：评估器（pytest）在环外、只读、诚实。这正是
RSI 领域公认的**唯一能安全改进的前提**。

### 2.4 设计：做第二层，不做第三层

明确的取舍：

**做**（有限自改进，收益真实、风险可控）：

- **S1 技能自建**：`skill_write(name, content)` 工具，写进 `skills/`。
  必须审批（新增可执行指令面）。技能内容**不能改 harness 代码**。
- **S2 自改进需验证**：写技能的同一个回合，要求在 `tests/` 里有对应断言
  或者手工跑一次并记录结果。没验证的技能不算"改进"。
- **S3 记录与回滚**：每次自改进写一条 `self_improved` 审计事件
  （改了什么、为什么、验证结果）；`/improvements` 列出历史；
  一键回滚到上一版。
- **S4 技能不进 git 自动提交**：留在工作区，人审后再提交。

**不做**（红线）：

| 不做 | 原因 |
|---|---|
| agent 改自己的策略/审批/权限代码 | `never_do` 与 `POLICY_KEYS` 的意义就是这些不能被自我修改 |
| agent 改 tests/ | 评估器一旦可改，所有"改进"都不可信——这是 RSI 的第一原则 |
| agent 写 `.pi/extensions` 式可执行插件并自动加载 | 需要沙箱与能力边界设计，个人单机收益不抵风险 |
| 自动调高"什么算更好"的标准 | 第三层，公开系统都没做到；没有 taste 的抬标只会更快优化错的东西 |

### 2.5 为什么不做第三层

Phil Schmid 的总结值得抄下来：

> **Verification is the bottleneck.** Every convincing result has a ruler the
> system does not own.
> **Taste still lives outside the loop.** Agents will raise any number you give
> them. RSI without taste is a faster way to optimize the wrong thing.
> Deciding what counts as a win — and that reward hacking does not — is still
> our job.

还有一个实证：**PAST-Bench 发现很多场景下打开记忆并不带来收益**。这说明
"加机制"本身不等于"变好"，而**判断"有没有变好"的能力（taste）不在机制里**。

对我们的直接含义：**不要为了"能自我改进"而加自我改进**。先让 agent
更懂人（第一部分），那有明确可验证的目标（用户少重复几次、纠正次数下降）。

---

## 三、优先级

| 编号 | 内容 | 价值 | 风险 | 默认 |
|---|---|---|---|---|
| **D1** | post-action 反馈通道 | 高（PAHF 证明是纠错核心） | 低（只读分析，不新增写面） | 开 |
| **D2** | `correction` 覆盖语义 | 高 | 低 | 开 |
| **D4** | `/why` 记忆来源 | 中（防黑箱，TIME 教训） | 低 | 开 |
| **D3** | 条件性偏好写法 | 中 | 极低（约定，非 schema） | 开 |
| **S1** | `skill_write` 工具 | 中 | 中（新增指令面，需审批） | 开 |
| **S3** | 自改进审计 + 回滚 | 中 | 低 | 开 |
| **S2** | 自改进需验证 | 中 | 低（流程约束） | 开 |
| S4 | 技能不自动提交 git | — | — | 开 |

**先 D1 + D2**：它们直接对应"越用越懂人"的核心机制，且不需要新增写权限面。

**S 系列整体后置**：自改进的价值取决于"能判断改得对不对"，而我们现在只有一个
粗糙的评估器（pytest 全绿）。先让 D 系列跑一段时间，积累"什么样的记忆真的有帮助"
的数据，再考虑让 agent 改自己。

---

## 四、参考

- PAHF：Personalized Agents from Human Feedback（三步循环 + 双反馈通道）：
  <https://arxiv.org/pdf/2602.16173>
- RSI 三层定义与"验证者是瓶颈"：<https://www.philschmid.de/recursive-self-improvement>
- RSI 研究地图（含 PAST-Bench：记忆不一定带来收益）：
  <https://github.com/lobehub/awesome-rsi>
- Codex memories 官方文档（两层、异步整理、老化）：
  <https://learn.chatgpt.com/docs/customization/memories>
- dots（学习偏好与标准、跨渠道上下文）：<https://openai.com/index/introducing-dots/>
- Muse（自我反思、可遗忘）：<https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/>
- Muse 隐私调查（不可观察记忆的代价）：<https://time.com/article/2026/10/06/meta-muse-ai-agent-privacy/>
