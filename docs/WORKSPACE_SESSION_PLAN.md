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
| `workspace_choices(sessions, cwd, limit=6)` | 纯函数：去重、排序、标记当前目录，返回 `{path, label, sessions, last_active, current}` |
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
