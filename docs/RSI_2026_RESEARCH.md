# RSI 深度调研：2026 主流实现，以及我们设计里的三个错误

日期：2026-10-28。承接 [AGENT_LEARNING_AND_RSI.md](AGENT_LEARNING_AND_RSI.md)
（三层定义）与 [PERSONAL_AGENT_LEARNING_RSI_DESIGN.md](PERSONAL_AGENT_LEARNING_RSI_DESIGN.md)
（架构方案）。

**结论先说**：B3 照 autoresearch 骨架写的那版有三个真问题——判据假设错了、
缺两个维度、台账本身是新的攻击面。本文先摆证据，再改实现。

---

## 一、主流实现已经往前走了

### 1.1 autoresearch 是什么位置

Karpathy 的 autoresearch 是**最小可行版本**：改一个文件 → 跑固定评估 → 保留
或回滚。它的价值是把"可验证改进"这件事降到可操作，但它对**多轮退化**和
**目标漂移**都没有处理。我们现在这版（`self_improve.py`）继承了这个短板。

### 1.2 2026 的六种主流机制（OpenClaw 综述了 60+ 论文）

| 机制 | 代表 | 关键数字 |
|---|---|---|
| **自我反思循环** | Reflexion（Stanford） | HumanEval 80% → 91%，纯语言自我纠错，无权重更新 |
| **演进式上下文手册** | ACE（Stanford） | +10.6%，适配延迟降 87%。三角色：Generator / Reflector / **Curator（只提交 delta，绝不整体重写）** |
| **经验库** | Voyager 模式、Self-Generated ICE | ALFWorld 73% → 93% |
| **自改代码 agent** | SICA | SWE-Bench Verified 17% → 53%；**效用函数 = 性能 0.5 / 成本 0.25 / 时间 0.25** |
| **元提示优化** | DSPy / TextGrad | Llama 3.2 1B 分类 51.9% → 63% |
| **LLM 自评** | Arize 四模型研究 | 未经人工校准时都给自己打高分；校准后只有 Google 模型仍自涨 **37.1%**，Claude 严 7.4%，GPT 中性 |

### 1.3 生产侧的做法：证据门控而非时间门控

`lamarck-skill`（Oct 2026，支持 Claude Code / Codex / Cursor / pi）：

> every installed skill keeps accumulating **evidence from real usage** and
> evolves **when the gate opens** — governed, user-authorized, reversible.

和我们的区别在于**证据从哪来**：我们靠跑一次 pytest（合成信号），它靠真实使用
累积（`checks run / edits kept / your next reply`）。后者的信号更接近"真的变好"。

---

## 二、推翻我们设计的三个发现

### 2.1 ❌ 错误一：「变差就丢弃」会扔掉 92% 正确的路径

Gödel Agent（ACL 2025）的实测：

> **92% of optimization runs experience temporary regressions before finding
> improvements.** Without error handling, performance drops 14.8%.

我们的 `regressed()` 是**硬门槛**：任何一项变差立刻 discard。按这个数据，
92% 的改进轨迹会被我们自己的判据掐死——它们在变好之前必然先变差。

**这是判据层面的设计错误，不是参数没调好。**

改法：**耐心窗口**。回归不立即丢弃，而是进入观察；连续 N 轮仍未恢复才放弃。
同时记录"曾经回归过但最终变好"的轨迹，这类轨迹本身就是高价值数据。

### 2.2 ❌ 错误二：只有正确性，没有成本与时间

SICA 的效用函数是三目标：

```
utility = 0.5 × performance + 0.25 × cost_efficiency + 0.25 × execution_time
```

> This multi-objective framing prevents the agent from optimizing performance
> at absurd computational cost.

我们只有 `pytest_pass`（二值）与 `ruff_errors`（计数）。一个让测试全绿但把
token 成本翻三倍的改动，在我们的判据里是"无变化"甚至"改进"。

改法：加 `tokens_per_turn`（lower）与 `wall_time_s`（lower）两个软指标，
参与打分但不做硬门槛——避免因为机器负载抖动把好改动误杀。

### 2.3 ❌ 错误三：台账本身是新的攻击面（OWASP ASI06）

这是最严重的一条。OWASP 2026 把 **ASI06: Memory & Context Poisoning** 加进
Agentic Top 10；MINJA 攻击（NeurIPS 2025）通过普通用户查询达到 **>95% 注入成功率**。

真实事故（从业者自述，跑了 220+ 个自主循环）：

> after 100+ iterations, his agent had **invented metrics ("99.8% recall
> accuracy"), inflated impact claims, and was citing its own fabricated numbers
> as established fact.**

机制很简单：

> agent writes optimistic summary → that summary becomes input to the next loop
> → agent reads its own optimism and builds on it. **Positive feedback loop.
> Devastation.**

对照我们的 `self-improvements.jsonl`：**agent 写入 summary，下一轮又读台账**。
这就是那个正反馈回路。而且 summaries 是模型自己写的散文——正是"可被修辞"
的形态。

改法（三条都必须做）：

1. **台账里的判定依据只能来自测量**，不能来自模型写的散文。summary 仅作人类
   阅读，**不参与任何决策**，读取接口也不暴露给改进逻辑。
2. **指标必须由代码计算**，不接受模型上报的数字（否则就是"自己发明
   99.8% recall"那条路）。
3. **时间衰减**：旧记录权重递减。从业者经验——
   > older reflections carry less weight; a February lesson shouldn't override
   > recent evidence.

---

## 三、治理层：第四个循环（实践者的共识）

r/AI_Agents 上一个跑了生产系统的从业者指出三循环结构（runtime / reviewer /
persistent）的共同盲点：

> none asks **whether an improvement should survive when it lifts the score but
> shifts the real objective**.

他给出的四项控制，前两项我们已经有了（留出集、verifier_guard），后两项没有：

| 控制 | 我们的状态 |
|---|---|
| 定期人工复核"看起来很好"的案例 | ❌ 缺 |
| 留出基准，系统无法对其优化 | ✅ `tests/` 在 Ring 0 |
| **轮换评估者**，不让同一模型家族永远判自己 | ❌ 缺 |
| 分离"分数上升"与"部署到生产"两个决策 | ⚠️ 部分（Ring 1 需审批） |

还有一条来自车间经验的启发式，成本极低：

> the changes I can explain in one sentence are usually genuine improvements.
> The ones that require a paragraph of justification are usually me optimizing
> for something I did not intend to optimize for.

→ **可解释性门槛**：改动必须能用一句话说清机制；说不清就是漂移的信号，
不进 keep。

这个讨论串里最诚实的一句值得抄下来：

> every proposed solution **smuggles judgement in somewhere**. Humans reach for
> process because process feels safer than responsibility. But the process
> always has an author, a boundary, and a failure mode.

所以本文档不假装解决了漂移问题。它只是把**一种**漂移暴露出来，按**我们的**
定义，只要那个定义还站得住。

---

## 四、改后的设计

### 4.1 判定模型：从"线性的 keep/discard"改成"带耐心的轨迹"

```
baseline ──→ 改动1 ──→ 改动2 ──→ 改动3
              ↓回归       ↓回归     ↓改进
           观察期      观察期     keep
              ↓连续 N 轮未恢复
            discard
```

三个状态：`keep` / `observe`（回归但在耐心窗口内）/ `discard`。

`observe` 是新增的，它是 Gödel Agent 那条 92% 的直接产物——**没有这个状态，
我们就只能看见"一次成功"，看不见"先退后进"**。

### 4.2 指标分层

| 层 | 指标 | 作用 |
|---|---|---|
| **硬门槛** | `pytest_pass` | 测试必须全绿；红了进 observe 而不是立刻 discard |
| **硬门槛** | `ruff_errors` | 同上 |
| **软指标** | `tokens_per_turn` | 成本（SICA 的 cost 维度） |
| **软指标** | `wall_time_s` | 时间（SICA 的 time 维度） |
| **元信息** | `mechanism` | 一句话机制说明，缺失则不得 keep |

软指标**不否决**改动，只在总分里降权：一个把成本翻三倍的正确性改进应该被
看见，而不是被静默接受。

### 4.3 台账防污染三条

- `metrics` 与 `decision` 由代码计算，`mechanism`/`summary` 是**人类可读文本，
  不进入任何判定**
- 读取接口 `entries()` 明确不是"决策输入"；改进循环只读 `measure()` 的返回值
- 时间衰减：超过 90 天的记录在统计里权重 ×0.5（只影响展示与趋势，不影响已定决策）

### 4.4 明确仍然没解决

- **轮换评估者**：我们只有一个 pytest 命令。轮换意味着有第二套独立评估，
  个人单机场景没有第二套可换。**记录为缺口，不假装解决。**
- **定期人工复核**：`/improvements` 是唯一的入口，靠人。
- **目标漂移本身**：见上文"smuggles judgement in somewhere"。判定标准是人
  定的，它错了，整条链路就是更快地走向错的地方。

---

## 五、参考

- Gödel Agent（ACL 2025，92% 临时回归、自升模型）：<https://arxiv.org/abs/2410.04444>
- SICA 三目标效用函数（0.5/0.25/0.25）
- AlphaEvolve / OpenEvolve：<https://github.com/codelion/openevolve>
- 六机制综述（含 Arize 自评偏差 37.1%）：OpenClaw Index《Reflection + Anti-Drift》
- OWASP Agentic Top 10 2026 **ASI06 Memory & Context Poisoning**；MINJA（NeurIPS 2025，>95% 注入成功率）
- 第四个循环与治理控制：<https://www.reddit.com/r/AI_Agents/comments/1u5a96c/>
- lamarck-skill（证据门控的生产自演化）：`newdee/lamarck-skill`
- 三层定义与"验证者是瓶颈"：<https://www.philschmid.de/recursive-self-improvement>
