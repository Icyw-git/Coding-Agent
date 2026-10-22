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

- `.cache` 初始约 75 MB，其中 42 MB 是误放在缓存里的 `xmanrui/dsh-im` 公共仓库 clone；
  已确认无本地改动（HEAD `00e2683`）并清理，可按需重新 clone。当前剩余约 31 MB，
  可识别 Agent 数据约 29 MB，未识别文件仅 `probe_pt.py`（654 B，保留排查用）。
- `sessions` 约 9 MB / 46 个文件；`checkpoints` 约 17 MB / 177 个节点；`transcripts` 约 1.8 MB；`tool_results` 约 58 KB。
- 最大单个 session 约 5 MB：显式 `read_events()` 峰值约 23 MB；改为流式 rebuild 后
  重建峰值约 1.8 MB、当前约 0.5 MB。流式重建比缓存路径多花约 200 ms/次，是
  “长会话内存换 CPU”的有意折中。
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
存储 profile：已知 29.1 MB，可回收约 0 B；未识别 654 B（不处理）。
源数据（不自动删除）
  会话事件流: 9.0 MB · 46 个文件 · 最早 5 天前 → 保留；用 /archive 或 /delete 管理
  ...
可回收派生缓存
  工具大输出/图片: 58.4 KB · 2 个文件 → /cache gc [天数] [--apply]
  回溯快照: 17.2 MB · 398 个文件 · 177 个节点 → /checkpoint-gc [--all] [--keep N]
未识别目录（只报告，不自动清理）
  probe_pt.py: 654 B · 1 个文件 · 排查脚本，保留
```

独立 profile（含最大会话加载内存）：

```bash
python scripts/profile_storage.py --session largest
python scripts/profile_storage.py --json
python scripts/profile_storage.py --shallow
python scripts/bench_layout.py 10000 100
python scripts/bench_history.py 10000
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
| 流式 Markdown 最后一帧 | 仅 1 个 assistant block；正文 ≤100k 字符且渲染 ≤10000 行；完成/切换历史后释放 |
| `TranscriptLayout` | 按块版本/宽度缓存，宽度变化重建 |
| 审批历史/审查官缓存 | 固定上限，撤销时连带清空 |

`SessionEventStore` 默认只缓存不超过 **2 MiB** 的事件文件；超过后 `read_events()`
不保留完整事件表，`rebuild_openai_history()` 走两遍流式扫描（第一遍取
`tool_compacted` 占位，第二遍构建 messages）。`session_summary()` 另按
`(size, mtime)` 缓存摘要，不缓存完整事件。环境变量
`HARNESS_EVENT_CACHE_MAX_BYTES` 可调整阈值（默认 2097152，0 表示关闭事件缓存）。
这借鉴 Codex 的“历史分页/按需读取”和 Pi 的“只投影当前 active branch”思路：
保留 append-only 源数据，不把完整事件列表长期复制到内存。若 session 继续增长，
下一步再考虑分页/压缩存储，而不是删除历史。

超过事件缓存阈值时，首次 `rebuild_openai_history()` 仍走两遍流式扫描。后续在同一个
store 实例内追加普通消息/指标事件时，复用当前历史投影，只合并新增消息。
投影保存为紧凑 JSON bytes，返回时解码出独立对象，调用方修改不会污染缓存。
投影与新增事件合计最多 2 MiB，待合并事件最多 256 条；超过后退回完整回放。
这与完整事件缓存是两个独立预算，最坏可同时保留各 2 MiB 的序列化/源文件预算。

压缩、工具占位、snip、恢复事件使投影失效，复用完整回放算法；文件 size/mtime
变化也会使投影失效。包含 rewind 的历史依赖外部快照，本轮不缓存该投影，以保持
快照缺失校验。首次打开、跨实例 API 请求及频繁压缩不会获得常规追加路径的收益。

对照脚本 `python scripts/bench_projection.py --rounds 30 --events 1000` 使用临时目录、
相同事件流，计时不含 fsync；tracemalloc 测 Python 分配，不能等同进程 RSS：

| 样本 | 完整回放中位数 | 增量投影中位数 | 等价性 |
|---|---:|---:|---|
| 6.1 MB 历史压缩后连续追加 30 轮 | 92.12 ms | 0.21 ms | 最终历史逐条相等 |
| 本机 5.2 MB 真实会话，重复读取 10 次，无 tracer | 159.30 ms | 3.23 ms | 每次历史逐条相等 |

合成样本冷启动为 74.05/79.45 ms，峰值为约 6.21/6.17 MiB，主要收益在重复回放，
不是首次加载峰值。完整回放仍作为正确性对照和所有复杂事件的恢复路径。

TUI 布局同样不再长期保留所有块的行文本：`TranscriptLayout` 默认只缓存最近
6000 行，离屏块保留高度、需要时重渲染。`scripts/bench_layout.py` 的 10k 块对比：

| 模式 | model | 布局后 | 当前 | 峰值 | layout | visible | stream tail | 缓存行 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 完整缓存 | 5.8 MB | 9.5 MB | 9.6 MB | 10.1 MB | 约 56–64 ms | 约 7–8 ms | 0.057 ms | 30000 |
| 有界缓存 | 5.8 MB | 7.5 MB | **7.5 MB** | **8.1 MB** | 约 82 ms | 约 7.8 ms | 0.050 ms | 6000 |

稳定内存降低约 22%。全量构建现在边建边淘汰：每追加一块，超过行上限就从最旧
块开始释放行，只保留高度；因此峰值也从约 10.1 MB 降到约 8.1 MB。代价是首次
布局约多 15–20 ms。流式更新只从变更块重算起始行，不再每次扫全部 10k 块：
尾部流式段落旧方式约 1.2 ms/次，现在约 0.05 ms/次。后续若要继续降低峰值，
需要让块高度也可惰性计算。

### 历史分页实验

完整历史仍保留给 Agent，但 TUI 初始只把最近 400 条消息变成块；滚动到顶部或
`/more` 时每次向前补 200 条。10,000 条消息的启动对比：

| 模式 | UI 块数 | 就绪时间 | 当前内存 | 峰值 | 历史起点 |
|---|---:|---:|---:|---:|---:|
| 全量 | 10000 | 约 10.9 s | 18.1 MB | 27.8 MB | 0 |
| 初始 400 + 分页 | 400 | **约 0.57 s** | **2.3 MB** | **6.2 MB** | 9600 |

分页显著减少首次布局和 UI 内存，但不会减少 `_messages` 中供模型使用的完整历史；
它解决的是长会话进入界面时的首屏成本。可用
`HARNESS_HISTORY_INITIAL_MESSAGES` / `HARNESS_HISTORY_PAGE_MESSAGES` 改变窗口大小，
做真实终端对比。

## 6. 暂不采用

- 不自动删除会话、记忆、导出或未识别目录。
- 不在启动时做全量 GC；`/cache profile` 只读，清理必须显式。
- 不按“文件夹年龄”删除未知数据；没有引用关系证据就不动。
- 暂不引入 rollout zstd 压缩或分页存储；先把 profile 和可证明的回收规则稳定下来，再按真实增长数据决定。

## 7. 验证

- `tests/test_storage_profile.py`：源数据/派生/未识别分类、引用保护、checkpoint 回收预览、浅扫描。
- `tests/test_harness_app.py`：`/cache profile` 路由。
- `tests/test_tool_cache.py`：原有引用保护与预览/执行规则保持不变。
