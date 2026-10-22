# Checkpoint / 回溯能力对照（Pi、Codex、本仓库）

## 结论先行

本仓库的快照不是单纯“会话树”，而是 **会话 + 工作区文件 pre-image** 的组合。
用户常见的“回到上一轮对话之前”语义现在由 `turn` 引用保证：

- `/rewind`（默认 `turn`）：回到最近一个用户回合开始前的会话和文件状态。
- `/rewind last`：只撤销最近一个有文件改动的节点，可能落在回合中间。
- `/undo`：等价于 `/rewind turn --yes`。
- `/rewind head`：回到最新节点；`/checkpoint [label]` 可手动打点。

## Pi

Pi 的持久化会话是带 `id`/`parentId` 的树；`/tree` 在当前文件中切换分支，
`/fork` 从早期消息创建新会话，`/clone` 复制当前叶。压缩会追加摘要和保留边界，
但不删除原始 entries。Pi 的选择器还支持显式删除会话（优先 `trash`）。

Pi 的“checkpoint”主要是上下文/压缩层面的概念，不是通用的工作区文件快照；
文件改动是否可回退主要依赖外部版本控制。相较之下：

- 本仓库有真正的文件 pre-image 和冲突检测；
- Pi 有更成熟的会话分支树、分支摘要和分支导航；
- 本仓库用 `/fork`/`/clone` 产生独立会话，而不是同一文件内的分支树。

## Codex CLI

Codex 的 app-server 协议明确区分“历史回退”和“文件回退”：

```text
ThreadRevertParams:
  Replace a paginated thread's durable history with the prefix before one turn.
  This only changes persisted conversation history.
  It does not revert local file changes.
```

Codex 还提供 `thread/archive`、`thread/delete`、分页 thread history、rollout
压缩/迁移和 SQLite 状态库；`ghost_snapshot`/rollback 机制主要服务于其本地
rollout 迁移和一致性校验，不能简单理解为通用的用户文件快照。

因此：

- 本仓库显式保存工作区 pre-image，文件回退能力比 Codex 的 `thread/revert` 更直接；
- Codex 的历史分页、归档/删除和 thread store 更成熟；
- 两者都不把 `bash`/外部进程造成的文件改动自动回滚。

## 当前不足与对齐方向

| 不足 | 当前行为 | 对齐建议 |
|---|---|---|
| 回退粒度 | 节点按 loop 迭代；`last` 可能是回合中间 | 已增加 `turn_start` 元数据和 `turn` 引用；`/rewind`/`/undo` 默认回回合起点 |
| 文件覆盖 | 只抓 `write`/`edit`/`apply_patch` 的 pre-image；bash 改动只提示 drift | 不伪装全量回滚；可在未来增加显式“快照工作区 delta”模式 |
| 会话分支 | `/fork`/`/clone` 生成独立会话 | 不强行移植 Pi 的树；保留 fork 选择器，避免双套历史语义 |
| 分支摘要 | 无 Pi 的离开分支摘要 | 需要时用独立 fork 会话，不在本期引入树模型 |
| 副作用 | 记忆、cron、任务、后台进程、worktree 不自动回滚 | 报告已列出；未来可在确认弹窗中逐项展示 |
| 大文件/秘密 | 不保存内容，恢复时跳过 | 保持安全边界，明确标注 unverified/skipped |
| 历史存储 | 会话 JSONL + checkpoint pre-image | 已有 checkpoint GC；下一步是压缩/清理策略 |
| 跨机器/协作 | 不支持 | 不在本期范围 |

关键原则：**能回滚的明确回滚，不能回滚的明确列出；不为了“看起来完整”而误删文件。**

## 缓存和内存后续

当前已有：

- 事件缓存 2 MiB 上限、长会话流式 rebuild；
- TUI 历史首屏 400 条、分页 200 条；
- 布局行缓存 6000 行、构建边建边淘汰；
- 流式 Markdown 自适应 50–300 ms 节流。

仍可继续做：

1. 为 `checkpoints` 增加按会话/时间的清理策略（当前 17 MB 级）；
2. 为超长会话做增量历史投影，而不是每次完整 rebuild；
3. 对流式 Markdown 做稳定前缀缓存，降低 100k 字符级回答的重复解析；
4. 为 `turn` 回退增加端到端验收：两轮文件修改、无文件回合、冲突和取消。
