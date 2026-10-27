# 分层与配置：现状诊断与建议

日期：2026-10-27。回答两个问题：**代码分层做好了吗**、**参数是否需要统一的 config/yaml**。

先给结论，再给证据。

---

## 结论

**分层**：设计是对的，但**没有用目录表达**。`src/` 下 151 个 `.py` 平铺（只有 `ui/`
一个包），靠文件名前缀（`tool_`/`api_`/`feishu_`/`session_`/`turn_`）暗示归属。
依赖深度是自然形成的、基本健康，但存在 7 对循环依赖和 26 个文件通过
`llm_route.harness()` 反向读入口全局。

**配置**：**不建议**现在引入统一 config/yaml。理由不是"工程洁癖"，而是有三条硬约束
会让它变成倒退：安全档位必须只认进程启动环境、运行时换档必须即时生效、参数在调用
时读取（不是导入期）。有一件事**值得做**：建 `settings.py` 作为唯一读取口。

下面展开。

---

## 一、分层：现状

### 1.1 目录结构

```
src/                     151 个 .py，平铺
├── ui/                  13 + 5 个子包（chrome/composer/modals/renderers/transcript）
├── .mailbox/            运行时（空）
└── .worktrees/          运行时（空）
```

按前缀分组（说明命名承担了分层职责）：

| 前缀 | 数量 | 层 |
|---|---|---|
| `tool_` | 20 | 工具实现 |
| `feishu_` | 11 | 传输网关（飞书） |
| `api_` | 11 | 控制面 API |
| `session_` | 7 | 会话存储 |
| `turn_` | 6 | 回合生命周期 |
| `document_` | 6 | 文档解析 |
| `memory` | 4 | 记忆 |
| `approval` | 4 | 审批 |

### 1.2 依赖深度（自动分析 151 个文件的 import 图）

被依赖最多 = 最底层，依赖最多 = 最顶层：

| 底层（被依赖） | 次数 | | 顶层（依赖多） | 出边 |
|---|---|---|---|---|
| `paths` | 16 | | `feishu_gateway` | 32 |
| `exec_policy` | 16 | | `status_report` | 22 |
| `turn_progress` | 11 | | `harness_api` | 19 |
| `hooks` | 11 | | `tools` | 18 |
| `session_store` | 9 | | `hooks` | 15 |
| `llm_route` | 9 | | `agent_teams` | 12 |

**这个分布是健康的**：底层是路径/策略/存储这类纯原语，顶层是网关/UI/入口。说明分层
在**语义上**是对的，缺的只是**物理表达**。

### 1.3 问题一：7 对循环依赖

```
agent_teams         <-> autonomous_agent
agent_teams         <-> team_protocols
approval_judge      <-> hooks
clipboard_windows   <-> ps_worker
compaction          <-> session_store
desktop_control_windows <-> ps_worker
hooks               <-> tools
```

其中 **`hooks <-> tools` 是真实痛点**：这轮加记忆工具时踩到了——`tools` 导入
`tool_tasks` → `import hooks` → `from tools import tool_registry`，而 `tools` 还没
执行到 `tool_registry = {...}`，于是 `ImportError: cannot import name 'tool_registry'
from partially initialized module`。当时的解法是改成惰性导入 + 注释说明。

这种"靠 import 顺序碰巧能跑"的结构，**每次新增工具都可能再踩一次**。

### 1.4 问题二：规范与实际不一致

`docs/coding-standards.md` 第 19 行明文规定：

> CLI 只适配输入输出，runtime 与工具不反向依赖入口和网关。**`src/` 模块不 import
> harness**：工作区、工具表、技能目录与 model client 由入口采集后注入

实际：**26 个 `src/` 文件 import harness**，主要经 `llm_route.harness()` 这个"合法
后门"：

```python
def harness():
    """延迟取 harness：模块导入期避免循环，调用期读到最新全局状态。"""
    import harness
    return harness
```

`llm_route` 自己的 docstring 也承认这是为了测试（`monkeypatch.setattr(harness,
'client', fake)`）。**这是个合理的妥协**，但它让"规范说不行、实际到处在用"成为常态，
新代码会照抄后门而不是遵守规范。

---

## 二、配置：为什么不建议统一到 yaml

### 2.1 现状

- **79 个** `HARNESS_*` 环境变量，138 处 `os.getenv` 调用点
- 已有 `docs/ENVIRONMENT.md` 作为权威清单（这轮刚建）
- `llm_config` 已经是 yaml 配置的成功先例（provider/model/role/fallback）

### 2.2 三条硬约束（这是关键）

**约束 A：安全档位必须只认进程启动时的环境**

`harness.py` 开头：

```python
# 执行/审批档位只认「启动进程时的真实环境」：项目根的 .env 就在工作区里，
# write/edit 在默认档免审，Agent 改一行就能把下次启动的权限降到 never。
POLICY_ENV_KEYS = ('HARNESS_SANDBOX_MODE', 'HARNESS_APPROVAL_POLICY', 'HARNESS_APPROVAL_ALL')
_TRUSTED_POLICY_ENV = {key: os.environ[key] for key in POLICY_ENV_KEYS if key in os.environ}
```

**如果档位写进工作区里的 yaml，Agent 用 `write` 就能给自己降权限** —— 这是真实的
提权路径，不是理论风险。统一配置必须把这三项排除在外，否则是安全倒退。

**约束 B：运行时换档必须即时生效**

`/sandbox read-only` 靠 `set_tier()` 直接写 `os.environ`，`load_policy()` 即读即用：

```python
def load_policy(environ=None) -> Policy:
    """每次调用即时读取，所以 /sandbox 与审批档切换无需重启即可生效。"""
```

任何"启动时加载一次配置对象"的设计都会破坏这个语义（`/sandbox` 改了但工具调用
还读旧值）。要保留就得在每个读取点判断"配置有没有变"，复杂度比现在的 getenv 更高。

**约束 C：参数在调用时读取，不是导入期**

实测：138 处 `os.getenv` 里，**只有 2 处**是模块级常量（导入期求值）：

- `api_streams.STREAM_PROGRESS_INTERVAL`
- `feishu_gateway.BINDINGS_PATH`

其余全在函数体内。这意味着"调用时读最新环境"已经是既有语义，改成配置对象时
缓存策略会很别扭：不缓存则每次查 dict（没省什么），缓存则破坏约束 B。

### 2.3 统一配置会付出什么

| 收益 | 代价 |
|---|---|
| 参数集中、可注释 | 与约束 A 冲突（需排除 3 项并解释为什么） |
| 类型校验 | 与约束 B 冲突（需在每个读取点判失效） |
| 少写样板 | 79 个变量 × 138 个调用点都要改，回归风险高 |
| | 测试大量依赖 `monkeypatch.setenv`，改配置对象会大面积失效 |

**结论：现在做统一配置是负收益。** 真正的痛点不是"参数散"，而是"没有一个可问
'这个参数是什么、默认多少'的地方"——那个已经有了（`docs/ENVIRONMENT.md`）。

---

## 三、建议做的事

按性价比排序。都是增量，不推翻现有结构。

### 建议 1（高性价比）：建 `src/settings.py` 作为唯一读取口

不改变"读环境变量"这个机制，只是**收敛读取点**：

```python
# src/settings.py
def flag(name: str, default: bool = False) -> bool:
    """布尔开关：接受 1/true/yes/on。"""
def number(name: str, default: float, *, minimum=None, maximum=None) -> float:
    """数值：越界收敛而不是报错（参数问题不该让已批准的操作失败）。"""
def text(name: str, default: str = '') -> str:
```

收益：

- 79 个变量的**默认值只写一次**（现在分散在 138 个调用点，`docs/ENVIRONMENT.md`
  是靠人肉核对的，会漂移）
- 新增参数时自动有统一的越界/非法值处理（现在每个模块自己 `try/except`）
- **为将来真的要做配置文件留了接口**：`settings.py` 内部可以先读环境、以后加
  yaml 来源，调用点不用改

风险：低。纯新增模块，逐步替换而不是一次性重写。

### 建议 2（中性价比）：把 `hooks <-> tools` 的环解开

只需要把 `hooks` 从 `tools` 拿的两样东西移走：

- `PatchError` / `patch_paths` → 已经有独立模块的迹象，移到 `patch_utils.py`
- `tool_registry` → `hooks` 只在 `unknown tool` 检查里用，改成从注入的集合读

这是**唯一**一个已经在实际开发中造成过故障的环，值得优先解。

其余 6 对（`agent_teams` 系列、`approval_judge`、`ps_worker` 系列、`compaction`）
影响面小，可以缓做。

### 建议 3（中性价比）：`src/` 分类到子包

如果要做，按已有前缀分组，**纯移动 + re-export**，不改一行逻辑：

```
src/
├── core/         paths, file_lock, console, perf, process_utils
├── policy/       exec_policy, policy_bash, hooks, approval*, rules, grants, net*
├── storage/      session_store, session_*, checkpoints, trash, memory*
├── tools/        tool_*.py (20)
├── transport/    feishu_*, api_*, harness_api
└── ui/           已有
```

**但要做好心理准备**：151 个文件的 import 路径全变，`tests/` 里大量
`import xxx` 和 `monkeypatch.setattr('src.xxx')` 会失效。按之前"正则批量改测试文件
一次改坏 6 处"的教训，这个动作需要**机械、可验证**的做法（比如 `git mv` + 全量
测试 + `--doctest-modules` 导入检查），不要手改。

**我的判断：现在不值得。** 分层在语义上已经清楚（前缀命名 + 依赖深度健康），
目录结构的收益主要是观感，而代价是几小时的机械工作和一次高风险回归。

### 建议 4（低成本）：把规范改成与实现一致

`coding-standards.md` 说"`src/` 不 import harness"，实际 26 个文件在用
`llm_route.harness()`。二者必须统一：

- 要么承认后门、写清什么情况下允许（测试注入、调用期读全局）
- 要么真的去掉（工作量大，且会破坏大量测试的 monkeypatch 方式）

**推荐前者**：把规范改成"允许经 `llm_route.harness()` 延迟取，禁止顶层
`import harness`"，并在这个函数上加约束说明。规范与实际一致，比规范更严格但没人
遵守要有价值。

---

## 四、如果一定要做统一配置文件

给将来留一条可执行的路，前提是把三条约束处理干净：

1. **分层来源**：`settings.py` 内部按优先级读
   `进程环境 > .env > config.yaml > 内置默认`。但 `POLICY_ENV_KEYS` 三项
   **跳过 .env 与 yaml**（只认进程环境），并在 `/status` 里显式说明。
2. **热更新**：`config.yaml` 的 mtime 缓存（`rules.py` / `net_grants.py` 已有
   这个模式），改完下次读取即生效，不需要重启。
3. **不引入类型系统**：用 `settings.flag/number/text` 三个函数，不要 pydantic
   模型——79 个参数建模的收益不抵维护成本，而且会让"加一个开关"变重。
4. **迁移策略**：新参数一律走 `settings.py`；旧参数在动到时顺手改，不专门做一次
   大迁移。

只有满足 1 和 2，配置文件才是安全的。否则它会把现在的"每次调用即读环境"这个
简单可靠的语义，换成一个需要处处判失效的复杂机制。

---

## 五、一句话总结

**分层**：语义对、物理没表达；2 个环值得解（`hooks<->tools`），其余可以不碰。

**配置**：**不要**现在做 yaml 统一——三条硬约束（安全档位只认启动环境、换档即时
生效、调用期读取）会让它变成安全倒退或复杂度上升。**要**做的是 `settings.py`
收敛读取点，把默认值写一次。

先做这个，等参数真的要给人配、且需要跨机器同步时，再上配置文件。
