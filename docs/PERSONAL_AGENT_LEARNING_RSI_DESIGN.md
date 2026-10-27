# 偏好学习 与 RSI：完整设计方案

日期：2026-10-28。承接 [AGENT_LEARNING_AND_RSI.md](AGENT_LEARNING_AND_RSI.md)（调研），
本文是**可执行的架构设计与实现方案**。

两部分独立：

- **Part A 偏好学习**：怎么越用越懂人。核心是"反馈信号 → 记忆 → 检索 → 约束行动"的闭环。
- **Part B RSI**：怎么让 agent 改进自己。核心是"谁能改尺子"。

---

# Part A · 偏好学习

## A.0 一手调研结论（这决定设计）

| 来源 | 结论 | 对我们的含义 |
|---|---|---|
| **PAHF**（arXiv 2602.16173） | 三步循环：pre-action 澄清 → 用记忆 grounding → **post-action 反馈写回**。最后一步才是纠错核心，"pre-action queries alone cannot solve" | D1 已做（`memory_feedback.py`），但只到"喂提示词"，没有写回策略 |
| PAHF | 记忆条目：`{text, last_update_date, keywords}` + embedding；API 只有 add / retrieve top-k / **near-duplicate 检测以决定 update vs add** | 我们没有 `last_update_date`、没有 keywords、没有 update-vs-add |
| PAHF | **两层**：session notes 合并后清除 → global memory 只留 durable | 我们 session 与 global 混在一起 |
| PAHF | Consolidation：合并重复、**冲突按 last_update_date 取最新**、清理低信号 | 我们有合并，但无显式时间戳可依 |
| PAHF | "如果不做细致剪枝，记忆会积累冗余与过时信息，**反而降低质量**" | 我们的剪枝只有 50 条上限 + 90 天老化 |
| PAHF Evals | Distillation（precision / recall / safety）；Injection（**recency correctness / over-influence / token efficiency**） | 我们一条都没有 |
| **EvoMemBench**（arXiv 2605.18421） | **FAMA** = max(0, MPA − λ·(1−FAA))，λ = N_forget/(N_presence+N_forget)。"**Failure to Forget** is a major issue"；agent 必须 "forgetting-aware" | 需要一个惩罚"依赖过时记忆"的指标 |
| PAHF Evals | **over-influence**：记忆是否错误覆盖了当前用户意图 | ⚠️ 我们的注入位置正好有这个风险（见 A.4） |

## A.1 现状对照

| 维度 | 我们现在 | PAHF | 差距 |
|---|---|---|---|
| 条目字段 | `name` / `description` / `type` / `source_session` / `source_quote` | `text` / `last_update_date` / `keywords` / embedding | 缺显式时间戳、关键词、向量 |
| 检索依据 | 只对 `name + description` 打分 | 全文 dense retrieval | **正文与关键词不参与打分** |
| 作用域 | 无（session/global 混在一起） | session → global 两段 | 缺分层 |
| 写入策略 | 直接写文件（同名覆盖） | near-duplicate → update vs add | 缺去重判定 |
| 冲突 | 合并时 LLM 自行决定 | 按 `last_update_date` 取最新 | 规则不确定 |
| 剪枝 | 50 条上限 + 90 天老化 | stale / overwritten / low-signal | 缺信号维度 |
| 注入位置 | **请求最末尾**（用户消息之后） | — | ⚠️ over-influence 风险 |
| 评估 | 无 | precision / recall / safety / recency | 完全缺失 |

实测我们库里 20 条记忆：`project` 13、`user` 5、`feedback` 1、`reference` 1。
**65% 是 project 类**——按 PAHF 的 durability 原则，这正是最容易过时的一类，
而它们都没有时间戳。

## A.2 记忆 schema v2

### A.2.1 字段设计

frontmatter 扩展（保持向后兼容：旧文件缺字段都有默认值）：

```yaml
---
name: user-sleepy-tea
description: "when sleepy: prefers tea over cola"     # 条件写成 when <cond>: <pref>
type: preference            # preference | correction | fact | context
scope: global               # global | session   ← 新增
keywords: sleepy,tea,drink  # 新增；检索用，逗号分隔
confidence: 0.8             # 新增；0..1，来源决定初值
last_update_date: 2026-10-28  # 新增；ISO 日期
supersedes: user-cola-pref  # 新增；这条修正了哪一条（可空）
source_session: sess-abc    # 已有
source_quote: "我困的时候更想喝茶"  # 已有
---
```

**为什么加 `keywords` 而不是直接上 embedding**：
- 我们的召回是本地关键词匹配（零网络往返，省 ~2s/轮）。引入 embedding 要么每次
  调一次 API（违反本地优先），要么本地跑模型（依赖重）。
- `keywords` 由抽取时顺手产出，成本为零，且能让**正文里的词参与检索**——这直接
  解决"条件偏好写在正文、名字里没有关键词就召回不到"的问题。
- 将来真要向量检索，`keywords` 就是现成的稀疏索引，不冲突。

**为什么 `last_update_date` 必须显式**：
现在用文件 mtime，但召回记录写在独立 json 里（避免扰动 mtime）——一旦需要
"哪条最新"的确定性排序，mtime 不可靠。

### A.2.2 写入策略（update vs add）

PAHF 的 near-duplicate 检测，我们不做向量，做**确定性近似**：

```
写入一条新记忆 new：
1. 计算 new 与库里每条 existing 的相似度：
   sim = Jaccard(new.keywords, existing.keywords)
       + 名字/描述二元组重叠加权
2. sim ≥ 0.8  → UPDATE：保留 existing 的文件名（不新建文件），
                 合并 keywords、刷新 last_update_date、confidence 取大
3. 0.5 ≤ sim < 0.8 → SUPERSEDE（仅当 new.type == correction）：
                 写新文件，旧文件 frontmatter 加 superseded_by，移入 .archive
4. sim < 0.5 → ADD
```

**关键点**：`correction` 类走 SUPERSEDE 而不是 ADD。这是 D2 的落地方式——
之前只在合并提示词里写了规则，现在写入时就直接执行，不依赖下次合并。

### A.2.3 两层作用域（session → global）

```
session 层：本会话内新学到的（scope: session）
           检索时可用，但**不跨会话**
consolidation 时：判定 durability
   durable（跨场景成立）  → 提升为 scope: global
   非 durable（本次一次性）→ 丢弃或留 session 归档
```

PAHF 原文例子：一趟旅行里说"这次不想托运行李"，被**故意丢弃**，因为它
explicitly scoped to that specific trip。

**判定规则**（确定性，不额外调模型）：

| 信号 | 判定 |
|---|---|
| 含"这次/本次/今天/这一回" | 非 durable |
| type == `context` 且无时间跨度的表述 | 倾向非 durable |
| 含"以后/总是/我一直/每次都" | durable（强） |
| 含条件（`when X`） | durable（条件也会重复出现） |
| 同一内容在 ≥2 个不同会话出现过 | durable（强） |
| 其他 | 默认 durable（宁可留，我们的量小） |

### A.2.4 注入：修正 over-influence

**现状问题**：`inject_memories` 把记忆块追加在**用户消息之后**（为了保住前缀缓存）。
模型对末尾内容权重高，于是"旧偏好"可能压过"用户刚说的"。

这不是理论问题——PAHF 的 injection eval 专门测这个：
> Over-influence: did memory incorrectly override current user intent?

**修法（不动位置，只加一句话）**：记忆块内部首行加优先级声明：

```
<relevant_memories>
Priority: the user's latest message always overrides these notes when they
conflict. These are background preferences for use when the request is silent
or ambiguous.
...
</relevant_memories>
```

这样既保住前缀缓存（记忆仍在末尾），又显式降低其优先级。

**为什么不用"把记忆移到用户消息之前"**：那会改写历史消息，作废整个前缀缓存
（每轮召回变化 → 全部重算）。这个取舍已经在代码注释里论证过，不推翻。

### A.2.5 检索改进

现状：只对 `name + description` 打分，阈值 2。

改进三处：

```
1. 打分字段扩为 name + description + keywords
   （body 不参与：正文长，会把稀有词权重稀释，且召回噪声大）
2. 加时间衰减：score *= (0.9 ** (age_days / 30))
   —— 30 天前的同分记忆排在后面，但不会消失
3. correction 类加权 ×1.3
   —— 用户纠正过的信息比普通偏好的优先级高（这正是它存在的意义）
```

### A.2.6 遗忘：FAMA 意识

EvoMemBench 的 **FAMA = max(0, MPA − λ·(1−FAA))** 说明：只测"记住没有"会奖励
"什么都记"。必须同时测"该忘的忘了没有"。

落到实现：

- `superseded_by` 指向的旧记忆 → **立即归档**，不再参与检索（不算"记着"）
- 老化（已有 90 天）之外，加**低信号剪枝**：从未被召回且 confidence < 0.3 且
  超过 30 天 → 归档
- 归档不删除（既有约束）

## A.3 实施方案（Part A）

### A.3.1 模块划分

```
src/memory_schema.py     新：字段解析/序列化、keywords 归一、相似度计算
src/memory_write.py      新：写入策略（ADD/UPDATE/SUPERSEDE）、durability 判定
src/memory.py            改：_render_memory 用 schema；list_memory_files 返回新字段；
                            local_relevant_memories 扩字段 + 衰减 + 加权；
                            inject_memories 加优先级声明
src/memory_feedback.py   已做（D1）：无需改动
src/memory_aging.py      改：加低信号剪枝
tests/test_memory_schema.py / test_memory_write.py / test_memory_injection.py  新
```

### A.3.2 分阶段（每阶段独立可回滚）

| 阶段 | 内容 | 风险 | 验证 |
|---|---|---|---|
| **A1** | 注入优先级声明（一句话） | 极低 | 现有召回测试 + 新增断言 |
| **A2** | schema v2 字段（keywords / last_update_date / confidence / scope） | 低（向后兼容） | 旧文件仍能读 |
| **A3** | 检索改进（扩字段 + 衰减 + correction 加权） | 中（召回结果会变） | 对比召回命中测试 |
| **A4** | 写入策略（update vs add vs supersede） | 中（会改文件集合） | 去重/覆盖测试 |
| **A5** | 两层作用域 + durability 判定 | 中 | session 不外泄测试 |
| **A6** | 低信号剪枝 | 低（只归档） | 归档而非删除 |

**建议先做 A1 + A2**：A1 是安全修正（一处文案），A2 是纯加字段。A3 之后每次
改动都会影响召回质量，需要真实使用数据配合。

### A.3.3 记忆评估（缺失但必需）

按 PAHF 的 eval 框架，建 `tests/test_memory_eval.py` —— 不是单测，是**场景回归集**：

```python
CASES = [
    # (历史交互, 新请求, 期望行为, 禁止行为)
    {
      'history': ['Kate 喜欢可乐'],
      'new': '我困了，给我杯饮料',
      'correction': '我困的时候更想喝茶',
      'expect': '记下 when sleepy: tea，且原「喜欢可乐」不被删除',
      'forbid': '覆盖成「喜欢喝茶」',
    },
    {
      'history': ['这次旅行不想托运'],
      'new': '(会话结束)',
      'expect': '不提升为 global',
      'forbid': '变成长期偏好',
    },
    {
      'history': ['部署走 make ship-prod'],
      'new': '部署到哪个环境',
      'expect': '召回该条',
      'forbid': '召回无关的 project 记忆',
    },
]
```

三个指标（对应 PAHF）：

- **Precision**：写入的记忆里 durable 的比例（抽查）
- **Recency**：冲突时用到最新那条的比例
- **Over-influence**：用户当轮明确指示被旧记忆覆盖的次数（**应为 0**）

---

# Part B · RSI

## B.0 调研结论

### B.0.1 三层定义（Phil Schmid, 2026-08）

| 层 | 系统 | 尺子（verifier） |
|---|---|---|
| **Iteration** | 不变 | 不变 |
| **Self-improvement** | **持久改变** | 不变 |
| **Recursive** | 改变 | **也抬高，且仍改不动** |

判据：
> A rising score proves **self-improvement**. Recursion is a claim about whether
> the next round faces a **harder, still-honest bar**.

**公开系统无一做到第三层。** 原因：
> The moment the system owns the ruler, it will get very good at **looking** improved.

### B.0.2 六个可复用实践

| 来源 | 实践 |
|---|---|
| **autoresearch**（本机 skill） | 有界循环：固定指标 + 方向 → 隔离（独立 worktree）→ 跑 → keep/discard → 记录 jsonl → 收敛窗口（连续 N 轮无新最优即停）→ 硬限额（runs/runtime/cost） |
| **Hyperagents** | task agent + **可编辑的 meta-agent**，且"自我修改的程序本身"也能演化并跨域迁移 |
| **HarnessOpt-Bench** | 问"增益能否通过更强测试"；111 次运行、5 个优化器模型、4 个任务 → **结果差异极大**，说明不是万能 |
| **SWE-Gym verifiers** | 提供可执行仓库任务与轨迹，训练"推理期 verifier"；**verifier 确认过的轨迹**优于技能包（七个模型上都成立）→ 增益来自**已验证的来源** |
| **Archive-based search** | 贪婪选择会剪掉"当时差、后来成为最佳父本"的变体；保留档案 |
| **PAST-Bench** | 很多场景下**打开记忆并无收益** → "加机制"≠"变好" |

### B.0.3 关键洞察

> **RSI 不要求更好的模型。** A frozen model with an **honest verifier** and a
> **writable environment** can climb on its own. The easier target is the harness.

> **Verification is the bottleneck.** Every convincing result has a ruler the
> system does not own.

> **Taste still lives outside the loop.** Agents will raise any number you give
> them. RSI without taste is a faster way to optimize the wrong thing.

## B.1 现状与定位

| 能力 | 现状 | 判定 |
|---|---|---|
| 技能系统 | `scan_skills` / `load_skill` 只读 | 有载体，无写入 |
| 技能目录 | `SKILLS_DIR = WORKDIR/"skills"`，在工作区内 | agent 技术上能写 |
| 动态加载 Python 扩展 | 无 | 不做（需沙箱+能力边界） |
| 评估器 | **2575 条 pytest**，`tests/` 不在写授权内 | ✅ 诚实且 agent 改不到 |
| 自改进记录 | 无 | 缺 |
| 回滚 | 无 | 缺 |

**定位：第二层（self-improvement），不碰第三层。**

理由不是保守，而是 B.0.3 的三条：我们有一个 agent 改不到的诚实评估器（前提具备），
但**没有 taste**（不知道怎么判断"更好的记忆/技能"），而"抬高标准"恰恰需要 taste。

## B.2 架构：三环分离

核心设计原则：**谁都能改，唯独改尺子的那条路必须是死路。**

```
┌─────────────────────────────────────────────────────────────┐
│  Ring 0 · 不可变（Immutable）                                │
│  exec_policy / hooks 审批链 / never_do / POLICY_KEYS         │
│  tests/（评估器）                                             │
│  → agent 无法写、无法审批绕过、无工具可达                        │
└─────────────────────────────────────────────────────────────┘
              ▲ 只读引用
┌─────────────────────────────────────────────────────────────┐
│  Ring 1 · 需审批的自我改进（Gated）                            │
│  skills/*.md（技能文本）                                       │
│  AGENTS.md（项目约定）                                         │
│  → 写入必须过审批；改动进 ledger；可一键回滚                      │
└─────────────────────────────────────────────────────────────┘
              ▲
┌─────────────────────────────────────────────────────────────┐
│  Ring 2 · 自由（Free）                                        │
│  记忆（memories/）                                             │
│  会话、任务、todo、工作区文件                                     │
│  → 正常权限模型即可                                             │
└─────────────────────────────────────────────────────────────┘
```

**为什么 tests/ 必须在 Ring 0**：一旦 agent 能改测试，"所有测试通过"就不再是
证据。这是 B.0.1 的直接推论，也是我们唯一真实拥有的 verifier。

### B.2.1 已有的 Ring 0 保护

- `never_do`（第四档）：改密码/支付/2FA 只能人做
- `POLICY_KEYS` 只认进程启动环境：agent 改 `.env` 也不能降档
- `tests/` 不在工作区写授权（`PATH_OUTSIDE` / 需显式 grants）
- 审批作用域 N7：默认 `once`，不会"批一次免永久"

**缺口**：没有一条**显式规则**说"agent 不得修改评估口径"。现在靠的是路径权限，
而不是语义约束。→ 见 B.3.1。

## B.3 实现方案（Part B）

### B.3.1 `verifier_guard.py`：评估器保护（新，关键）

不是靠路径权限（`tests/` 也可能被 grant 授权），而是**语义级硬拒**：

```python
# 沿用 never_do 的第四档机制（不可审批、不进队列）
PROTECTED = ('tests/', '.github/workflows/', 'pytest.ini', 'ruff.toml')
# 命中即 MUST_DO_SELF：改测试、改 CI、改 lint 配置只能人做
```

理由：`tests/` 一旦可改，`pytest 全绿` 就失去意义，整个 RSI 的地基没了。
这条比"禁止改 .env"更本质。

**与既有规则的关系**：D 系列已有"`.git` / `.hg` / `.svn` 归 `PATH_SECRET`"，
同一个思路——改完立即生效的东西最危险。

### B.3.2 `self_improve.py`：有界改进循环

照 autoresearch 的骨架，但只作用于 Ring 1：

```python
class ImprovementSession:
    """一次有界的自我改进尝试。

    契约（照 autoresearch）：
    - 明确的目标指标 + 方向（higher/lower）
    - 硬限额：轮数、时长、改动文件数
    - 收敛窗口：连续 N 轮无新最优即停
    - 隔离：改动只落在 Ring 1 的路径
    """

    def begin(self, goal: str, metric: str, direction: str,
              max_rounds: int = 5) -> str: ...
    def propose(self, change: str) -> str:      # 提出一个改动（不落盘）
    def measure(self) -> dict:                   # 跑固定命令，取指标
    def decide(self, action: str) -> str:        # 'keep' | 'discard'
    def finish(self) -> dict:                    # 汇总，写 ledger
```

**指标从哪来**（关键，决定它是否有意义）：

| 指标 | 命令 | 方向 | 用途 |
|---|---|---|---|
| `pytest_pass` | `pytest -q -m "not slow"` | higher | 正确性底线（回归即 discard） |
| `ruff_errors` | `ruff check .` | lower | 代码规范 |
| `correction_rate` | 审计事件统计 | lower | **偏好学习质量**（A 部分的真实指标） |
| `tokens_per_turn` | usage 事件 | lower | 成本 |

**`correction_rate` 是最重要的一个**：它是 Part A 与 Part B 的接点——如果偏好
学习真的有效，用户纠正次数应该下降。这给了 RSI 一个**诚实的、外部定义的**指标
（来自用户行为，不是 agent 自己编的）。

### B.3.3 `changes.py`：改动台账与回滚

```python
def record(change: dict) -> str:
    """append-only jsonl：{ts, session_id, ring, paths, diff_stat,
    metric_before, metric_after, decision, reason}"""

def rollback(change_id: str) -> str:
    """恢复到该改动之前的文件内容（只回滚它碰过的路径）"""
```

**必须 append-only**：与既有会话事件流同一原则。改动的历史本身不可变，
否则"自我改进"的记录也可以被改，等于没有记录。

### B.3.4 反奖励作弊（最重要的一节）

B.0.3 的核心警告："系统一旦拥有尺子，就会很擅长看起来变好了。"

四条防线：

| 防线 | 机制 |
|---|---|
| **尺子在环外** | `tests/` 在 Ring 0，`verifier_guard` 硬拒修改 |
| **留出集** | `tests/holdout/`（不进默认 pytest 跑，只在改进评估时跑），agent 连读都受限 |
| **改动可审计** | ledger append-only，`/improvements` 可查全部历史 |
| **回归即回滚** | 任何 Ring 0 指标变差 → 自动 discard + 回滚 |

**不做的事**（明确记录，避免以后有人"顺手加上"）：

- ❌ 让 agent 决定"什么算更好"
- ❌ 让 agent 修改收敛判据、阈值、测试权重
- ❌ 让 agent 写可执行插件并自动加载
- ❌ 自动抬高 holdout 难度（第三层，需要 taste）

### B.3.5 人类审批点

Ring 1 的每次写入都要审批，但要**可读**（复用既有审批面）：

```
[改进建议] 修改 skills/deploy.md
  指标：correction_rate 0.32 → 0.28（5 轮实测）
  改动：+12/-3 行
  验证：pytest 2543 passed
  风险：低（仅技能文本，不涉及策略/测试）
  [批准] [拒绝] [看完整 diff]
```

沿用 N7 作用域：这类改动默认 `once`，不给"永久允许"。

## B.4 阶段与优先级

| 阶段 | 内容 | 前置 | 价值 |
|---|---|---|---|
| **A1** | 注入优先级声明 | 无 | 高（修 over-influence，一句话） |
| **A2** | 记忆 schema v2 字段 | 无 | 高（A3/A4/A5 的基础） |
| **B1** | `verifier_guard`（保护 tests/） | 无 | **极高**（RSI 地基） |
| **A3** | 检索改进 | A2 | 中 |
| **A4** | 写入策略 update/add/supersede | A2 | 高（去重/覆盖是核心） |
| **A5** | 两层作用域 | A2/A4 | 中（防过时污染） |
| **A6** | 低信号剪枝 | A2/A5 | 中 |
| **B2** | `changes` 台账 + 回滚 | B1 | 高（自我改进的前提） |
| **B3** | `self_improve` 循环 | B1/B2 | 中（先有 A 的数据才有意义） |
| **B4** | 记忆评估集 | A2 | 高（否则 A3-A6 无法验证） |

**建议顺序：A1 → B1 → A2 → B4 → A4 → A3 → A5 → A6 → B2 → B3。**

理由：
- A1 和 B1 都是"一句话级"改动但价值最高（一个修安全风险、一个立地基）
- B4（评估集）必须在 A3-A6 之前，否则改完召回不知道是好是坏
- B2/B3 放最后：**没有 A 的数据，自改进就是无源之水**

## B.5 风险与不做的理由

| 风险 | 缓解 |
|---|---|
| 记忆检索变复杂后召回变慢 | keywords 是稀疏索引，不引入向量；加 bench 守住 `evaluate` 级别的延迟 |
| schema 迁移破坏旧记忆 | 所有新字段有默认值；旧文件照常读 |
| 自改进变成"看起来在改进" | holdout + ledger + verifier_guard |
| **过度工程**（加了一堆机制但没用） | PAST-Bench 教训：先做评估集，用数据决定后续 |

**明确不做**：

| 不做 | 原因 |
|---|---|
| 向量检索 / embedding | 本地优先，每次调 API 违反零往返；keywords 已够 |
| agent 自改策略/审批代码 | Ring 0 |
| agent 自改 tests | 尺子必须诚实 |
| 自动加载可执行插件 | 需要沙箱与能力边界，个人场景收益不抵风险 |
| 自动抬高标准（第三层） | 公开系统都没做到；没 taste 的抬标会更快优化错的东西 |
| 全量重写记忆存储 | md + frontmatter 足够；迁移成本 > 收益 |

---

# 附：Part A 与 Part B 的接点

两者不是并列关系，而是一个闭环：

```
Part A 产生数据（correction_rate、召回命中、记忆 precision）
        ↓
Part B 用这些数据判断"改动是否真的更好"
        ↓
改动的对象只能是 Ring 1（技能/AGENTS.md）
        ↓
Ring 0（策略/测试）永远不动
```

**如果 Part A 的评估集（B4）没建起来，Part B 就不该启动**——否则 RSI 会优化
一个我们自己都测不准的东西。这是本文档最重要的一个判断。

---

# 参考

- PAHF：Personalized Agents from Human Feedback —
  <https://arxiv.org/pdf/2602.16173>
  （三步循环、双通道、记忆 schema、update-vs-add、两层记忆、蒸馏/注入评估）
- EvoMemBench / FAMA：Forgetting-Aware Memory Accuracy —
  <https://arxiv.org/html/2605.18421v2>
- RSI 三层定义与"验证者是瓶颈" —
  <https://www.philschmid.de/recursive-self-improvement>
- RSI 研究地图（Hyperagents / HarnessOpt-Bench / SWE-Gym verifiers / archive search）—
  <https://github.com/lobehub/awesome-rsi>
- autoresearch（本机 skill）：有界循环、固定指标、keep/discard、收敛窗口
- Codex memories（两层、异步整理、老化）—
  <https://learn.chatgpt.com/docs/customization/memories>
