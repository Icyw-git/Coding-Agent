# 第三轮规划 · 补充调研（实现层对照）

接 [PERSONAL_AGENT_ROUND3.md](PERSONAL_AGENT_ROUND3.md)。第一版只看了产品层发布说明，
读到 Codex memory 的实现文档后，发现**我们和它的差距比原先判断的小，但性质不同**。
这份文档修正第一版的三处误判，并补上四个新缺口。

---

## 1. 修正：第一版说错的地方

### 1.1 「记忆没有一等公民入口」——机制有，工具没有

第一版写「`memory.py` 有读写但模型没有工具」，只对了一半。实际核查：

| 能力 | 我们 | 触发方式 |
|---|---|---|
| 自动抽取 | ✅ `extract_memories(messages)` | **每回合结束**（`harness.py:803`） |
| 合并去重 | ✅ `consolidate_memories()` | 每回合结束，紧接着抽取 |
| 索引重建 | ✅ `_rebuild_index()` | 合并后自动 |
| 召回注入 | ✅ `inject_memories()` | 每回合请求末尾 |
| **显式「记住这个」** | ❌ 无工具 | 只能靠自动抽取或 `write` 手写 md |

所以缺的不是机制，是**用户/模型主动指定**这条路径。区别在：

- Codex 的 `Memory` 是**纯自动**（后台摘要），没有 `remember` 工具；
- ChatGPT 的 saved memories 是**显式**（"remember that I..."），且能搜、能排序、有版本历史；
- 我们目前只有自动那一半。

结论不变（M1 仍要做），但**理由要改**：不是"没有记忆"，而是"记忆不可控、不可查、不可点名"。

### 1.2 「我们比 Codex 简单」——其实更激进

Codex 的整理时机（官方文档原文）：

> Codex waits until a chat has been **idle long enough** to avoid summarizing work
> that's still in progress. ... A session has to be idle for **several hours
> (six by default)** before it becomes eligible for consolidation.

我们的 `extract_memories` + `consolidate_memories` **每回合结束就跑**。

这是设计取向差异，不是缺陷：Codex 是"等定稿再总结"，我们是"边聊边记"。但我们的
方向有代价——**长回合谈到一半的临时结论会被当成事实记下来**。所以第一版 M1 说的
"记什么"之外，还要补"什么时候记"：

- 建议：抽取仍然每回合跑（够便宜），但**合并降频**（比如同一会话 5 回合内不重复
  合并），并允许 `remember` 显式写入走"立即生效"路径。
- 不照抄 Codex 的 6 小时闲置：单人本地使用，等到闲置时模型可能已经不在了。

### 1.3 「AGENTS.md 等价物缺失」——我们已有

`src/project_context.py` 第 1 行就写着「从当前工作区读取项目说明文件（AGENTS.md /
CLAUDE.md）」，且处理了 `AGENTS.override.md` 覆盖与符号链接逃逸。这块**不用做**。

---

## 2. Codex memory 的完整实现（一手细节）

来源：<https://learn.chatgpt.com/docs/customization/memories>、
<https://mem0.ai/library/coding-agents/how-memory-works-in-codex-cli>（实现层拆解）

### 2.1 两层结构

| 层 | 谁写 | 特点 |
|---|---|---|
| `AGENTS.md` | 人（进 git） | 稳定规则、项目约定；32 KiB 上限，超了**静默截断** |
| `~/.codex/memories/` | agent（生成态） | 摘要、持久条目、近期输入、支撑证据 |

官方对第二层的定性很值得抄：

> Treat these files as **generated state**. You can inspect them when
> troubleshooting ... but **don't rely on editing them by hand as your primary
> control surface.**

→ 对应我们的 M4：`memories/` 是生成态，**改记忆要用命令/工具，不是让用户去编辑 md**。

### 2.2 生成管线（两阶段）

| 阶段 | 做什么 |
|---|---|
| Phase 1 逐会话 | 认领 startup job → 按严格 schema 的抽取提示采样 → **落盘前脱敏密钥** |
| Phase 2 合并 | 拿**全局锁** → 准备 workspace → 跑 consolidation 子 agent → **写 diff** |

其他机制：

- **双模型**：一个决定"什么值得记"，一个负责"合并进已有记忆"（都可配置）。
- **有界 + 老化**：只考虑最近 256 个 rollout（有硬顶）；rollout 30 天未用淘汰，
  单条记忆 30 天未被召回清除。
- **配额感知**：剩余额度低于阈值就跳过后台整理，"不让后台任务跟你的前台请求抢"。
- **两个独立开关**：一个管"写不写"，一个管"读不读"——支持只读调试、只写引导等
  非对称配置。

**这四条我们全缺**（尤其"老化淘汰"和"配额感知"）。

### 2.3 召回不是向量检索

原文很直接：

> Recall is the part most readers expect to be "vector top-k retrieval over an
> embedding index." **It isn't.** At session start, Codex reads
> `memory_summary.md` whole, token-truncates it to fit the context budget, and
> then instructs the agent to `grep` over `MEMORY.md` when it needs more detail.

→ 我们 `HARNESS_MEMORY_RECALL=local` 的本地关键词打分 + 注入，**思路一致**。这块不用
改造。唯一可借鉴的是"给模型一个 `recall` 工具，让它自己 grep"——正是 M1 里的第三项。

### 2.4 Codex 自己的已知短板

- **不跨机器**：换台电脑记忆归零。
- **不团队共享**：per-user。
- **纯生成态、不宜手改**。
- **32 KiB 静默截断**。

→ 我们在 M5（跨渠道）做的是"同机跨渠道"，比"跨机器"保守且够用。

---

## 3. 新发现的四个缺口

按重要性排。

### N1：记忆没有秘密脱敏（安全 / 高风险）

**现状**：`_render_memory()` 对正文零处理，抽取提示词里也没有"不要记密钥"的约束。

**为什么危险**：记忆是**长期状态**，一条 `.env` 内容被记下来，之后每次会话都会
被召回并塞进请求。泄露面比会话本身大得多——会话可以删，记忆会一直跟着。

Codex 明确把这条做进管线（"redacts secrets from generated memory fields"，
Phase 1 就是"redacts secrets before anything hits disk"）。

**要做的**：

- `memory._redact(text)` 落盘前扫一遍：`sk-`/`ghp_`/`AKIA`/`Bearer`/
  `-----BEGIN ... PRIVATE KEY-----`/`password=`/`token=`/`api_key=` 等模式，
  以及**已有 `credentials.py` 的凭据值精确匹配**（把它当 denylist 更可靠）。
- 命中即替换为 `[redacted:kind]`，并**记一条审计**（用户要知道被拦了什么）。
- 抽取提示词加硬约束：不记密码、密钥、令牌、身份证、卡号。
- 出口再扫一次：召回注入前检查（防止绕过写入侧的历史数据）。

这条应当**并入 M1 一起做**，不能等。

### N2：记忆没有老化淘汰（功能 / 中风险）

**现状**：只有 `_MAX_MEMORY_ITEMS = 50` 的**数量上界**（超了报错），没有时间维度的
淘汰。用一年后：50 条里全是早期闲聊，最近有用的偏好挤不进去。

**Codex 的做法**：rollout 30 天未用淘汰、单条记忆 30 天未召回清除。

**要做的**：

- 记忆条目加 `last_recalled_at`（召回注入时更新）与 `created_at`。
- `consolidate_memories()` 里加淘汰：`last_recalled_at` 超过 N 天（默认 90）
  且 `type != 'preference'` 的优先清；偏好类不过期但要能在 `/memories` 里看到年龄。
- **不静默删**：淘汰前先移到 `memories/.archive/`，`/memories` 能看到并可恢复。
  \"不删除用户数据\" 是我们既有的约束，淘汰也必须遵守。

### N3：没有"这条会话要不要记忆"的逐会话开关（隐私 / 中风险）

**现状**：`HARNESS_LOAD_MEMORIES` / `HARNESS_MEMORY_RECALL` 都是**进程级**环境变量。
没法"这次聊天别记"或"这次别读我的记忆"。

**Codex 的做法**：`/memories` 逐会话控制，且**不改全局设置**；临时会话不产生也不
使用记忆。

**要做的**：

- 会话级元数据 `memory_use` / `memory_contribute`（默认都 true）。
- `/memories on|off|readonly|writeonly` 命令 + 飞书卡片。
- 关掉 `contribute` 的会话，`extract_memories` 直接跳过；关掉 `use` 的，不注入。
- **和 M5 联动**：跨渠道接续时，目标会话的记忆设置要跟着走，不能因为换渠道就
  悄悄开始记录。

### N4：记忆每回合多付一次模型调用（工程 / 低风险）

**已实测确认**：`extract_memories` 内部调 `llm_route.completion('memory', ...)`，
每回合一次。`src/memory.py` 里共 3 处 `completion` 调用（抽取 1 次、LLM 召回 1 次、
标题另算）。

**好消息**：housekeeping 跑在后台线程（`_housekeeping_threads`），有 deadline 与
`join(timeout=...)`，**不阻塞用户回合**——所以不是延迟问题。

**代价是额度**：一次抽取 = 一次模型调用，不管这回合有没有值得记的东西。Codex 用
"闲置 6 小时才整理"天然规避了这个（一天最多几次），我们是每回合都付。

**要做的**（轻量自限，不做 Codex 那套闲置等待）：

- 抽取前先做**本地预筛**：本回合用户消息里没有"记住/以后/别再/我喜欢/我总是"
  这类触发词，且没有纠正语气，就直接跳过 LLM，省一次调用。
- 距上次 `consolidate_memories` 不足 N 回合（默认 3）时，只抽取不合并
  （合并是最贵的一步：要读全部记忆）。
- 保留手动强制：`/memories sync` 立即抽取+合并。

**为什么不照抄 Codex 的 6 小时**：单人本地使用，等 6 小时模型进程可能早就退出了；
而且我们已经有后台线程，没必要引入"闲置判定"这套额外状态。

---

## 4. 修正后的优先级

| 阶段 | 内容 | 变化 |
|---|---|---|
| **M1 + N1** | `remember`/`forget`/`recall` + **秘密脱敏** | 脱敏并入，不能拆 |
| **N3** | 逐会话记忆开关 | 新增，升到第二批 |
| **M3** | `never_do` 硬拒清单 | 不变 |
| **N2** | 老化淘汰（带归档不删除） | 新增 |
| **M2** | 只读主动建议 | 不变（默认关） |
| **M5** | 跨渠道接续 | 不变，但需带记忆设置 |

**顺序调整的理由**：N1 是安全问题且改动小（`_render_memory` 加一层 + 提示词），必须
和 M1 同批；N3 是隐私开关，实现是查表，成本低；N2 涉及数据迁移（加字段），放后面。

**M1 的措辞修正**：不再说"补记忆能力"，改说"给记忆补上**显式入口 + 安全边界 +
生命周期**"。机制已经在跑，缺的是可控性。

---

## 5. 参考

- Codex memories 官方文档（两层结构、生成时机、逐会话控制、配置项）：
  <https://learn.chatgpt.com/docs/customization/memories>
- Codex memory 实现拆解（两阶段管线、双模型、老化、召回是 grep 不是向量）：
  <https://mem0.ai/library/coding-agents/how-memory-works-in-codex-cli>
- ChatGPT memory 用户侧控制（saved memories、Sources、Don't mention again）：
  <https://help.openai.com/en/articles/8590148-memory-in-chatgpt>
- Codex 2026-04 发布说明：<https://openai.com/index/codex-for-almost-everything/>
