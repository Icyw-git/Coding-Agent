# Personal Agent 与 RSI 现状评估（2026-11）

评估对象：记忆（M1/M4/N1–N6）、主动性（M2）、跨渠道与后台（M5）、桌面与屏幕、
审批与边界（M3/N5），以及自我改进（RSI）。

**结论先说**：**能力面基本齐了，但自修改面有一个真实的权限漏洞**——`AGENTS.md`、
`skills/`、`.cache/rules.yaml` 这三处"会影响 agent 自己判断"的文件，agent 可以直接写，
既不要审批也没有硬拒。RSI 的 Ring 0（测试/CI/lint）守住了，Ring 1（skills/AGENTS.md）
只写在文档里。

方法见最后一节：除了读代码和文档，这次专门核对了**每个模块有没有生产调用方**——
上一轮 `proactive.py` 就是"测试全绿、文档写着已完成、实际没人调用"。

---

## 一、评分表

| 维度 | 评分 | 依据（一句话） |
| --- | --- | --- |
| 记忆：写入与落盘 | **A−** | 唯一入口 `_render_entry`、脱敏、审计、原子写、同名降级 UPDATE |
| 记忆：召回 | **A−** | 本地关键词零往返；召回时刻记入老化；`offline 基准 + 异步评测` 双层 |
| 记忆：评测 | **B+** | 离线基准守退化、在线 dump 采样判相关性；κ 校准因余额未做 |
| 主动性 | **A−** | 三档（off/observe/auto）+ 只读白名单两条腿 + 观察台账 |
| 跨渠道/后台 | **B+** | 会话锁 + 回合槽 + 非阻塞抢锁，崩溃残留锁有实测守卫；缺"人工插队" |
| 桌面/屏幕 | **A−** | 控件树三预算、元素动作、等待、坐标契约、敏感窗口四入口齐 |
| 审批与边界 | **B** | 四档 + 第四档硬拒 + 污点 + 审查官逐项留痕；**Ring 1 未接线** |
| RSI 循环 | **B+** | 有界（5 轮/收敛 3 轮）、指标由代码算、台账 + 回滚；驱动仍靠人 |
| RSI 指标质量 | **B−** | 默认指标是 `ruff_errors`；"建议更准"这类目标没有可用指标 |
| 回归保护 | **A−** | 3403 个用例、verifier_guard 硬拒改测试、刻意不给偶发测试加重试 |

---

## 二、Personal Agent：五块

### 1. 记忆（`src/memory*.py`，约 2000 行，测试 5 个文件）

**做得好的**

- **落盘只有一条路**：`_render_entry()` 是唯一渲染入口，脱敏（`memory_redact`）在
  写入前跑，审计事件在写后落。这类"两处各写一份"的漂移在记忆上代价最大。
- **读宽容、写严格**：`normalize_type`（宽容）与 `canonical_type`（严格）分开，
  老文件能读、新写入不产生垃圾类型。
- **同名一律降级 UPDATE**，UPDATE 需要硬证据；`preference`/`correction` 不随时间淘汰。
- **召回用关键词不用 embedding**：省掉一次网络往返（实测 ~2s/轮），并记 `record_recall`
  ——老化看"最后一次有用"而不是"最后一次写入"。
- **评测分两层**：`tests/test_memory_benchmark.py` 离线守退化；`memory_eval` 采样
  真实回合做 LLM-as-judge，**只落盘不参与决策**（避免评测反噬系统）。

**缺口**

- **κ 没取到**（LLM 判官与人工标注的一致性），因为 API 余额不足。意味着"判官说相关"
  这个信号的可信度没有校准过——报告里的召回率只能当趋势看。
- `memory_tools` 没有生产调用方（只有测试），tools 实际走 `tool_memory`。这不是 bug，
  但同名模块容易误导。

### 2. 主动性（`src/proactive.py`，刚接线）

- 三档 `off / observe / auto`，默认 `off`；`observe` 跑静默回合、只写观察台账
  （`.cache/proactive-observations.jsonl`，上限 50 条）。
- **只读有两条腿**：`TurnRequest.tools` 裁掉写工具（模型看不到），
  `hooks.proactive_scope` 在白名单外硬拒（真拦截）。端到端测试钉住 `write` 被拒、
  `read` 放行。
- 缺口：**建议质量未在真实模型下验证**（余额），所以现在只能保证"接线对、边界对"。

### 3. 跨渠道与后台（会话锁 / 回合槽 / cron / 飞书）

- 会话写锁与回合槽都按"会话目录"定位（`sessions_dir_for`），`/resume` 打开别处会话时
  锁得住真正在写的文件。
- 定时回合**非阻塞抢锁**：agent 忙就跳过这一轮，不会让人等着。
- 新增 `tests/test_recovery.py`：持锁进程被强杀后，后来者必须能继续（用真子进程验证）。
- 缺口：人在等的时候没有优先级——如果 cron 正在跑一个长回合，人的下一条消息要排到它
  结束（我们无 worker 槽位，这块**按设计不做**，见 `ARCHITECTURE_REVIEW.md` 第六节）。

### 4. 桌面与屏幕

- 控件树：三预算（120 节点 / 深 12 / 2500ms）、单父 14 子项、结构指纹（只含
  编号|类型|名字）、元素动作 8 种、等待（present/absent × control/window/foreground）、
  坐标契约（`左,上 宽x高` + 截图 origin）。
- 敏感窗口**四个入口都拒绝**（inspect / wait / 物理输入 / 元素动作），最后一个是这轮补的。
- 缺口：大控件量窗口的遍历偶发慢 3–5 倍，根因未定（第 76 节），预热改动已按纪律撤回。

### 5. 审批与边界

- 四档 sandbox、`MUST_DO_SELF` 硬拒、污点出网、LLM 审查官**逐项留痕**
  （`checks=5/5` + 命中项，阴性也算证据）。
- 缺 `once/times/until/session` 之外的东西没有——这一块我不打算加（见评估表下方）。

---

## 三、RSI：五块

**做得好的**

1. **有界**：`max_rounds`（默认 5，上限 20）、`CONVERGENCE_ROUNDS`（3 轮无最好成绩就收敛）、
   `MIN_IMPROVEMENT`（0.15）、`MIN_ABSOLUTE_STEP`（1）。
2. **指标由代码算**：`measure_pytest` / `measure_ruff`（硬门槛）、`measure_tokens` /
   `measure_wall_time`（软，权重 0.25）。注释写明了理由："自己发明 99.8% recall 那条路
   就是从接受模型自报数字开始的"。测不出来就返回 `{}`，**不填默认值**。
3. **`observe` 状态**：硬门槛回归时先不丢，等耐心窗口——来自"92% 的优化在变好之前先变差"。
4. **台账 + 回滚**：每次改动存 pre-image，`/improvements rollback <id>` 可还原；
   决策（keep/observe/discard）由代码判，写进台账。
5. **Ring 0 有硬拒**：`verifier_guard` 拦 tests / CI / lint 配置的写入，且明确写清理由
   （"如果我能改测试，'测试全过'就不再是证据"）。
6. **机制说明是必填**：说不清"为什么这个改动有用"的一律 discard，且在**测量之前**校验。

**缺口（按严重程度）**

| # | 缺口 | 后果 |
| --- | --- | --- |
| 1 | **Ring 1 没有接线**：`skills/`、`AGENTS.md` 可直接写（见第四节实测） | agent 能改自己的指令/技能，不需要审批；RSI 只在判定阶段 discard，文件已经改了 |
| 2 | **`.cache/rules.yaml` 可直接写** | 能给自己放宽规则（`rule_allowed` 会自动放行），等于给自己发权限 |
| 3 | 默认指标是 `ruff_errors` | 只适合机械类目标；"建议更准""召回更好"没有指标可接 |
| 4 | 驱动靠人 | `/improvements begin → (手工改) → check`，不是自主循环（刻意，但要知道） |
| 5 | κ 未校准 | 在线评测的判官可信度未知 |

---

## 四、这轮核对出的新问题（B19）

**方法**：不看文档、不看测试，直接问"这几处谁能写"。

```
AGENTS.md              policy=allow  guard=-        ← Ring 1，文档说"需审批"
skills/demo.md         policy=allow  guard=-        ← Ring 1
.cache/rules.yaml      policy=allow  guard=-        ← 规则文件
.env                   policy=deny   guard=-        ← 已保护（历史修复）
tests/test_x.py        policy=allow  guard=BLOCK    ← Ring 0，有效
```

三个结论：

1. **Ring 0 真的守住了**（`verifier_guard` 在 `hooks` 里接线，实测 BLOCK）。
2. **Ring 1 只存在于文档和 `self_improve.evaluate()` 的判定里**：写入时不拦，事后才
   discard 那一轮——而文件已经改了。
3. **规则文件没有任何保护**，而它能让 agent 给自己放宽审批。这是这次评估里最该先补的
   一条：`verifier_guard` 自己的理由（"改判定面就等于改证据"）在这里同样成立。

**建议的修法**（下一步 P0，仍按先测试后代码）：

- `.cache/rules.yaml` 加入 `verifier_guard` 的硬拒名单（它和测试一样属于"判定面"）；
- `AGENTS.md`、`skills/**` 走审批（`ASK`），理由文案说清"这是 agent 自己的指令"；
- 三条都用"先写失败测试（期望 ASK / DENY）→ 再改策略"的顺序。

---

## 五、下一步优先级

| 优先级 | 项 | 理由 |
| --- | --- | --- |
| ~~P0~~ | ~~堵 B19（Ring 1 + 规则文件）~~ | ✅ **已完成**（第 84 节）：`.cache/rules.yaml` 硬拒、`AGENTS.md`/`skills/**` 走审批 |
| P1 | 给 RSI 加一个能表达"质量"的指标（例如 `memory_eval` 的召回率、`/suggest` 的采纳率） | 现在只能改机械目标，用户想改"建议质量"时没有抓手 |
| P1 | B4（OCR 中文拆字）——倾向**不改逻辑**，只在输出末尾加一句提示 | 引擎行为，加副本会制造第二份内容 |
| P2 | κ 校准（需充值） | 影响在线评测的可信度，不影响功能 |
| P2 | B16 人工验收（D1–D26） | 需要真机点一遍 |

---

## 六、这份评估的边界

- 读了 `memory*.py`（约 2000 行）、`self_improve*.py`（737 行）、`proactive.py`、
  `verifier_guard.py`、相关 8 篇文档；跑了 172 个相关用例（全绿）。
- **没有**逐行读 4 万行源码，**没有**在真实模型下跑过 RSI 一轮完整会话（需要模型余额），
  所以"RSI 好用不好用"只能从设计与代码判断，不能从使用判断。
- 评分是相对本项目目标（个人 agent + 有界自我改进）的，不是和商业产品比。
