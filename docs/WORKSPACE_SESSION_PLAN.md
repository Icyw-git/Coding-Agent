# 会话-工作区模型对齐 Codex：重构计划

## 背景

Codex 的模型是三条：**创建时绑定目录**（conversation 与 cwd 一起落盘）、**目录即信任**
（首次在某目录使用确认一次，记忆挂目录）、**工作区是会话的身份属性**（中途不换目录，
换目录 = 开新会话；列表按项目组织）。

本项目当前：`/new` 继承当前目录；`/cd` 可在会话中途换目录；工作区只出现在会话摘要里。
本计划把「新建会话」改成 Codex 式的工作区选择流程，**核心约束是用户不输入长路径**：
路径从已有会话推导成候选列表，键盘选；只有「浏览其他目录」才进输入框。

列表已经按工作区分组（`/sessions` 双栏、`cli_input.filter_session_rows(workspace=)`），
这一块不需要动。

## 交互设计

### CLI `/new`

```
选择工作区   ↑↓ 选择 · Enter 确认 · Esc 取消 · 直接输入可筛选

  ● 当前目录        D:\harness-agent
    最近使用
  ○ harness-agent   D:\harness-agent          3 个会话 · 2 小时前
  ○ sandbox-demo    D:\tmp\sandbox-demo       1 个会话 · 昨天
  ○ 浏览电脑中的文件夹…   Enter 进入；在置顶「进入此文件夹」行上 Enter 即确认
```

- 默认选中「当前目录」，**Enter 即建**：与旧 `/new` 行为等价，最多多一次回车。
- 「最近使用」来自本机会话摘要的 `workspace` 去重（排除当前目录），按最近活跃排序；
  用户第一次用就有内容，不需要任何配置。
- 只有选中「浏览电脑中的文件夹…」才进入目录浏览器：列表首行固定为「进入此文件夹」，
  Enter 在它上面就是确认当前目录并开始会话；在子目录上 Enter 是进入浏览，进入后光标回到
  首行。Backspace 返回上级，Windows 下额外列出盘符；全程不需要输入路径，也不用记第二个键。
- `/new <会话ID>` 保持兼容（命名新会话，工作区取当前目录）。
- 非 TTY（管道、脚本）：不弹选择器，按旧行为在当前目录建，并打印一行说明。

### 飞书 `/new`

不弹卡片，回复编号列表，回复 `/new 2` 选择（与 `/sessions` 的序号操作一致）：

```
新建会话 · 选择工作区
1. D:\harness-agent（当前，3 个会话）
2. D:\tmp\sandbox-demo（1 个会话）
3. 输入其他路径：/new D:\path\to\dir
回复 /new <序号>，或 /new <路径>
```

服务端沿用 `/cd` 的校验：路径必须存在且位于 `HARNESS_WORKSPACE` 下。

### 信任确认（阶段 2）

选中未信任目录后一行确认（CLI `[Y/n]`，飞书回复 `y`）：

```
首次在 D:\tmp\sandbox-demo 使用。信任此目录？[Y/n]
```

语义只做「确认 + 记忆」，**不改变任何执行/审批档位**——现有的路径分区与审批规则照旧。
迁移：升级时把已有会话记录过的 workspace 视为已信任，避免历史用户被逐个弹窗。

## 数据与接口

| 项 | 设计 |
| --- | --- |
| `workspace_choices(sessions, cwd, limit=6, saved=None)` | 纯函数：去重、排序、标记当前目录，返回 `{id, path, name, sessions, last_active, current, saved, exists, allowed}` |
| `harness.start_new_session(workspace=None, session_id=None)` | 从 REPL 内联的 `/new` 分支抽出：建 store、`session_created`、`record_workspace`、`set_workspace`、清空 messages；返回 store |
| `src/trusted_dirs.py` | `is_trusted(path)` / `trust(path)` / `seed(paths)`；文件 `.cache/trusted_dirs.json`（path → trusted_at） |
| `WorkspacePicker`（`src/cli_input.py`） | 仿 `SessionPicker` 的 prompt_toolkit 应用；只在这里做 UI，选择结果与展示行由纯函数提供 |
| 飞书 | 复用 `GatewayClient.list_sessions()` 的 `workspace` 字段构造同一份列表；`create_session(workspace=...)` 已支持 |

不在新建时改写会话题目：自动标题生成在任务结束后接管，预填目录名会被覆盖，收益低。

## 分阶段实施

### 阶段 1：CLI 工作区选择（可独立提交）

1. 新增 `src/workspace_choices.py`（纯函数 + 展示行）。
2. `harness.start_new_session()` 抽取，REPL 的 `/new` 改为调用它。
3. `WorkspacePicker` + `choose_workspace()`（非 TTY 回退打印列表并返回 None → 当前目录）。
4. `cli_commands.py` 的 `/new` 帮助文案改为「选择工作区开始新会话」。
5. 测试：
   - `workspace_choices` 去重/排序/排除当前/截断；
   - `start_new_session` 在 tmp 目录建会话、`record_workspace` 正确、`WORKDIR` 切换；
   - 非 TTY 下 `/new` 仍建在当前目录；
   - `/new <id>` 兼容。

验收：`/new` → Enter → 会话建在当前目录；选最近目录 → 新会话摘要里的 workspace 是它，
后续 bash/read 工具落在该目录。

### 阶段 2：信任记忆

1. `trusted_dirs` 模块 + 迁移 seed。
2. 选择/创建前的确认（CLI 单行，飞书文字）。
3. 测试：未信任问一次、信任后不问、迁移后历史目录不问、拒绝则回选择器。

验收：同一目录第二次 `/new` 不再确认；拒绝后不创建会话、回到选择器。

### 阶段 3：飞书对齐

1. `/new` 无参数 → 编号列表；`/new N` → 建会话并回复「已在新工作区 X 开始会话」。
2. `/new <路径>` 仍可用（后端校验同上）。
3. 测试：列表内容、越界路径拒绝、创建后绑定关系正确。

### 阶段 4：文档与收尾

1. README 的 `/new`、`/sessions`、`/cd` 三处。
2. `/cd` 标注「兼容保留；换工作区建议 `/new`，Codex 语义下工作区是会话身份」。
3. `docs/DEBUG_LOG.md` 记录取舍：为什么保留 `/cd`、为什么信任不改判定。

## 不做的

- 移除 `/cd`：存量会话与飞书用户都在用，破坏面大；只做语义降级与文档引导。
- 信任改变档位（未信任目录默认 read-only）：会悄悄改变安全语义，等有明确需求再做。
- 新建时预填会话标题为目录名：与自动标题冲突。
- `--temp`：Codex 也没有；临时任务用 `/new` 选临时目录即可。

## 风险与对策

- **prompt_toolkit 应用难做单测**：UI 层只负责渲染与按键，选择逻辑全部放纯函数；
  非 TTY 路径覆盖行为断言。
- **最近目录可能很长/重复**：按 `Path.resolve()` 去重，展示 `name` + 完整路径 + 会话数，
  上限 6 条，超出折叠为「更多…」。
- **选择器改变 `/new` 手感**：默认项是「当前目录」且 Enter 直达，脚本路径不受影响。
- **跨盘/不存在的目录**：与 `/cd` 同一套校验与错误文案。

## 前置条件

上一轮 10 个文件的改动（审查官/策略/飞书通知）尚未提交。重构开始前先提交或另起分支，
避免与本次改动的 diff 混在一起。

## 已确认的取舍

1. `/new` 默认弹工作区选择器，默认选中当前目录：Enter 直达，换目录只按键不输入。
2. 信任只做「确认 + 记忆」，不改任何执行/审批判定；迁移时把历史会话目录视为已信任。
3. 最近目录最多 6 条，纯自动生成；收藏/固定目录留到确有需求时再加。

## 进度

- 阶段 1（已完成）：`workspace_choices.py`（候选 + 文件夹浏览）、
  `harness.start_new_session()`、`_workspace_for_new_session()`、`WorkspacePicker`、
  `DirectoryBrowser`（Enter 进入 / 置顶「进入此文件夹」确认，不需要输入路径）、
  `/new` 分发与帮助文案；新增 `tests/test_workspace_choices.py`（10 例）。
- 阶段 2（信任记忆，暂缓）：确认后不改变任何判定与档位，等于每次多一次无效果点击；
  等信任能真正映射到「该目录的默认档位」时再做，避免为对齐而加噪音。
- 阶段 3（已完成）：飞书 `/new` 先列工作区候选，`/new <序号>` 选择，`/new <路径>`
  直接指定；序号只在最近一次列表内有效。
- 阶段 4（文档）：CLI/飞书 README 已同步；`/cd` 的降级说明待补。

## 实施顺序

先阶段 1（选择器 + 绑定），验收后再做信任与飞书；每阶段独立提交，附带新增用例，
并把 `ruff check src tests` 与全量 pytest 作为合并门槛。

---

## 现状审计与修订（2026-10-20）

### 已经落地

- CLI `/new`：当前目录 + 最近会话目录 + 目录浏览器；`start_new_session()` 负责
  绑定工作区，创建失败会回滚 `WORKDIR`。
- 飞书 `/new`：文本列表、`/new <序号>`、`/new <路径>`；API/SDK/网关建会话都支持
  `workspace`，会话事件里记录 `workspace_changed`。
- 授权：`HARNESS_WORKSPACE` 单根，同时约束 `AgentRuntime` 与 `TaskManager`。

### 与 Codex 的差距

| 能力 | Codex | 当前实现 | 结论 |
| --- | --- | --- | --- |
| 创建时绑定目录 | 是 | 是 | 已对齐 |
| 工作区可复用、可命名 | 项目/工作区是一等对象 | 只能从会话反推，无法显式登记 | 缺登记表 |
| 非交互指定工作区 | `-C` / exec 指定 cwd | 只有 TTY 选择器；`/new <id>` 不能带目录 | 缺脚本入口 |
| 一个进程内多个授权根 | Desktop 项目可附加多个文件夹 | 单根 `HARNESS_WORKSPACE` | 缺多根 |
| 飞书选择体验 | — | 首聊无会话时不能选；候选缓存无 TTL；纯数字会话 ID 与序号冲突 | 需修 |
| 会话中途换目录 | 不支持（换目录 = 新会话） | `/cd` 支持 | 保留为兼容 |

### 关键修订：登记不是授权

登记表放在 `.cache`/会话目录旁，而该目录对 Agent 可写；若登记项直接成为
`allowed_root`，Agent 只要写入 `workspaces.json` 就能自我授权任意路径。
因此：

- **登记表只负责发现、排序、命名，不参与任何授权判断**。
- 授权仍由启动配置决定：`HARNESS_WORKSPACE`（主根）+
  `HARNESS_WORKSPACE_ROOTS`（额外根，`os.pathsep` 分隔）。
- 未授权的登记项可以显示，但标注「需加入 HARNESS_WORKSPACE_ROOTS」并在创建前拒绝。
- 不做「首次信任」弹窗：确认后不改变任何判定，等于为对齐而加噪音（维持阶段 2 暂缓）。

### 数据契约

- `workspaces.json`（`HARNESS_WORKSPACE_FILE` 可覆盖，默认
  `SESSIONS_DIR.parent / "workspaces.json"`）：
  `{version: 1, workspaces: [{id, name, path, added_at, last_used_at}]}`；
  `id` 取规范化绝对路径的短哈希；写入用临时文件 + `os.replace`。
- `workspace_choices()` 行在现有字段上增加 `id`（仅登记项有值）、`saved`、`exists`、
  `allowed`；合并顺序：当前目录 → 登记表/最近会话目录（按 `last_used_at` 与
  `updated` 的较大值），按规范化路径去重，默认上限 6。
- `resolve_workspace()` 接受多个根；`AgentRuntime.configure_workspace_roots()` 与
  `TaskManager.allowed_roots` 一并扩展，`/run` 的 cwd 校验复用同一组根。

### 入口

- CLI：`/workspaces` 列表；`/workspace add <路径> [--name 名称]`、
  `/workspace remove <序号|名称|路径>`；`/new --ws <序号|名称|路径>`（非 TTY 也可用），
  `/new <会话ID>` 保持兼容。
- 飞书：`/new` 发选择卡片（文本列表兜底）；`/new <序号>`、`/new ws:<序号|名称|路径>`、
  `/new id:<会话ID>`；候选缓存 10 分钟过期，`/cd`、`/resume` 后失效。
- API：`GET/POST /v1/workspaces`、`DELETE /v1/workspaces/{id}`；
  `GET /v1/workspaces?current=` 返回选择器列表（`workspaces`，默认上限 6）与完整登记项
  （`saved`，不受上限影响）；`POST /v1/sessions {workspace}` 不变；远程登记只接受
  已授权根内的路径。

### 实施阶段

- **A 修现状**：候选行补 `exists`/`allowed` 状态；修飞书首聊无会话、空 workspace
  崩溃（`Path(None)`）、缓存过期与失效、数字 ID 歧义、相对路径识别；补测试。
- **B 登记表 + CLI**：`src/workspaces.py` 纯函数与原子存储；`/workspaces`、
  `/workspace add/remove`；选择器合并登记项；`/new --ws`。
- **C 多根授权**：`HARNESS_WORKSPACE_ROOTS`；`resolve_workspace` 多根；
  `AgentRuntime` / `TaskManager` / `harness_api` 贯通；API 与 SDK 增加 workspaces。
- **D 飞书**：选择卡片 + 命令语法 + 缓存失效 + 帮助文案。
- **E 收尾**：README、`/cd` 降级说明、`docs/DEBUG_LOG.md` 记录「登记不授权」取舍；
  `ruff check src tests` + 全量 pytest。

验收线：`/new --ws` 在脚本里可建会话；登记目录重启后仍在列表中；未授权目录
被明确拒绝且不能通过改 `workspaces.json` 绕过；飞书首聊即可选目录建会话。

---

## 落地结果（2026-10-21）

### 阶段状态

- **A 修现状（完成）**：候选行带 `saved` / `exists` / `allowed`；飞书首聊无会话时
  也能列出默认工作区（`AgentRuntime.list_workspaces(current=None)` 回落到默认根）；
  选择快照改成 `(token, rows, created_at)`，10 分钟 TTL，`/cd`、`/resume`、`/fork`、
  `/quit`、创建成功后失效；文本序号过期后明确提示重新 `/new`，不再把数字当会话 ID；
  `/new ws:` / `id:` / 相对路径分别处理。
- **B 登记表 + CLI（完成）**：`src/workspaces.py`（规范化去重、损坏文件容错、临时文件 +
  `Path.replace()` 原子写）；`/workspaces`、`/workspace add/remove`、`/new --ws`；
  CLI 的相对 `add` 基于当前 `WORKDIR`。
- **C 多根授权（完成）**：`HARNESS_WORKSPACE_ROOTS`；`workspace_context.workspace_roots()`
  统一读取；`resolve_workspace(allowed_roots=...)`、`AgentRuntime.configure_workspace_roots()`、
  `TaskManager.allowed_roots` 贯通；`GET/POST /v1/workspaces`、`DELETE /v1/workspaces/{id}`；
  SDK `client.workspaces.*`、`GatewayClient.*workspace*` 同步。
- **D 飞书（完成）**：`workspace_card()` 按钮回传服务端快照序号；文本列表兜底；
  `/workspaces`、`/workspace add/remove`；卡片的 `exists` / `allowed` 在真正建会话前
  再校验一次。
- **E 收尾（完成）**：README 的命令、环境变量、API/SDK 段落已更新；本文件补齐验收与
  偏差；`docs/DEBUG_LOG.md` 记录「登记不是授权」的取舍。

### 验收线核对

| 验收线 | 结果 | 证据 |
| --- | --- | --- |
| `/new --ws` 非交互可建会话 | 通过 | `test_repl_new_ws_binds_registered_workspace` |
| 登记目录重启后仍在列表 | 通过 | `test_add_list_and_remove_roundtrip`（文件往返） |
| 改 `workspaces.json` 不能越权 | 通过 | `test_registered_workspace_does_not_widen_authorization`、`test_workspace_card_refuses_an_unauthorized_entry` |
| 飞书首聊即可选目录建会话 | 通过 | `test_new_lists_the_default_workspace_before_any_session`、`test_workspace_card_button_creates_a_session_in_that_directory` |

### 实施中的修正

- 候选行需要携带登记项 `id`，否则飞书 `/workspace remove <名称>` 无法走
  `DELETE /v1/workspaces/{id}`（最初只回传了 path）。
- 飞书 `/new <数字>` 在快照过期后不能静默落成「会话 ID 为数字」；现在提示刷新列表。
- CLI `/workspace add 相对路径` 以 `WORKDIR` 为基准；飞书 `add` 以当前绑定会话的
  工作区为基准，避免服务端默认根与用户当前目录不一致。
- 测试侧用 `HARNESS_WORKSPACE_FILE` 把登记表钉在每个用例的 `tmp_path`，否则默认
  路径会落到共享测试父目录，跨用例串数据。
- 多根嵌套时任务沙箱改取最具体的授权根：原先取第一个命中根，`[C:\, C:\proj]`
  会把 `C:\proj` 内任务的沙箱放宽到整盘，违背「多根只收紧」的预期；
  新增 `test_nested_roots_sandbox_uses_most_specific_root` 钉住。

### 验证

- `D:\Anaconda_envs\envs\aitest01_py310\python.exe -m pytest -q`：**811 passed**。
  中间一轮曾出现一次 `test_mcp_adapters` 的 stdio 时序失败，单独运行、整文件运行
  和最终全量复跑都通过；该用例不涉及工作区改动。
- `ruff check`（本次改动文件及 `src`/`tests`）：通过。
- 未运行 `ruff format`：仓库现存文件是单引号风格，formatter 会把 21 个文件整体改成
  双引号，产生与本次改动无关的大面积 diff；遵循「只格式化本次改动的行」的既有约束。

### 仍然不做

- 首次信任记忆（确认后不改变任何判定，等于加噪音）。
- 一个会话绑定多个文件夹（Codex 的附加目录不在本切片内）。
- 登记表跨机器同步或云端存储。
