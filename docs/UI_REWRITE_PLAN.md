# TUI 重构计划（对标 Pi / Codex）

状态：实施中（P1 完成、P0 虚拟化原型完成）
进展：
- P1 拆包：TUI 实现迁到 `src/ui/app.py`，根 `ui_textual.py` 变兼容壳，UI 测试 75 条全绿。
- P0 虚拟化：`src/ui/transcript/{model,layout}.py` + 基准脚本；10k blocks 布局 27ms、
  滚动 1.4ms/帧、流式增量 3.2ms。
- P0 浮层：Textual 8.2 `position:absolute + overlay:screen + offset` 验证通过，
  补全浮层可按 editor 坐标定位且不抢焦点。
- P2 起步：`VirtualTranscript(ScrollView)` 用 `virtual_size + render_line`
  只画可见行；滚动不重算布局，视口外不创建 widget。
下一步：把 `TranscriptLayout` 接到自绘视口（VirtualTranscript），再迁移历史/流式渲染。
目标分支：`feature/ui`
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
| 选择器 | 搜索/排序/重命名/删除 | 搜索+方向键（本轮刚加） | 缺排序/重命名/删除/preview |

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
- 队列条：editor 上方一行显示待发送消息；`Alt+↑` 取回编辑、`Alt+↓` 删除；
  发送后进入队列，回合结束按序执行（当前已有队列逻辑，补 UI）。
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
- 更新节流：只在值变化时刷新（100ms 合并），避免每帧重建 Text。

### 4.5 事件与状态

- `events.py` 是唯一入口：`tool_start/end`、`assistant_start/delta/end`、`status`、
  `round_end`、`session_end`、`queue_update`、`permission_*` → `ConversationModel`。
- 渲染层只读 model（Textual reactive）；禁止业务代码直接改 widget。
- 保留 session/agent 标签；子代理/teammate 事件按 session 过滤（已有）。

### 4.6 Modals

- 会话选择器：搜索 + 排序（更新时间/创建时间/名称）+ 过滤（当前工作区/全部）
  + 重命名/删除 + 预览；复用本轮 `SessionPickerModal` 骨架，补搜索/排序/操作键。
- 审批：命令或 diff 预览 + `y`（一次）/`a`（本会话同类）/`n`；展示风险等级。
- 命令面板：复用现有 `COMMANDS`，fuzzy 评分（移植 Pi `fuzzy`），分组显示。
- 模型选择器：列出 `MODEL_WINDOWS` 与价格，切换后写回会话状态。

## 5. 分阶段实施

| 阶段 | 内容 | 产出 | 验收 |
|---|---|---|---|
| P0 调研 | 本文件 + 源码笔记 + 技术 spike（自定义虚拟列表、overlay） | 决策记录 | spike 能跑通滚动/浮层 |
| P1 拆包 | `src/ui/` 骨架；`ui_textual.py` 变薄壳；不改行为 | 目录结构、CI 绿 | 现有 UI 测试全过 |
| P2 对话区 | `state.py` + `transcript/*`；历史/流式/恢复切到新视图 | 虚拟化 transcript | 2k/10k 基准达标；历史/恢复对比一致 |
| P3 输入区 | Composer + 补全浮层 + 队列条 + 边框状态 | 新输入区 | 键位矩阵测试；队列可编辑 |
| P4 展示层 | tool renderers + diff + footer + 状态 | 新展示层 | 快照对比；工具/审批/中断回归 |
| P5 选择器 | sessions/models/commands/search 重做 | 新 modals | 键盘路径全覆盖 |
| P6 切换 | `HARNESS_UI=textual` 指向新 app；保留 legacy 一版 | 双实现对照 | 真实终端 checklist 通过 |
| P7 清理 | 删除旧 `ui_textual.py`/`cc_ui` 中重复逻辑 | 单一实现 | 文档更新、全量回归 |

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
- 真实终端 checklist：Windows Terminal / PowerShell / VS Code 终端各一轮。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 自定义虚拟列表复杂、易出滚动 bug | 先做 spike；保留“限量 400 条”作为降级路径 |
| 行渲染丢失 Textual 的选择/复制能力 | 保留 `Ctrl+A`/鼠标选择方案在 spike 验证；必要时对可见区挂 Markdown widget |
| Markdown 在行模型中重排成本高 | 流式用纯文本行，结束时一次性渲染 Markdown 行并缓存 |
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

## 9. 待确认

1. 是否接受 ~2–3 周、约 2–3k 行新代码的重构（本文假设接受）。
2. 是否保留行式 REPL 作为降级（建议保留，`HARNESS_UI=ansi`）。
3. 图片在 TUI 内嵌显示是否必须（建议先不做，保留附件提示）。
4. 完成后是否合并回 `main`（建议先在 `feature/ui` 验收）。
