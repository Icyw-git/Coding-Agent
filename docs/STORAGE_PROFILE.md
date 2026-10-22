# Agent 存储与内存 profile

## 1. 目标

先回答三个问题，再决定删什么：

1. 哪些数据是恢复源，删了就丢历史？
2. 哪些是派生缓存，可以重新生成或安全丢弃？
3. 哪些目录当前无法判断用途，必须只报告、不自动处理？

本项目的原则：**源数据只通过归档/删除会话处理；派生缓存按可证明规则回收；未知目录不猜。**

## 2. 调研：Pi 与 Codex 的做法

### Pi

本机 `@earendil-works/pi-coding-agent` v1.3.0 源码与文档显示：

- 会话是 append-only JSONL，默认在 `~/.pi/agent/sessions/`；恢复列表只扫描会话 header，并限制 header 扫描上限，避免为列表加载完整正文。
- `/resume` 选择器支持 `Ctrl+D` 删除；先尝试 `trash` CLI，失败才 `unlink`，优先可恢复删除（`docs/session-format.md`）。
- 自动压缩会追加 summary 和保留边界，**不删除原始 session entries**；`/compact` 也不清历史（`docs/sessions.md`、`docs/compaction.md`）。
- 图片在写入历史前按模型限制缩放，默认限制约 2000×2000、4.5 MiB base64 编码，避免一张图把历史撑大（`docs/models.md`）。
- prompt cache 只在模型声明生命周期且预计节省足够成本时预热；它是供应商缓存策略，不是磁盘 GC（`docs/settings.md`）。
- 没有启动时自动删除旧会话的机制；清理由会话选择器显式触发。

可复用结论：**列表按 header 增量读取、原始历史不因压缩删除、删除优先进 trash、图片/大对象设上限。**

### Codex CLI

本机 `codex-cli 0.162.1` 与 `codex doctor --json` 显示：

- 本地会话同时在 rollout JSONL（部分为 `.jsonl.zst` 压缩）和 SQLite thread history 中维护；`sessions/` 是活动区，`archived_sessions/` 是归档区。
- 提供显式 `codex archive`、`codex unarchive`、`codex delete`；`delete` 永久删除，`--force` 可跳过确认。`thread/archive`、`thread/delete` 也出现在 app-server 协议中。
- `migrate-rollouts` 把旧 rollout 转成分页 history；本地库包含 `thread_history_1.sqlite`、`state_*.sqlite`、`logs_*.sqlite`，并设置 WAL 与 `auto_vacuum=INCREMENTAL`。
- `codex doctor` 报告活动/归档 rollout 文件数、总字节、SQLite 完整性和 rollout/DB 一致性；它做诊断，不自动删除。
- 当前本机 Codex 数据：`sessions` 约 99 MB、`archived_sessions` 约 300 KB、`thread_history_1.sqlite` 约 46 MB、`logs_2.sqlite` 约 50 MB、`cache` 约 38 MB。可见“把历史压缩/分页”和“显式归档/删除”是两条独立机制。

可复用结论：**活动区与归档区分开；历史可分页/压缩；删除必须显式；诊断先报告大小和一致性。**

### 本仓库当前情况

以当前工作区为例（数字随使用变化）：

- `.cache` 总计约 75 MB；其中可识别的 Agent 数据约 29 MB，未识别目录约 42 MB（`dsh-im-reference`，不自动处理）。
- `sessions` 约 9 MB / 46 个文件；`checkpoints` 约 17 MB / 177 个节点；`transcripts` 约 1.8 MB；`tool_results` 约 58 KB。
- 最大单个 session 约 5 MB：完整读取事件约 12 MB 当前内存、23 MB 峰值，读取约 100 ms、重建历史约 200 ms。
- 之前对快照执行 `--keep 10` 后，21 个会话各保留 10 个节点；默认策略仍是 20，因此当前 `/cache profile` 显示快照可回收 0。

## 3. 分类与清理矩阵

| 类别 | 当前策略 | 命令 |
|---|---|---|
| 会话事件流 `sessions/` | 恢复真相源，不自动删 | `/archive`、`/delete` |
| 长期记忆 `memories/` | 知识源，不自动删 | 记忆工具 |
| 任务状态 `tasks/`、`os_tasks/` | 可恢复任务状态，不自动删 | `/watch`、任务系统 |
| 导出 `transcripts/` | 用户产物，不自动删 | 手动处理 |
| 工具大输出/图片 `tool_results/` | 超期且未被会话引用才删 | `/cache gc [天数] [--apply]` |
| checkpoint `checkpoints/` | 每会话保留最新 N 个；head/label 永不删 | `/checkpoint-gc`、`/checkpoint-gc --all` |
| `tmp/`、`sandbox-tmp/` | 只报告，过期项可手动处理 | 预览后手动 |
| API 访问日志、`cache_debug.jsonl` | 审计/诊断，默认保留 | 按需轮转 |
| 未识别目录/文件 | 只报告，不猜 | 手动确认 |

关键原则：

- 被会话引用的工具缓存永远不删；宁可少删，也不让旧图/大输出失效。
- checkpoint 删除会切断更早回溯链；默认只预览，显式确认才执行。
- 未识别目录（例如误放到 `.cache` 的仓库）不能自动删除；`/cache profile` 会单独列出。
- 会话归档/删除与缓存回收互不替代：归档会话不会自动清它的派生文件。

## 4. 使用方式

```text
/cache profile
/cache gc 7
/cache gc 7 --apply
/checkpoint-gc
/checkpoint-gc --all --keep 20
```

`/cache profile` 输出示例（会随实际数据变化）：

```text
存储 profile：已知 29.0 MB，可回收约 0 B；未识别 42.4 MB（不处理）。
源数据（不自动删除）
  会话事件流: 9.0 MB · 46 个文件 · 最早 5 天前 → 保留；用 /archive 或 /delete 管理
  ...
可回收派生缓存
  工具大输出/图片: 58.4 KB · 2 个文件 → /cache gc [天数] [--apply]
  回溯快照: 17.2 MB · 398 个文件 · 177 个节点 → /checkpoint-gc [--all] [--keep N]
未识别目录（只报告，不自动清理）
  dsh-im-reference: 42.4 MB · 859 个文件 · 请确认用途后手动处理
```

独立 profile（含最大会话加载内存）：

```bash
python scripts/profile_storage.py --session largest
python scripts/profile_storage.py --json
python scripts/profile_storage.py --shallow
```

`--session` 用 `tracemalloc` 统计读取事件、重建 OpenAI 历史和峰值；它只测量存储/历史层，不包含 Textual 界面渲染。

## 5. 内存侧的有界缓存

除磁盘外，长期进程里还需要有界缓存：

| 缓存 | 上限/策略 |
|---|---|
| `_session_summary_cache` | 512 个会话摘要，超出淘汰最旧条目 |
| `agent_runtime._summary_cache` | 512 个（已有） |
| `project_context._cache` | 256 个工作区说明 |
| TUI Markdown Console | 16 个宽度 |
| `VirtualTranscript._line_cache` | 4000 行 |
| `TranscriptLayout` | 按块版本/宽度缓存，宽度变化重建 |
| 审批历史/审查官缓存 | 固定上限，撤销时连带清空 |

活跃会话的 `SessionEventStore._events_cache` 仍保存完整解析事件；`messages` 还保存重放后的历史。这是当前“恢复速度 vs 内存”的折中。若单个会话继续增长，下一步应参考 Codex 的分页 history / Pi 的 active-branch projection，而不是简单把历史删掉。

## 6. 暂不采用

- 不自动删除会话、记忆、导出或未识别目录。
- 不在启动时做全量 GC；`/cache profile` 只读，清理必须显式。
- 不按“文件夹年龄”删除未知数据；没有引用关系证据就不动。
- 暂不引入 rollout zstd 压缩或分页存储；先把 profile 和可证明的回收规则稳定下来，再按真实增长数据决定。

## 7. 验证

- `tests/test_storage_profile.py`：源数据/派生/未识别分类、引用保护、checkpoint 回收预览、浅扫描。
- `tests/test_harness_app.py`：`/cache profile` 路由。
- `tests/test_tool_cache.py`：原有引用保护与预览/执行规则保持不变。
