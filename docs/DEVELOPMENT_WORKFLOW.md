# 开发与分支流程

## 分支职责

`main` 是稳定集成分支，最终同时包含核心功能、ANSI 与 Textual UI。
从 main 创建短期分支，完成一个可验收的改动后合回；不长期维护 UI 与核心两条产品线。

| 工作 | 当前分支安排 | 示例 |
|---|---|---|
| UI 验收修复 | 从 main 创建 fix/ui-* | 输入法、复制、焦点、审批、滚动、缩放、终端退出恢复 |
| UI 性能与运行时接线修复 | 从 main 创建 fix/ui-* | 流式显示、事件归属、取消、队列、会话切换 |
| 稳定版本紧急修复 | 从 main 创建 fix/*，合回 main 后同步 feature/ui | 安全、工具执行、API 或无头模式故障 |
| 独立核心功能 | 从 main 创建 feat/* | 模型提供商、MCP、沙箱、缓存、文档解析 |
| 跨 UI 与核心的大功能 | 本次 UI 合并后再从 main 创建分支 | 消息树、存储协议、执行调度重构 |

一个功能涉及查询、命令、弹窗时放在同一条功能分支，保持完整验证和回滚能力。
例如 `feat/session-search`；不为同一个功能另设长期 ui/core 分支。

## 本次合并状态与后续

`feature/ui` 已于 `bd99673` 合并到 main，旧 Textual 实现已按 P7 删除，默认入口
已切到 Textual；合并前检查确认 main 无独有提交、无冲突，合并后全量回归通过。
用户已确认真实终端验收通过，`feature/ui` 已删除，后续 UI 改动从 main 拉 `fix/ui-*`。
未完成的收尾：

1. [终端验收记录](UI_TERMINAL_ACCEPTANCE.md) 的环境/版本表与逐项结果仍为“未执行”；
   需要补真实记录，不能用自动测试数据代填。
2. ANSI 降级路径仍需在真实终端验证；默认入口为 Textual，
   `HARNESS_UI=ansi` 仅作降级。
3. P7 已完成：旧实现、`textual-legacy` 入口和专属测试已删除；
   `textual-next` 仅作兼容别名。

自动测试只证明回归通过，不能替代逐项人工记录；未记录项保持待验收。

## 同步 main 的修复

main 有新提交时，先 fetch 并检查工作区。以 main 的同一个修复提交为准，
通过 merge 同步到 feature/ui；避免手工复制修复或对已共享分支强推重写历史。
发生冲突时检查实际行为，解决后重新验证相关模块与 UI 接线。

合并前检查远端分支状态和 `git diff Coding-Agent/main...feature/ui`；远端变化后
重新评估合并候选，旧测试结果不能自动证明新的合并候选有效。

## 验证与发布记录

使用项目指定解释器 `D:/Anaconda_envs/envs/aitest01_py310/python.exe`。
按改动运行必要的定向测试；合并前运行主套件 `pytest tests/ -q`、
旧 UI/MCP 回归 `pytest test_ui.py test_mcp_ui_regression.py -q`、
增量 Ruff 和 `git diff --check`。性能修改补充 `scripts/bench_ui.py` 数据。
纯文档变更检查链接、命令、换行和 diff，无需重跑应用测试。

合并说明记录：问题与行为变化、共享模块影响、自动验证结果、人工验收记录、
已知限制、legacy 保留安排，以及默认入口为 Textual、ANSI 为降级路径。
验收记录注明提交、终端与 shell 版本、失败复现及修复后结果。
