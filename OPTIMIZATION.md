# 未来优化建议（OPTIMIZATION）

> 基于当前工作区结构（D:\harness-agent）整理。目的是把「功能已齐、工程未整」的项目推向
> 「可维护、可测试、可发布、可扩展」的工程化状态。
> 建议按 **P0（地基）→ P1（质量）→ P2（锦上添花）** 的优先级推进。

---

## 0. 当前结构速览

```
D:\harness-agent
├── harness.py / agent_loop.py / agent_core.py / autonomous_agent.py / subagent.py   # 核心引擎
├── tools.py / skill_system.py / background_task.py / hooks.py / recovery.py          # 能力与钩子
├── task_system.py / cron_scheduler.py / agent_teams.py / team_protocols.py           # 任务/定时/协作
├── worktree.py / MCPclient.py / mcp_bridge.py / mock_mcp_server.py                   # 沙箱/外部接入
├── memory.py / config.py                                                             # 记忆/配置
├── cc_ui.py / src/ui/harness_app.py / ui_events.py / renderer.py                  # UI 层
├── test_*.py（6 个，平铺在根目录）                                                   # 测试
├── _orig_harness.py（重构前的单体遗留）                                               # 遗留
├── DEBUG_LOG.md（开发实录/面试向文档）                                                # 文档
└── .cache/（crons.json / memories/ / tasks/ / transcripts/ ...）                      # 运行时状态
```

现状评价：
- **功能覆盖广**：主循环、工具注册、Hook、子代理、Skill、上下文压缩、记忆、任务、后台任务、
  cron、teams、worktree、MCP 全部具备，且已有拆分意识（`harness` 从 1577 行拆到 640 行）。
- **工程化缺口**：无包结构、无依赖声明、无 CI、测试平铺且非标准 pytest、无 README、
  根目录混入遗留文件、UI 多套实现并存。

---

## P0 — 地基：先做这些，收益最大

### 1. 目录结构重组（打包成正式 Python 包）

把平铺的根目录收敛为包结构，避免 `from tools import ...` 的顶层导入耦合：

```
harness_agent/
├── pyproject.toml            # 依赖声明 + 构建配置（setuptools 或 hatchling）
├── README.md                 # 安装/使用/架构一图流
├── src/harness_agent/
│   ├── core/       (agent_loop, agent_core, harness, recovery)
│   ├── tools/      (tools.py → 按领域拆分)
│   ├── subsystems/ (task_system, cron_scheduler, memory, worktree, mcp, teams)
│   ├── ui/         (harness_app, cc_ui, ui_events, renderer)
│   └── config.py
├── tests/                    # 与 src 对应的测试目录
└── scripts/                  # 入口脚本（原 __main__/main）
```

收益：解决循环导入（见 #2）、可 pip install、可单测导入、可发布。
迁移手法：保持每个文件内部逻辑不变，只动导入路径 + 加 `__init__.py`，分批进行，每批跑一遍现有 test_*.py 验证。

### 2. 消除 `tools.py` 对 `harness` 的运行时循环导入（`_h()` 反模式）

`tools.py` 通过 `def _h(): import harness; return harness` 在运行时偷渡依赖，说明
**可变全局状态（WORKDIR 等）放在 harness 里 + 工具层需要反向读它**，存在真正的循环依赖。

建议：
- 抽出独立的 `state.py` / `context.py` 持有可变状态（WORKDIR、当前分支、会话上下文），
  由 harness 初始化、工具层直接导入——单向依赖，删掉 `_h()`。
- 或者更进一步：用「依赖注入」，工具 handler 通过参数拿到 context，而不是闭包全局。

### 3. 统一 UI 层，明确「渲染器 vs 事件总线」边界

当前 UI 相关有 `cc_ui.py`、`src/ui/harness_app.py`、`renderer.py`、`ui_events.py`。
（历史分析写于统一为单一 Textual 实现之前；旧 `ui_textual.py` 已移除。）

建议：
- 确立分层：`ui_events.py`（事件总线，纯数据）→ `renderer.py`（把事件渲染成文本/ANSI）→
  `ui_textual.py` / `cc_ui.py`（具体前端适配器，二选一收敛）。
- 把 `cc_ui.py` 中 1143 行里属于「业务逻辑」的部分（如权限判定、命令处理）下沉到 core，
  让 UI 只做展示。
- 已决定 Textual 作为默认前端，ANSI 作为降级；旧 `ui_textual.py` 已删除。

### 4. 清理遗留与临时文件

- `_orig_harness.py`（1577 行重构前单体）：确认无引用后归档到 `archive/` 或删除，避免误导。
- 根目录测试文件迁移进 `tests/`（见 P1 #7）。
- 补充 `.gitignore` 覆盖 `.cache/` 的运行时产物（transcripts、tool_results 等），
  记忆/任务按需保留，transcript 全量入库会污染仓库。

---

## P1 — 质量：让项目可信

### 5. 依赖与配置规范化

- 用 `pyproject.toml` 声明依赖（openai/anthropic、textual、mcp 等），加版本范围，
  锁定环境可复现；本地 `pip install -e .` 即可跑。
- `config.py` 支持环境变量 + `.env`（12-factor 风格），密钥（API key）不入库、
  不写进日志；当前配置若硬编码默认值，需区分「默认值 vs 敏感值」。

### 6. 可靠性与并发（重点看 cron_scheduler / agent_lock）

- `cron_scheduler.py` 手写了 cron 表达式解析（`_cron_field_matches`），只支持部分语法
  （`*`、`*/n`、`,`、`-`），不支持名称（MON/FRI）、`?`、`L/W/#` 等。建议：
  - 要么换用成熟库 `croniter`（成熟、经过验证）；
  - 要么保留手写解析但补：字段合法校验、非法表达式提前报错、单元测试覆盖边界。
- 全局 `agent_lock` + `cron_lock` 是「一把大锁」：并发 teammates 抢同一把锁会互相阻塞。
  建议按资源粒度拆锁（每任务/每 cron 独立锁），并给所有 `.cache/*.json` 读写加锁与原子写
  （先写临时文件再 rename），防止并发写坏 crons.json / tasks。
- `.cache/transcripts/` 已有 100+ 个 jsonl 且无保留策略：加**轮转/清理**（按大小或天数），
  或迁移到 sqlite，避免无限膨胀。

### 7. 测试体系标准化

- 现有 `test_*.py` 平铺在根目录，且部分看起来是「脚本式断言」而非 pytest 用例
  （如 test_memory.py 里是 LLM 提炼的文档说明式描述，需核对）。
- 建议：
  - 迁到 `tests/`，统一用 pytest + fixture + tmp_path（避免测试污染 `.cache/`）。
  - 对 LLM 调用做 mock / 注入假 client（工具层已有 `_h()` 便于 patch 的意图，正好顺势注入）。
  - 补 CI（GitHub Actions 或本地 pre-commit）：pytest + ruff + mypy。
  - 优先补 cron 解析、任务状态机、worktree 增删、权限门禁的纯逻辑单测（不依赖网络）。

### 8. 文档补齐

- 缺 README：把 DEBUG_LOG.md 的「核心架构一图流」提炼成 README（安装、快速开始、命令一览）。
- 各子系统（teams 协议状态机、worktree 生命周期、MCP 桥）值得一份简短设计文档，
  或至少在模块 docstring 里写清状态流转。

---

## P2 — 增强：从「能用」到「好用」

### 9. 可观测性

- 目前日志/调试以 print 为主（可查 DEBUG_LOG）。建议引入 `logging` + 结构化日志（JSON lines），
  关键事件打点：API 调用耗时、token 消耗、工具执行时长、hook 拦截次数。
- 有 metrics 后可以加一个 `stats` 工具让 agent 自查资源占用；长期可接 OpenTelemetry。

### 10. 性能

- 上下文压缩已有四级策略，可补：token 计数用真实 tokenizer（tiktoken）替代估算；
  记忆召回按相关度排序而非全量注入（给 MEMORY.md 加索引/embedding 检索）。
- 工具调用频繁 `import harness`（`_h()`）有重复 import 开销；状态下沉到 state.py 后顺带消除。

### 11. 安全加固

- 已有 PermissionGate 黑名单/询问机制，可扩展：
  - bash 工具的路径越界检查（防止 agent 读写 `.cache` 之外的敏感路径）；
  - Windows 下命令执行的沙箱/超时/资源限制（subprocess 加 timeout 与 shell=False 白名单）；
  - 工具 schema 里的敏感参数（API key）脱敏。
- MCP 外部工具动态注册：增加来源白名单与 schema 校验，防止恶意 server 注入危险工具。

### 12. MCP 与外部集成健壮性

- `MCPclient.py`（63 行）较薄：补连接超时、重试/退避、断线重连、心跳；
- 考虑直接换用官方 `mcp` SDK（stdio/SSE 双传输），减少自维护成本；
- `mcp_servers.json` 持久化已有，可加「server 健康检查 + 一键重启」。

### 13. teams / worktree 协同体验

- 工作区有两个可清理的 worktree（delta-prime、epsilon-notes），可加一个 `worktree prune` 定时巡检；
- teams 协议状态机（shutdown/plan_approval）可补超时与死信处理（teammate 无响应时自动回收）；
- teammate 邮箱（BUS）目前为内存结构，跨会话/重启丢失——如需长驻可持久化。

### 14. 长期演进方向（可选）

- 把 harness 拆成「runner（执行）+ SDK（库）两层」：runner 供 CLI 使用，SDK 供二次开发。
- 支持多模型后端抽象（OpenAI/Anthropic/本地 Ollama）统一接口，方便评测对比。
- 评估/自省循环：记录每次任务的工具-结果-成本，做成可回放的 evaluation 集（详见 #15）。

### 15. Agent 功能测评（评测体系）

> 现有 test_*.py 覆盖的是「模块级」逻辑（cron 解析、任务状态机、worktree 增删、权限门禁），
> 缺的是「agent 级」端到端能力测评：一件多步任务能不能**做对、做快、做得省**，要有数字说话。

- **Golden Task 集**：沉淀 20~50 个有明确验收标准的端到端任务（如「建任务 → spawn teammate →
  worktree 提交 → 合并」、工具编排、MCP 调用、权限拦截场景），固化到 `benchmarks/` 或 `tests/e2e/`，
  作为功能基线，而不是靠人工抽查。
- **可量化指标**：任务成功率、工具调用正确率/失败率、完成耗时、token 与成本、
  上下文压缩后的信息保留率、hook 拦截命中率——每一项都要能跑出数字，避免「感觉上没问题」。
- **回放与 diff**：`.cache/transcripts/`（100+ jsonl）已是天然的轨迹语料，记录每次任务的
  「工具→结果→成本」。做成可回放的 evaluation 集后，支持「改一个模块 → 全量回放 → 行为 diff」，
  定位回归点。
- **模型横向对比**：配合 #14 的多模型后端抽象，同一 Golden 集跑 OpenAI / Anthropic / 本地 Ollama，
  输出对比表，为选型与降本提供依据。
- **防回归机制**：利用项目已有的 cron 能力做 nightly 评测，或提供 `scripts/evaluate.py` 一键可跑；
  新功能合入前先过 Golden 集，通过才允许合并。

---

## 建议落地顺序（参考路线图）

| 阶段 | 内容 | 预计收益 |
|------|------|----------|
| 第 1 周 | P0#1+#2+#3：包结构 + 状态下沉 + UI 收敛 | 消除循环导入，结构清晰，可安装 |
| 第 2 周 | P1#5+#6+#7：依赖声明 + 锁/持久化加固 + pytest 迁移 + CI | 可复现、可靠、可回归 |
| 第 3 周 | P1#8 + P0#4：README/文档 + 清理遗留 | 可上手、仓库干净 |
| 之后 | P2 按需：观测性 → 安全 → MCP 健壮性 → 性能 → 功能测评（#15） | 好用、可信、可扩展、可度量 |

> 原则：**每一步都不改变现有功能行为**，先保证旧 test 全绿，再谈新特性；
> 每完成一步，把 DEBUG_LOG.md 的第 11 部分（面试速查）同步更新，保持「问题→方案→坑→收获」主线。
