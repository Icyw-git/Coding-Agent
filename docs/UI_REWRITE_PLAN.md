# TUI 重构计划（对标 Pi / Codex）

状态：已合并到 main（bd99673）；P7 legacy 清理和默认 Textual 入口已完成；用户已确认真实终端验收通过，环境/版本与逐项结果记录仍待补齐（见 UI_TERMINAL_ACCEPTANCE.md）
进展：
- P1 拆包：TUI 实现迁到 `src/ui/app.py`，根 `ui_textual.py` 变兼容壳，UI 测试 75 条全绿。
- P0 虚拟化：`src/ui/transcript/{model,layout}.py` + 基准脚本；10k blocks 布局 27ms、
  滚动 1.4ms/帧、流式增量 3.2ms。
- P0 浮层：Textual 8.2 `position:absolute + overlay:screen + offset` 验证通过，
  补全浮层可按 editor 坐标定位且不抢焦点。
- P2 起步：`VirtualTranscript(ScrollView)` 用 `virtual_size + render_line`
  只画可见行；滚动不重算布局，视口外不创建 widget。
- P2 骨架：`src/ui/harness_app.py` 打通事件→块模型→视口（历史/流式/工具/
  提交），`HARNESS_UI=textual` 已指向新实现，旧实现保留为 `textual-legacy`。
- P3：补全浮层（overlay，Tab/Alt+↑↓/Esc）、待发送队列条（Alt+↑ 取回）、
  editor 边框内嵌 Working 状态、footer 基础指标（分支/ctx/tokens/cost）。
- P4：工具块 renderer（调用/结果/diff 着色/预览/展开）；harness 为
  write/edit/apply_patch 生成 unified diff（跳过秘密/二进制、20k 上限）。
- P5：审批弹窗与会话选择器抽到 `src/ui/modals/`（旧/新 TUI 共用）；
  新 TUI 支持 `/` 命令路由、`/sessions` 选择恢复、`permission_request` 弹窗。
- P5b：命令注册表 + 移植 Pi 的 fuzzy 匹配；补全/帮助/未知命令建议共用一份
  元数据，`/hotkeys` 与快捷键表同源；`!` shell、Esc 取消、Ctrl+O 展开。
- P5c：对齐 Pi `getArgumentCompletions`：`/resume` 会话、`/model` 模型、
  `/allow|deny|ask` 工具、`/help` 命令名；`/help <cmd>` 显示用法/可用性。
- P5d：动态技能命令 `/skill:<name> [args]`（展开技能正文为 user 消息，同 Pi
  `_expandSkillCommand`）；技能自动进入补全/帮助/模糊建议。
- P5e：把常用行式命令接进新 TUI：`/diff` `/cache` `/sandbox` `/review`
  （后台+进度块）`/model`（选择器）`/expand` `/approvals`；参数补全覆盖
  sandbox/cache。
- P5f：对照 Pi 会话命令命名后迁移 `/new` `/rename`（别名 `/name`）`/fork`
  `/checkpoints` `/compact`；`/status` 增加 `/session` 别名。Pi 用消息树
  （`/tree`）而非 checkpoint/rewind，语义差异记录在 DEBUG_LOG，暂不强行改名。
- P5g：Pi 风格 `/fork` 消息选择器 `ForkPickerModal`：列出 user 消息、默认最新、
  Enter 从所选消息**之前**分叉并把原文回填输入框；`/clone` 改为独立命令
  （复制当前叶），保留 `/fork <seq>` 作为 inclusive 兼容入口。
- P5h：交互自检修复：Ctrl-C 退出、补全候选 ↑↓/鼠标点击、会话选择器搜索框
  ↑↓/Enter、模态焦点延后到 screen 激活后。
- P5i：响应体验：提交后立即刷出用户消息；屏蔽 agent_loop 对 Textual 屏幕的
  print；回合结束用 agent_loop 返回值兜底最后一答（先 drain 事件防重复）；
  工具块点击在 10/40 行预览间切换（show more/show less），Ctrl+O 仍展开全文。
- P5j：修复 worker 未把 `session_store` 传给 `agent_loop` 导致事件 session_id
  对不上（UI 不显示本轮结果，重开才从会话文件刷出）；footer 增加上下文
  `ctx N% 50k/200k` 与 `cache N%`，并处理 `round_end` 的 usage。
- P5k：footer 常驻 cwd/branch（隐藏“新会话”占位标题，每秒刷新）；token/成本
  对齐 Pi：`↑input ↓output RcacheRead WcacheWrite`、最近一次请求的 `cache%`，
  cost 按 fresh input/output/cacheRead/cacheWrite 分桶，reasoning 不再重复计费。
- P5l：流式也用 Markdown 渲染，不再先显示 `**md 源码**`；Rich Markdown 增加
  dark 主题（代码块不再默认 cyan-on-black）；工具输出剥离 ANSI；流式重排限频
  120ms，最终回答强制刷新。
- P5m：审批弹窗改为半透明遮罩+居中盒子（不再全屏盖住对话）；Markdown 渲染加
  try/fallback，畸形链接不炸帧；支持选中文本 Ctrl-C/Ctrl+Shift+C 复制和 `/copy`
  复制最近工具输出。Profile：10k 块布局约 30ms、滚动约 1ms；Markdown 10k 文本约
  63ms，瓶颈在 markdown_it parse + rich render，已用 120ms 限频。
- P5n：队列语义对齐 Pi：Enter 运行中=steer，Alt+Enter=follow-up；Alt+↑/F2 或
  `/dequeue` 取回全部；Esc 取消时把队列内容退回编辑器。
  （P6d 已按用户反馈调换：Enter=下回合队列，Alt+Enter=当前回合 steering；
  Ctrl+G 改为选择器，支持单条取回与删除。）
- P5o：会话选择器按最近更新/创建时间/名称循环排序（Alt+S），并可在全部/当前
  工作区间切换（Alt+W）；搜索覆盖标题、工作区、ID 与摘要，预览随高亮行更新。
  复用已有 session summary，不改存储格式；80×24 下弹窗约束在视口内。
- P5p：会话选择器支持活动/已归档视图（Alt+V）；活动会话可重命名（Alt+R）或归档
  （Alt+A），归档会话 Enter 恢复、Alt+D 永久删除。归档列表后台加载；操作重新扫描并
  校验候选后复用共享确认/清理流程，当前会话不可归档，永久删除仍要求输入“删除”。
  临时存储端到端测试覆盖取消/确认及归档、恢复/删除流程；全量 **1461 passed / 1 skipped**，
  Ruff、`py_compile` 与 diff 检查通过。
- P5q：`Ctrl+F` 打开当前对话搜索，异步扫描完整会话历史和当前 transcript；`Enter` /
  `Shift+Enter` 循环定位命中并高亮，命中较早历史时按页加载。历史块使用稳定索引 ID；
  修复布局将前缀插入误判为尾部追加的问题。Textual 回归覆盖旧页定位、导航、高亮、无结果
  与关闭清理；全量 **1462 passed / 1 skipped**，Ruff、`py_compile` 与 diff 检查通过。
- P5r：`Ctrl+Shift+P` 打开命令面板，复用注册表和 fuzzy 匹配，按命令/技能分组。无参数
  命令交给现有分发；有参数提示的命令只回填 Composer，不提前执行。Textual 验证覆盖 fuzzy
  搜索、原分发、参数回填、无结果、取消及 80×24 布局；全量 **1463 passed / 1 skipped**，
  Ruff、`py_compile` 与 diff 检查通过。真实终端验收仍待执行。
- P5s：模型选择器支持 fuzzy 搜索，优先高亮最近状态中生效的模型；无运行时状态时按配置解析
  context window。切换先解析配置，再设置 `HARNESS_MODEL_MAIN`（主角色运行时覆盖）并同步
  legacy `LLM_MODEL_ID`；刷新上下文窗口及占用。包含旧环境变量和多 provider 配置回归。
  配置未提供价格元数据，不显示猜测价格；模型选择不是 per-session 持久化设置。
- P6：`HARNESS_UI=textual` 路由到 `src/ui/harness_app.py`；旧实现保留为
  `textual-legacy`，`textual-next` 作为兼容别名。
- P6a：迁移 `/checkpoint` `/rewind` `/undo`：回滚前显示 `plan_restore` 预览，
  `ConfirmModal` 确认后才落盘；`/undo` = `turn --yes`（回退到用户回合开始）。
- P6b：迁移剩余命令 `/archive` `/delete` `/find` `/workspaces` `/workspace`
  `/cd` `/watch` `/revoke` `/more`，并补 `/new --ws` 与参数补全。交互按 Pi
  `ctx.ui.select` 的思路抽成通用 modal：`ChoiceModal`（搜索+单选）、
  `CleanupPickerModal`（多选，Ctrl+A 全选）、`TypedConfirmModal`（删除需
  输入“删除”）。归档/删除的扫描与执行在后台线程，扫描诊断通过 `emit` 回调
  传递，避免修改进程级 stdout；`/find` 命中后弹选择器，Enter 直接恢复；`/watch` 通过
  `emit` 回调把进度写进状态块，不再直接 print。清理计划/执行、工作区增删等
  共享语义下沉到 `harness.py`，行式 REPL 与 TUI 使用同一份实现。
  注册表 40 条命令全部 `ported=True`；补齐 `/checkpoint-gc`、行式命令别名和
  `/resume last`，由测试对比两套命令注册表，避免仅凭 `ported` 标记判断迁移完成。
- P6c 收尾检查：任务期间禁止切换会话/工作区；shell、压缩和清理串行执行，
  此时提交的消息排入 follow-up，操作成功或失败后继续处理。已切换会话的后台
  搜索/清理结果不再弹出旧选择器。确认报告可滚动；目录候选只回填，避免提前切换。
  新增真实文件回滚、冲突拒绝、强制恢复与 rewind 事件持久化回归测试。
- P6e：`/checkpoint-gc` 默认清理当前会话并提示其他会话待清理量；`--all` 先预览、
  确认后按每会话保留上限清理所有会话，`--keep N` 覆盖默认 20；行式 REPL 与 TUI 共用
  `checkpoints.gc_plan/gc_all_checkpoints`，dry-run 不落盘。
- P6d 队列交互：按用户反馈把 Enter 与 Alt+Enter 对调——Enter 忙时进入下回合队列，
  Alt+Enter 立即进入当前回合 steering（空闲时两者都直接发送）。Ctrl+G/Alt+↑/`/dequeue`
  改为队列选择器：↑↓ 选择、Enter 取回单条、Ctrl+R 取回全部、Delete/Ctrl+D 删除选中项
  （二次确认）、Esc 关闭；单条时 Ctrl+G 直接取回。取消息时校验预期文本，Agent 已消费
  的旧行不会误取到下一条；Esc 取消仍取回全部。
- P7：`src/ui/app.py`、`ui_textual.py` 和 `textual-legacy` 路由已删除；
  默认交互入口改为 Textual，`ansi` 保留为显式降级；旧专属测试已删除或迁到新 App。
- P7a：会话管理动作（工作区切换、跨会话搜索、归档/恢复/删除）下沉到
  `src/ui/session_admin_actions.py` 的 `SessionAdminActionsMixin`，与核心层
  `session_admin.py` 同口径；斜杠命令路由与模型切换下沉到
  `src/ui/command_actions.py` 的 `CommandActionsMixin`。`harness_app.py` 从
  1943 行降到 1408 行，App 只保留视图、事件分发与回合控制。
- P7b：Rich 渲染辅助（Markdown 兜底、流式围栏改写、按宽度复用的 Console）
  下沉到 `src/ui/rendering.py`；队列/输入框/补全浮层/可选文本视口下沉到
  `src/ui/widgets.py`。`harness_app.py` 从 1408 行降到 1129 行，只保留
  App 布局、事件归约、回合控制与状态栏；`harness_app` 继续 re-export
  `_render_markdown` 等名字，测试 patch 点跟随实现改到 `ui.rendering`。
- P7c：事件归约（UIEvent → 块模型、`round_end`/`status` 状态累积、历史分页）
  下沉到 `src/ui/event_reduction.py` 的 `EventReductionMixin`，历史窗口常量
  与 `HARNESS_HISTORY_*` 环境变量读取一并迁入。`harness_app.py` 从 1129 行
  降到 925 行，App 只保留布局、视图交互、回合控制与工具/后台任务。
- P7d：队列动作（取回/全部取回、Ctrl+G 选择器、单条删除确认、队列条刷新）
  下沉到 `src/ui/queue_actions.py` 的 `QueueActionsMixin`；队列对象仍由 App
  拥有，worker 取头部的语义不变。`harness_app.py` 从 925 行降到 797 行。
- P7e：补全事件/候选接线下沉到 `src/ui/composer/suggestions.py` 的
  `SuggestionsMixin`；footer 与 Working/历史分页状态分别下沉到
  `src/ui/chrome/footer.py`、`status.py`；`/review` 与 `/watch` 后台动作下沉到
  `src/ui/background_actions.py`。状态仍由 App 拥有，mixin 通过 `self` 复用现有
  队列、widget、model 与事件生命周期，不改交互语义。`harness_app.py` 从 797 行
  降到 558 行。全量 **1455 passed / 1 skipped**，相关 UI 定向 **97 passed**，
  Ruff 与 diff 检查通过；真实终端验收仍按 [UI_TERMINAL_ACCEPTANCE.md](UI_TERMINAL_ACCEPTANCE.md)
  待执行。
- P7f：`!command` 的提交、后台执行与结果回写并入
  `src/ui/background_actions.py`，与 `/review`、`/watch` 共用本地后台动作边界；
  `HarnessApp` 仍负责输入路由和运行时组装，busy 拒绝、审批上下文、工具块状态与
  队列调度保持不变。`harness_app.py` 从 558 行降到 514 行；新增失败清理与忙时拒绝
  回归。真实终端验收仍待执行。
- P7g：`FooterMixin` 缓存最后发送的 footer 文本和 widget 身份；周期刷新值不变时跳过
  `Static.update()`，数据变化或 footer widget 替换时正常刷新。新增测试验证重复调用不
  更新、ctx 状态变化仍更新。全量 **1457 passed / 1 skipped**，Ruff、`py_compile` 与 diff
  检查通过。
- `/tree` 评估（不实现）：本项目以 append-only 事件流为真相源，
  checkpoint/rewind 负责文件回滚，`/fork <seq>` 与 fork 选择器负责从历史
  分叉；Pi 的消息树是另一套会话模型。强行引入会形成两套历史语义，收益低于
  维护成本。等价路径：`/fork` 选择器分叉、`/rewind` 回滚、`/sessions` 切换。
  结论同步记录在 `docs/DEBUG_LOG.md`。
目标分支：`feature/ui`（已于 bd99673 合并到 `main`）
调研日期：2026-10-21

## 1. 调研结论（先看证据）

### 1.1 Pi：本机有完整源码（MIT）

本机安装（不是网上抄的）：

- `D:/npm/node_modules/@earendil-works/pi-coding-agent` v1.3.0（MIT）
- 其内嵌 `@earendil-works/pi-tui` v1.3.0（MIT，独立 TUI 框架）

`dist/**/*.js.map` 带 `sourcesContent`，可直接读原始 TypeScript。关键文件：

| 文件 | 行数 | 关键设计 |
|---|---|---|
| `pi-tui/dist/tui.ts` | 1517 | `Component.render(width): string[]`；差分渲染；overlay 锚点；鼠标/焦点/硬件光标 |
| `pi-tui/dist/layout.ts` + `layout-node.ts` | 449 + 51 | VStack/HStack 的 auto/grow/shrink/minSize 布局 |
| `pi-tui/dist/components/scroll-view.ts` | 224 | 单子组件视口、follow-end、滚动条、内容高度与视口分离 |
| `pi-tui/dist/components/editor.ts` | 2473 | 多行编辑、词级换行、撤销、kill-ring、粘贴标记、CJK/emoji 分段、自动补全列表 |
| `pi-tui/dist/components/select-list.ts` | 273 | 通用方向键列表 |
| `interactive/chat-viewport.ts` | 46 | transcript（ScrollView）+ 固定 dock：pending 队列、status、editor、footer |
| `interactive/components/footer.ts` | 302 | cwd(~) + git 分支 + 会话名；↑↓ tokens、R/W cache、命中率、成本、上下文条、auto-compact |
| `interactive/components/tool-execution.ts` | 408 | 每个工具自带 `renderCall/renderResult`；partial→result；展开/收起；图片；预览 10 行 |
| `interactive/components/session-selector.ts` | 1045 | 搜索/排序/过滤/重命名/删除/树形模式 |
| `interactive/interactive-mode.ts` | 7127 | 事件接线、队列、overlay、会话生命周期 |

Pi 的对话区**不是 widget-per-message**：组件把内容渲染成行（`string[]`），由单个
`ScrollView` 按视口裁剪；输入区与 footer 固定在底部；补全和选择器走 overlay/焦点切换。

### 1.2 Codex：本机只有二进制，按公开行为对齐

`@openai/codex` 包只有启动脚本，TUI 是 Rust（ratatui）。可确认的交互（`codex --help`
与公开文档）：`resume` 选择器、`queue` 排队消息、`archive`/`delete`/`fork`、审批
overlay、`Ctrl+O` 类展开、内联 diff、状态栏。代码不可直接复用，按交互模式对齐即可。

### 1.3 我们与它们的结构差异

| 维度 | Pi / Codex | 当前实现 | 影响 |
|---|---|---|---|
| 对话区 | 行模型 + 视口裁剪 | 每条消息一个 Textual widget | 2000 条历史 66s，必须限量 |
| 输入区 | 独立 editor + 内联补全/overlay | TextArea + 提示行列表 | 补全不是浮层，焦点逻辑简单 |
| 状态 | editor 边框内嵌工作状态 + footer | 两行 Static | 信息密度低，更新靠手写刷新 |
| 工具展示 | 每工具 renderer、可展开、diff | Collapsible + 纯文本 | 写操作看不到 diff，展开粒度粗 |
| 队列 | pending 区可编辑/删除 | 只有计数 | 队列不可见、不可编辑 |
| 选择器 | 搜索/排序/重命名/删除 | 搜索/多排序/工作区范围/摘要预览；活动会话重命名/归档，已归档恢复/删除 | 已集成，共享确认与清理验证 |

## 2. 总体决策

1. **保留 Textual，不移植 Pi 的 TS 渲染器**：语言不通，重写差分渲染器收益低；
   Textual 8.2 已支持 `overlay`/`layer`/`position: absolute`、`TextArea`、`OptionList`。
2. **移植 Pi 的“行模型 + 视口”架构**：这是性能与交互复杂度的根因，必须搬。
3. **能直接复用的直接复用**：
   - 运行时：Textual 8.2、Rich（已有依赖），不新增运行时依赖；
   - 算法/格式：按 MIT 许可移植 Pi 的 `formatTokens`、`formatCwdForFooter`、
     `visual-truncate`、`fuzzy` 评分、session filter/sort、kill-ring/word-navigation、
     tool preview 常量；在文件头注明来源与许可；
   - 交互：Pi 的 chat viewport、footer 字段、session picker 能力、队列语义；
   - Codex：`resume` picker、排队消息、Esc 中断、展开快捷键、审批 overlay、内联 diff。
4. **渐进替换，不做大爆炸**：新实现放 `src/ui/`，`ui_textual.py` 变兼容壳；
   `HARNESS_UI=textual` 先切新实现，保留 `HARNESS_UI=textual-legacy` 一版用于对照。

## 3. 目标结构

```
src/ui/
  __init__.py
  app.py              # HarnessApp：组装 viewport/dock/modals，订阅事件
  theme.py            # 颜色 token（从 cc_ui 收口）
  state.py            # ConversationModel：blocks、队列、指标、会话元数据
  events.py           # harness 事件 → model 更新（唯一入口）
  viewport.py         # transcript + dock 布局（VStack）
  transcript/
    model.py          # Block 定义、版本、依赖（dirty/height cache）
    view.py           # VirtualTranscript（自定义 Widget/ScrollView）
    renderers.py      # message/tool/diff/notice → Rich 行
  composer/
    editor.py         # Composer(TextArea)：Enter 发送、Ctrl-J 换行、历史、队列
    completions.py    # CompletionOverlay（绝对定位浮层）
    queue.py          # 待发送消息条（可编辑/删除）
  chrome/
    status.py         # 工作状态（spinner/elapsed/permission）
    footer.py         # cwd(branch) · session · tokens · cache · ctx · cost · model
  modals/
    sessions.py       # 搜索/排序/重命名/删除
    approval.py       # 命令/diff 预览 + y/a/n
    commands.py       # 命令面板（fuzzy）
    search.py         # 会话内搜索 n/N
    models.py         # 模型选择
  legacy.py           # 旧 ui_textual 的兼容导出（迁移期）
```

`ui_textual.py` 暂时保留为 `from ui import HarnessApp, run` 的薄壳，现有测试继续可用；
迁移完成后删除。

## 4. 关键设计

### 4.1 对话模型 + 虚拟化（性能核心）

- `Block {id, kind, payload, version, expanded}`；kind：user/assistant/thinking/tool/diff/notice。
- 流式：`append_delta` 只更新 assistant block 的 payload 与 version，不重建树。
- 渲染：每个 block 在给定宽度下渲染成 `list[Text]`，高度缓存 `(width, version)`。
- `VirtualTranscript`：
  - 维护 block 起始行索引（前缀和），二分查找 `scroll_y` 对应 block；
  - 只渲染可见行 + 上下 overscan（如 40 行）；
  - 宽度变化/展开时失效高度缓存并保持“底部跟随”或当前位置；
  - 自定义滚动条、follow-end（新消息自动到底，用户上滚后暂停）；
  - 鼠标点击映射到 block（用于展开工具块）。
- 验收：10k blocks 滚动单帧 < 50ms；2k 历史首次挂载 < 200ms；50k 字符流式渲染
  CPU 时间低于 20%。

### 4.2 Composer（输入区）

- `TextArea` 原生多行/撤销；自定义键位：Enter 发送、Ctrl-J 换行、Tab 补全、
  ↑↓ 单行历史、Ctrl+P/N 历史、Ctrl+O 展开最近工具块。
- 补全浮层：Textual 8.2 `overlay` + `offset` 绝对定位到输入框上方；
  输入焦点保持在 editor；`↑↓` 选择、`Tab/Enter` 采用、`Esc` 关闭；
  数据源 `/` 命令（fuzzy）、`@` 文件（复用 `file_mentions`）。
- 队列条：editor 上方一行显示待发送消息；Ctrl+G/Alt+↑ 打开选择器单条取回或
  删除，Ctrl+R 在选择器内取回全部；发送后进入队列，回合结束按序执行。
- 工作状态嵌入 editor 顶边框（Pi 的 `embedWorkingStatus`）：spinner + 阶段 +
  已用时 + 队列数；不再单独占一行 status。

### 4.3 工具与 diff

- `ToolRenderer` 注册表：按工具名选择 renderer；未知工具回退通用预览。
  - bash：命令 + exit/耗时 + 输出头尾（默认 10 行）；
  - read/write/edit/apply_patch：路径 + 行数/变更摘要；
  - write/edit/apply_patch 有 diff 时渲染彩色 diff（`+/-` 行号）；
  - view_image/render_document：图片附件提示（TUI 暂不内嵌图片）；
  - 状态：pending/running/success/error/denied/queued；
  - `Ctrl+O` / 点击展开；默认成功折叠、失败展开（沿用当前行为）。
- diff 渲染复用 `workspace_diff` 输出，加 Rich 着色，不做二次解析。

### 4.4 Footer / 状态

- 两行：工作状态（editor 边框）+ footer（dock 底部固定）：
  `~/repo (branch) • session  ↑12k ↓3k R8k CH72%  ctx 42% [███░░]  ~¥0.0123  model`
- 数据来自 `ui_bridge` 指标（已有），加 `auto-compact` 标志与格式化函数（移植 Pi）。
- 更新节流：格式化值或 footer widget 变化时才调用 `Static.update()`，相同内容的周期
  刷新直接跳过，避免反复重建 renderable。

### 4.5 事件与状态

- `events.py` 是唯一入口：`tool_start/end`、`assistant_start/delta/end`、`status`、
  `round_end`、`session_end`、`queue_update`、`permission_*` → `ConversationModel`。
- 渲染层只读 model（Textual reactive）；禁止业务代码直接改 widget。
- 保留 session/agent 标签；子代理/teammate 事件按 session 过滤（已有）。

### 4.6 Modals

- 会话选择器：搜索标题/工作区/ID/摘要，支持最近更新/创建时间/名称排序与当前/全部
  工作区范围（`Alt+S` / `Alt+W`）；`Alt+V` 切换活动/已归档视图。活动会话可用
  `Alt+R` 重命名、`Alt+A` 归档；已归档会话 Enter 恢复、`Alt+D` 删除。归档列表后台
  加载；操作重新扫描所选条目并复用共享确认流程。当前会话不能归档，永久删除仍须输入
  “删除”；不开放直接删除活动会话。
- 审批：命令或 diff 预览 + `y`（一次）/`a`（本会话同类）/`n`；展示风险等级。
- 当前对话搜索：`Ctrl+F` 输入关键词，`Enter` / `Shift+Enter` 循环定位，高亮当前命中；
  搜索包含完整历史并按需补载较早页面。
- 命令面板：`Ctrl+Shift+P` 打开；复用 `COMMANDS`、技能命令注册与 `fuzzy_filter`，按命令/
  技能分组。无参数命令走现有分发；有参数提示的命令回填到 Composer，后续补全仍复用现有路径。
- 模型选择器：按配置来源列出 `MODEL_WINDOWS` 和 `llm_config.models` 中可用模型，fuzzy 搜索
  provider/model；当前主角色项默认高亮，并显示 provider 与已登记 context window。切换解析后
  写入 `HARNESS_MODEL_MAIN` 主角色运行时覆盖并同步兼容变量 `LLM_MODEL_ID`，更新状态栏模型和
  context window/占用。当前配置结构没有价格字段，不展示臆测价格；也不把主角色覆盖伪称为
  per-session 持久化设置。

## 5. 分阶段实施

| 阶段 | 内容 | 产出 | 验收 |
|---|---|---|---|
| P0 调研 | 本文件 + 源码笔记 + 技术 spike（自定义虚拟列表、overlay） | 决策记录 | spike 能跑通滚动/浮层 |
| P1 拆包 | `src/ui/` 骨架；`ui_textual.py` 变薄壳；不改行为 | 目录结构、CI 绿 | 现有 UI 测试全过 |
| P2 对话区 | `state.py` + `transcript/*`；历史/流式/恢复切到新视图 | 虚拟化 transcript | 2k/10k 基准达标；历史/恢复对比一致 |
| P3 输入区 | Composer + 补全浮层 + 队列条 + 边框状态 | 新输入区 | 键位矩阵测试；队列可编辑 |
| P4 展示层 | tool renderers + diff + footer + 状态 | 新展示层 | 快照对比；工具/审批/中断回归 |
| P5 选择器 | sessions/models/commands/search 重做 | 新 modals | 键盘路径全覆盖 |
| P6 切换 | ✅ `HARNESS_UI=textual` 指向新 app；legacy 保留 | 双实现对照 | 真实终端 checklist 待验收 |
| P7 清理 | ✅ 旧 Textual 实现、legacy 入口和专属测试已删除；默认入口已切 Textual | 单一实现 | 保留 ANSI 降级；真实终端验收仍待执行 |

预估（单人）：P1 1–2 天，P2 3–5 天，P3 2–3 天，P4 3–4 天，P5 2–3 天，
P6/P7 1–2 天；合计约 2–3 周。P2 是关键路径，建议先做 spike。

## 6. 测试与基准

- 单元：model reducer、block 高度缓存、fuzzy/session 过滤排序、footer 格式化。
- Textual `run_test`：键位矩阵（发送/换行/补全/队列/取消/展开/审批/选择器）、
  流式最终化、历史分页、宽度变化重排。
- 快照：固定会话 + 固定宽度导出渲染文本，防止视觉回归。
- 基准（提交到 `scripts/bench_ui.py`）：
  - 启动到可输入；
  - 2k/10k 历史首次渲染；
  - 10k blocks 连续滚动 100 帧；
  - 50k 字符流式输出总 CPU；
  - 会话切换（搜索+恢复）。
- 真实终端 checklist（尚未验收；Windows Terminal / PowerShell / VS Code 终端各一轮）：
  - [ ] 中文输入法、换行、粘贴及目录补全。
  - [ ] Enter 进下回合队列、Alt+Enter 立即进入当前回合、Ctrl+G 选择器取回/删除、Esc 取消。
  - [ ] 审批遮罩、允许同类、拒绝，以及退出后的终端恢复。
  - [ ] 鼠标选中复制、Ctrl-C / Ctrl+Shift+C、`/copy`。
  - [ ] 流式 Markdown、长会话滚动、窗口缩放与工具展开。
  - [ ] 会话恢复、搜索、归档恢复及删除短语确认（使用测试会话）。
  - [ ] checkpoint 回滚预览、取消、冲突提示及文件恢复（使用临时工作区）。

通过上述验收后再处理 P7 与合并；自动化 `run_test` 结果不能代替真实终端验收。
实际环境、操作步骤和结果写入 [UI 终端验收记录](UI_TERMINAL_ACCEPTANCE.md)。
自绘视口已补齐 offset 元数据、选区高亮与显示文本提取；自动回归使用真实鼠标
拖动覆盖中文/emoji、正反向、滚动后跨行和 Ctrl-C。窗口换宽重排时清除选区。
系统剪贴板与终端鼠标行为仍需人工验收。
分支同步、合并条件与 legacy 退场安排按 [开发流程](DEVELOPMENT_WORKFLOW.md) 执行。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 自定义虚拟列表复杂、易出滚动 bug | 先做 spike；保留“限量 400 条”作为降级路径 |
| 行渲染丢失 Textual 的选择/复制能力 | 保留 `Ctrl+A`/鼠标选择方案在 spike 验证；必要时对可见区挂 Markdown widget |
| Markdown 在行模型中重排成本高 | 流式与完成态统一 Markdown，120ms 限频并缓存渲染结果 |
| CJK/emoji 宽字符错位 | 复用 Textual `cell_len`/`rich.cells`；补宽度测试 |
| 双实现维护成本 | 新实现走 feature flag；legacy 只保留 1 个版本周期 |
| 浮层与 IME/焦点冲突 | 浮层不抢焦点，键位由 editor 统一分发；补 IME 手动测试 |
| 移植代码许可 | Pi 为 MIT；移植文件头注明来源与许可，不复制大段代码 |

## 8. 可直接复用的清单

**Textual（已有依赖）**：`App`、`ModalScreen`、`OptionList`、`TextArea`、`Markdown`、
`ScrollableContainer`、`Reactive`、`run_test`、`overlay/layer/offset`。

**Pi（MIT，移植算法/格式）**：
- `formatTokens`、`formatCwdForFooter`、上下文条阈值、缓存命中率显示；
- `visual-truncate`（按显示宽度截断，CJK 安全）；
- `fuzzy` 评分与排序；
- session filter/sort（`session-selector-search.ts`）；
- word-navigation、kill-ring（编辑器键位）；
- tool preview 行数/展开语义、`renderCall/renderResult` 分层；
- 队列“可编辑/可取回”的交互（`pendingMessages`）。

**Codex（交互对齐，无代码复用）**：resume picker、queue、Esc、展开键、审批 overlay、
内联 diff、footer 字段。

## 9. 已确定的后续安排

- `feature/ui` 仅继续本次验收与修复，验收通过后合入 main 并删除分支。
- 默认交互入口已切到 Textual；行式 REPL 保留为 `HARNESS_UI=ansi` 降级路径。
- 合并时保留 legacy 一个过渡版本，P7 核对引用与重复实现，后续短期分支完成删除。
- 图片内嵌显示不纳入本次收尾；后续按独立需求评估。
- 核心与 UI 共同以 main 为集成目标，后续功能使用短期分支，详见开发流程。
