# 性能与内存 profile，以及与 pi 的 runtime 对比

日期：2026-11-01。对象：本仓（`harness-agent`）vs `@earendil-works/pi-coding-agent` 1.4.0。

**结论先说**：我们的**每轮运行开销不差**（稳态 15–26ms、200 条重建 34ms、300 轮无泄漏），
但**固定开销很贵**：57 个工具的 schema 每次请求约 **7,394 tokens**，而 pi 默认只带 **4 个工具**。
启动也慢一个数量级（0.48s vs 0.08s）。安全模型是两条路：我们逐次审批，pi 明确不做逐次
审批、靠 OS/容器边界。

方法：所有数字都是本机实测（Windows 11 / 150% 缩放 / Python 3.10.20 / Node 24.13.1）。
测量脚本在 `scripts/profile_*.py`，命令见每节脚注。pi 那边读的是 `docs/`（1.4.0 自带）
与 bundle 里的实测常量，**没有跑完整会话**（需要模型 key），所以它的每轮开销无法对比。

---

## 一、启动开销

| 项 | 实测 |
| --- | --- |
| `import harness`（含全部工具/钩子/存储） | **0.43–0.55s**（CPU 累计 482ms） |
| `harness.py --help`（含解释器启动） | **653–759ms** |
| 空 Python 进程（基线） | 80ms |
| `pi --version`（Node 24） | **71–86ms** |

`importtime` 的分解（累计）：

```
harness        482ms
├ hooks        116ms ── agent_core 71ms
├ subagent      54ms ── compaction 54ms ── llm_route 51ms ── llm_config 49ms ── yaml 45ms
├ dotenv        45ms
└ tools         41ms
```

（`importtime` 的"累计"列会把子模块的时间算进父模块，单模块自带依赖时数字偏小；
下面用的是**单独导入**的实测值，更接近"去掉它能省多少"。）

单模块实测（`-X importtime`）：`yaml` **79.6ms**、`dotenv` **88.5ms**——两者合计约
**168ms**，占 482ms 的三分之一。它们只在"读配置"时需要，而现在都是模块级 `import`
（`llm_config.py:27`、`memory.py:22`、`harness.py:44`、`agent_teams.py:10`）。
这是启动慢最直接的一块。

> pi 快的原因不只是语言：它是 bundle（`dist/bundle/cli.js` 单入口），Node 的模块加载是
> 编译期就绪的字节码缓存；我们每次都要 import 一堆纯 Python 模块。

## 二、内存

| 场景 | 峰值 RSS |
| --- | --- |
| 空 Python 进程 | 10.2 MB |
| `import paths` 一类轻模块 | 12.0 MB |
| **`import harness` 全量** | **26.6–26.8 MB**（即 harness 本身约 16.5 MB） |
| 打开一个会话 + 50 条消息 | 26.9 MB |
| 注入 20 条记忆 | 27.0 MB |

会话内增量（`tracemalloc`，200 条消息 ≈ 45KB）：

```
rebuild 耗时 34.5ms | 峰值增量 0.53 MB | 驻留约 1873 B/条
```

`profile_memory.py` 的 300 轮模拟：

| 轮次 | 抖动 0.04 MB @300 轮 | 结论 |
| --- | --- | --- |
| 50 → 300 | RSS 33.5 → 33.9 MB | **无泄漏**（`bounded_cache` 统一了 4 处 `clear()`） |

**判断**：26.8 MB 的常驻对桌面 agent 完全可以接受（一个 Electron 窗口的零头）。
真正贵的是"每轮都要重新发一遍工具表"，见下一节。

## 三、每轮开销

`scripts/profile_turn_pipeline.py`（真实会话文件，本地召回模式）：

| 会话 | 消息数 | 稳态 | 首次 | rebuild p50 | 记忆注入 p50 | 峰值 |
| --- | --- | --- | --- | --- | --- | --- |
| 4.9 MB | 239 | **14.98ms** | 306ms | 6.7ms | 9.1ms | rebuild 11.26 MB |
| 1.0 MB | 783 | **26.39ms** | 45ms | 17.9ms | 9.6ms | rebuild 8.68 MB |

阶段分解（cold / p50，ms）= rebuild / memory / context / system / compact / assembly。
**稳态下没有单项超过 20ms**；首次的 306ms 主要是 `rebuild`（244ms）+ `memory`（300ms）冷启动。

`profile_memory.py` 的规模曲线：

| 消息数 | rebuild p50 |
| --- | --- |
| 10 | 8.0 ms |
| 50 | 9.4 ms |
| 200 | 65.2 ms |
| 500 | 61.8 ms |

200 → 500 没有线性增长（单遍化生效），这是 212→106ms 那次优化的结果。

## 四、最贵的一项：工具表

每个请求都要把全部工具的 JSON schema 发一遍。实测：

```
57 个工具 → 29,576 字符 ≈ 7,394 tokens（按 4 字符/token 粗估）
系统提示          3,040 字符 ≈   760 tokens
────────────────────────────────────────────
每次请求固定开销            ≈ 8,150 tokens（还没算对话历史）
```

最贵的 6 个：

| 工具 | 字符 | ≈tokens |
| --- | --- | --- |
| `desktop_action` | 3,811 | **952** |
| `schedule_cron` | 1,196 | 299 |
| `desktop_wait` | 1,164 | 291 |
| `grep` | 1,080 | 270 |
| `remember` | 950 | 237 |
| `render_document` | 911 | 227 |

**一个 `desktop_action` 等于 952 tokens**——它的参数确实多（两套模式：element 与键鼠），
但这是每次请求都付的固定成本。

## 五、与 pi 的 runtime 对比

### 5.1 工具面（差距最大的一处）

| | 我们 | pi |
| --- | --- | --- |
| 默认工具数 | **57** | **4**（`read` / `bash` / `edit` / `write`） |
| 固定 schema 成本 | ≈7,394 tokens | 粗估 500–1,000 tokens（最长工具描述 358 字符） |
| 扩展方式 | Python 模块 + `tool_registry` + hooks | TypeScript 扩展进程内注册；`--tools` 精确增删 |

pi 的 README 明说 *"ships with powerful defaults but skips features like sub-agents and
plan mode"*——**少即是设计**，复杂能力靠扩展装。

我们的 57 个里，桌面 6 + 屏幕 3 + 记忆 3 + 任务/团队/会话 10 左右，确实都有用。
但"每次请求都带"和"需要时才带"是两件事。pi 有 `--tools` 精确控制；我们的
`agent_loop(tools=...)` 现在也能裁（这轮为 `/suggest` 加的），但**默认仍是全量**。

### 5.2 安全模型（方向完全相反）

| | 我们 | pi |
| --- | --- | --- |
| 逐次审批 | **有**：四档 sandbox + ASK/DENY + 第四档硬拒 + 审查官 | **没有**。原文：*"it does not ask for approval before every tool call"* |
| 边界靠什么 | 进程内策略层 + 审批队列 | OS 权限 / 容器 / VM（`docs/containerization.md` 四种方案） |
| 明说的边界 | 第四档不可绕过 | *"Watching the transcript, using project trust, and reviewing changes **do not create a security boundary**"* |

这不是谁对谁错：**pi 把安全外包给环境，我们把安全做进调用链**。代价与收益：

- 我们：无需容器就能拦"删系统目录/读密钥/外发"，但每次都要人多点一下（或走审查官）；
- pi：不打扰人，但**误操作只能靠事后 review**；要真隔离就得进容器。

值得注意：pi 那句"看 transcript 不是安全边界"和我们的第四档是同一个洞察的不同落点。

### 5.3 会话与压缩

| | 我们 | pi |
| --- | --- | --- |
| 存储 | append-only JSONL，**fork 复制**新文件 | JSONL **树**（`id`/`parentId`），原地分支，v3 格式 |
| 分支 | fork 出独立会话 | `/tree` 在同一个文件里换分支，离开的分支可摘要后附加 |
| 压缩触发 | `estimate_size > CONTEXT_LIMIT`（`(窗口-8192)*0.8`） | `contextTokens > contextWindow - reserveTokens`（默认 16384） |
| 保留策略 | **最近 6 条** + 尾部预算 `min(50k, LIMIT/8)` | **最近 20k tokens** |
| 压缩产物 | 摘要 + 最近现场；原文另存 transcript | `CompactionEntry`（摘要 + `firstKeptEntryId`），原条目保留 |

两种都合理。**差异在"保留按条数还是按 token"**：我们按条数（6 条），遇到"6 条里塞了
三个大文件"就靠尾部预算兜底；pi 直接按 token，更贴近真实成本。

我们的 `CONTEXT_LIMIT` 对 `deepseek-flash` 算出来是 **832,307 tokens**——压缩触发得很晚，
好处是少打断，坏处是一次压缩要处理的跨度很大。

### 5.4 启动与分发

| | 我们 | pi |
| --- | --- | --- |
| 启动 | 0.48s / 26.8 MB | 0.08s |
| 分发 | 源码 + Python 依赖 | npm 包，`dist` 21MB / 336 个 JS |
| 依赖数量 | 运行期 `yaml`/`dotenv`/`openai`/`fastapi`… | 22 个直接依赖（含 5 个自家 `@earendil-works/*`） |

### 5.5 值得学 / 不必学

| pi 的做法 | 该不该跟 | 理由 |
| --- | --- | --- |
| 默认工具少、按需扩 | **部分跟** | 我们的能力是真需求，但可以按"场景"分组默认集（如无桌面环境不装 6 个 desktop 工具） |
| 会话树 + 原地分支 | **不跟** | 我们是"一人一机 + fork 出独立会话"，树模型要重写存储与回放，收益在"频繁回溯"场景 |
| 压缩按 token 保留 | **可以跟** | 改动小：`KEEP_RECENT` 从"条数"改成"token 预算"，`_tail_within_budget` 已经有这个骨架 |
| 不做逐次审批 | **不跟** | 我们的用户就是在桌面上用，没有容器时逐次审批是唯一实时边界 |
| bundle 单入口启动 | **可以跟** | 但收益要权衡：打包会牺牲"能直接改源码" |

## 六、如果要动手，优先这三件

1. **工具表按需装配**（省 ≈5,000 tokens/请求）
   把 57 个工具的 schema 做成"基础集 + 能力组"，例如桌面环境探测不到就不发 6 个 desktop
   工具；`HARNESS_TOOLS_PROFILE=minimal|full` 给用户选。落点：`tool_schemas.py` 分组 +
   `harness.TOOLS` 装配处。**先测**：`tool_schemas` 的字符数作为守卫断言。

2. **压缩保留按 token 而不是条数**（改动 < 30 行）
   `KEEP_RECENT=6` → `KEEP_RECENT_TOKENS=20000`；`_tail_within_budget` 已经把预算算出来了，
   只是外面还套了一层条数。落点：`compaction.py` + `harness.py` 的调用处。

3. **启动去 `yaml`/`dotenv` 的模块级导入**（省 ≈168ms，占启动 1/3）
   两者都只在读配置时用，改成函数内惰性导入。落点：`memory.py`/`rules.py`/`llm_config.py`
   的 `import yaml`，`harness.py` 的 `from dotenv import load_dotenv`。

> 三件都不改行为，只改"什么时候花这笔钱"。按计划纪律：每件先写一个能失败的度量断言
> （字符数 / 耗时 / 导入列表），再改。

## 七、测量边界

- 所有耗时是本机单次或三次取中，**没有做统计显著性检验**；机器负载会影响绝对值，
  但同一台机器上的相对关系（如"yaml+dotenv 占 19%"）是稳定的。
- pi 的每轮开销、内存占用**没有测**（需要在真实会话里跑），只能从文档与常量推断。
- 我们是 Python、pi 是 Node：语言差异会让启动/内存的绝对值不可直接比，所以本文的
  对比集中在**设计选择**（工具数、压缩策略、安全模型），不是"谁更快"。

---

### 复现命令

```bash
python scripts/profile_turn_pipeline.py     # 每轮开销（真实会话）
python scripts/profile_memory.py            # 重建 / 驻留 / 泄漏
python scripts/profile_storage.py           # 磁盘占用
python -X importtime -c "import sys; sys.path.insert(0,'src'); sys.path.insert(0,'.'); import harness"
node /d/npm/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js --version
```
