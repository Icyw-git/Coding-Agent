# Personal Agent 第三轮规划：记忆、主动性与委托边界

状态：规划（2026-10-27）。前两轮见 [PERSONAL_AGENT_ROADMAP.md](PERSONAL_AGENT_ROADMAP.md)
（P1–P6 能力边界）与 [PERSONAL_AGENT_PLAN.md](PERSONAL_AGENT_PLAN.md)（阶段 0–2 远程控制）。

第一轮的结论是「桌面/系统/权限层已补齐」。这一轮换个问法：**把三家 2026 年的
personal agent 摊开对照，我们缺的不是能力，是「长期关系」那部分。**

---

## 1. 我们现在的真实位置

| 维度 | 我们 | 说明 |
|---|---|---|
| 工具数 | 48 | 桌面、剪贴板、删除、屏幕、文档、浏览器(MCP)、cron、团队 |
| 权限分层 | 三档 × 三策略 + 目录/域名/规则账本 + 审计 | 比大多数开源方案细 |
| 常驻 | 登录自启 + 监督进程 + 本地通知 + 空闲感知 | 已闭环 |
| **记忆** | `src/memory.py` 有完整读写，**但模型没有写入工具** | 只能靠 `write` 直接写 md 文件 |
| **主动性** | 只有「每日只读心跳」+ cron | 不会自己提建议、不会接着上次干 |
| **委托边界** | 有审批与审计 | 缺「哪些永远不代做」的硬编码清单 |

一句话：**执行层已相当完整，关系层几乎是零。**

---

## 2. 三家在做什么（2026 年一手资料）

### 2.1 Codex（2026-04「Codex for (almost) everything」）

新增五件事，全部指向「长期关系」：

| 能力 | 具体做法 |
|---|---|
| **Background computer use** | 用它自己的光标看/点/打，**多个 agent 在同一台 Mac 上并行**，不干扰用户操作 |
| **内存（memory，preview）** | 记住「个人偏好、纠正、花了时间才收集到的信息」，让后续任务更快更好 |
| **Automations 复用线程** | 定时任务可以**复用已有对话线程**，保留之前积累的上下文；能给自己安排未来工作、**跨天跨周自己醒来继续** |
| **主动提议** | 用项目、插件和记忆的上下文，**主动提出今天该做什么、上次的项目从哪继续** |
| 90+ plugins | skills + app integrations + MCP 的打包单位 |

来源：<https://openai.com/index/codex-for-almost-everything/>

### 2.2 dots（OpenAI，2026-09）

| 设计 | 具体做法 |
|---|---|
| 自己的电脑 | 每个 dot 有云电脑 + 自己的浏览器；**你的电脑默认隔离，除非你选择连接** |
| 主动性 = 只读 | 「proactive research」只用**被限制为只读**的工具：不能发消息、改内容、控浏览器或电脑 |
| Custom Rules | allow / 需审批 / block 三态 |
| auto-review | 影响账户或外发信息的动作过 auto-review；**改密码这类永远留给人** |
| 观察 | 随时打开它的电脑看工作过程；Activity View 看进度（含后台） |
| 凭据 | 保存的密码用系统代填，**模型看不到** |
| 长期 | 跨 ChatGPT / Slack / Teams，上下文跟着走；「越用越懂你」 |

来源：<https://openai.com/index/introducing-dots/>

### 2.3 Muse（Meta，2026-09）

架构上最值得抄的三点：

| 设计 | 具体做法 |
|---|---|
| **每人一台 Secure VM** | agent 和数据都在里面，与其他用户隔离 |
| **Sentinel 监督 agent** | 独立进程、系统级隔离；**Muse 的任何出网请求都要 Sentinel 批准**，必要时问人 |
| **凭据代换** | 密码/支付方式存在安全存储里，Muse 只能用、不能看；浏览器里用户自己敲的密码它也看不见；支付用一次性卡号（限商户/金额/时间） |
| 审计 | 完整记录做过什么、打算做什么；**每次购买都要显式批准并展示交易详情** |
| 遗忘 | 可以「忘掉」学到的具体内容 |

来源：<https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/>

**反面对照**（TIME 2026-10 调查）：Muse 每小时更新用户和联系人的「档案」，推断未说出口的目标，
而且指令里明确要求「不要告诉用户原始消息可能仍可见」。<https://time.com/article/2026/10/06/meta-muse-ai-agent-privacy/>

这条对我们的意义：**记忆必须可查看、可删除、可解释「为什么记这个」，否则就是黑箱**。

---

## 3. 由此得出的差距清单

按「对个人日常价值 ÷ 实现风险」排序。

### M1：记忆没有一等公民入口（高价值 / 低风险）

**现状**：`memory.py` 有 `write_memory_file` / `read_memory_index` / `list_memory_files` /
`select_relevant_memories`，机制完整；但**模型没有工具**，只能靠 `write` 往
`memories/` 写 md，等于「知道目录结构才能用」。

**三家对比**：Codex 的 memory 是产品级（自动抽取 + 主动应用）；Muse 有显式
「忘记」。

**要做的**：

- `remember(text, type, description)` 工具：写一条记忆，返回文件名。
  `type` 收窄成枚举（`preference` / `fact` / `correction` / `context`），对齐
  Codex 说的「偏好、纠正、花时间才拿到的信息」。
- `forget(name)` 工具：删一条。对应 Muse 的「tell it to forget」。
- `recall(query)` 工具：按关键词查记忆全文（现在只有 `select_relevant_memories`
  的内部注入，模型无法主动查）。
- **写入要审批吗**：记忆是长期状态，写一次影响所有未来会话。建议 `remember`
  走审批但允许「本会话同类」；`forget` 必须逐次审批（删除比写入更需确认）。
- 系统提示加一句：什么时候该记（用户明确说「记住」、纠正你、反复出现的偏好），
  什么时候不该记（密码、一次性信息、别人的隐私）。

**验收**：说「记住我周三下午不看代码」→ 下次会话主动提及；说「忘掉刚才那条」→
列表里消失；`/memories` 能列出来。

### M2：主动性只有「摘要」，没有「提议」（高价值 / 中风险）

**现状**：`heartbeat.py` 明确写了「只读巡检（任务板、工作区最近改动、待审批数量）」，
**不跑模型**。cron 是按用户给的 prompt 跑。所以「agent 自己想到该做什么」这件事
完全不存在。

**三家对比**：Codex 会「主动提议今天从哪开始」；dots 有 proactive research。

**要做的**（严格按 dots 的「主动性只读」原则）：

- 新增 `proactive.py`：定时（比如每天开工前）用**只读工具**扫一遍
  （待办、最近会话、未完成的 task、cron 结果、工作区 git 状态），让模型生成
  1–3 条**建议**。
- **建议不是行动**：产出进 outbox → 飞书卡片，带「开始做这件事」按钮；
  点了才进正常回合（带正常权限）。
- **只读硬约束**：proactive 回合把工具面裁成只读白名单（`read`/`grep`/`glob`/`ls`/
  审计查询），不是靠提示词。可复用 `hooks` 里既有的 scheduled 只读机制，但
  **不要复用 cron 的写权限**——那是两件事。
- 频率上限 + 静默期：默认每天一次，用户连续忽略 3 次就自动降频（避免变骚扰）。

**风险**：建议质量差会变成噪音。第一版应该**默认关闭**，等有真实使用再开。

### M3：委托边界没有硬编码清单（中价值 / 低风险）

**现状**：`SCHEDULED_FORBIDDEN_TOOLS`、`PROTECTED_CODES` 这些是「按工具/策略」拦，
但没有一份「**这类事情永远不代做**」的业务清单。

**三家对比**：dots「改密码这类永远留给人」是写在产品里的，不是靠提示词。

**要做的**：

- `src/never_do.py` 一份**代码里硬编码**的清单（不受配置、不受档位影响）：
  改密码 / 支付与转账 / 2FA 与验证码 / 输入任何凭据到表单 / 同意法律条款 /
  修改监护人设置。
- 不是靠「识别工具」，而是**在 `enforce` 最前面做文本与上下文检查**：
  命令含 `passwd`/`chpasswd`；`desktop_action` 的 type 目标是密码框；
  `clipboard_read` 结果像验证码（6 位数字且上下文在登录页）——最后这条
  太玄，先不做，只做前两条。
- 命中时**不是**走审批，而是直接拒绝并说明「这类操作请你自己做」。这和普通
  ASK 的区别很重要：审批可以被模型「说服」，硬拒不行。

### M4：记忆与主动性的可观察面（必需，随 M1/M2）

- `/memories` 命令 + 飞书卡片：列出每条记忆的 type / 描述 / 什么时候记的 /
  来源会话，每条带「忘记」按钮。
- `/proactive` 查看最近建议历史与采纳率。
- 审计事件：`memory_written` / `memory_forgotten` / `proactive_suggested`。

**为什么必需**：TIME 那篇调查说明，不可观察的长期记忆会变成「黑箱档案」。
可查看、可删除是底线。

### M5：跨渠道上下文（中价值 / 中风险）

**现状**：飞书、TUI、API 各自绑定一个会话；切渠道要手动 `/use`。

**三家对比**：dots「上下文跟着你走，在 ChatGPT、Slack、Teams 都有」；Codex
automations 复用线程。

**要做的**：轻量版——「最近活跃会话」跨渠道共享：在 TUI 停下的会话，飞到手机上
发消息时默认接上（而不是新建）。已有 `/resume` 与 bindings，缺的是「默认行为」。

**风险**：多设备同时操作同一会话 → 已有 `session_locks` + `turn_coordinator`
兜底，但要补一条：另一渠道正在跑回合时，新渠道的消息应该**排队/提示**而不是
静默失败。

---

## 4. 明确不做

| 不做 | 原因 |
|---|---|
| 云电脑 / 独立 VM | 个人单机场景没有收益，运维成本极高 |
| Sentinel 式双 agent 监督 | 我们的审批 + 审计 + 硬拒清单覆盖同样威胁模型，不需要第二个 agent |
| 自动代填密码 | 与「密码不暴露给模型」冲突；Muse 那套需要凭据代换基础设施 |
| 支付 / 代下单 | 与 M3 硬拒清单冲突，且不可逆 |
| 每小时更新「用户档案」 | TIME 暴露的问题；我们只做用户显式要求记住的事 |
| 4,000 个 app 接入 | 用 MCP 按需接，不维护清单 |

---

## 5. 建议顺序与工作量

| 阶段 | 内容 | 状态 | 默认 |
|---|---|---|---|
| **M1 + N1** | `remember`/`forget`/`recall` + 落盘脱敏 | ✅ 已完成 | 开 |
| **M3** | 第四档 `MUST_DO_SELF` + 硬拒清单 | ✅ 已完成 | 开 |
| **N6** | 外部内容不可信围栏 | ✅ 已完成 | 开 |
| **N5** | 污点出网 | ✅ 已完成 | 开 |
| **N3** | 逐会话记忆开关 | ✅ 已完成 | 开 |
| **N2** | 记忆老化（带归档） | ✅ 已完成 | 开 |
| **M4** | 记忆审计事件 | ✅ 已完成 | 开 |
| **M5** | 跨渠道接续 | ✅ 已完成 | 开 |
| **M2** | 只读主动建议 | ✅ 已完成 | **关** |
| N7 | 审批作用域 once/task/expires | 未做（可选） | — |

实现细节见 [PERSONAL_AGENT_ROUND3_IMPL.md](PERSONAL_AGENT_ROUND3_IMPL.md) 与
[PERSONAL_AGENT_ROUND3_SECURITY.md](PERSONAL_AGENT_ROUND3_SECURITY.md)。

**先做 M1 + M3**：一个补「长期关系」，一个补「硬边界」，都不碰主动性，
风险最低，而且 M1 是 M2 的前置（没有记忆的主动建议只能是泛泛而谈）。

**M2 最后做且默认关闭**：主动性最容易做成品味灾难（每天推三条没用的建议）。
先有真实使用数据，再决定开不开。

---

## 6. 参考

- Codex 2026-04 更新（computer use / memory / automations / 主动提议）：
  <https://openai.com/index/codex-for-almost-everything/>
- dots 产品与权限模型：<https://openai.com/index/introducing-dots/>
- Muse 产品与安全架构：<https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/>
- Muse 深挖（凭据代换 / Sentinel / 一次性卡）：
  <https://coursiv.io/blog/meta-muse-personal-ai-agent>
- Muse 隐私调查（记忆不可观察的代价）：
  <https://time.com/article/2026/10/06/meta-muse-ai-agent-privacy/>
- 用户诉求（「不工程师化也要能用」的完整系统）：
  <https://community.openai.com/t/users-want-a-personal-assistant-without-having-to-engineer-it/1409119>
