# 提案：工具按需发现（tool_search 式延迟注册）

日期：2026-11-01。状态：**提案，未实现**。承接 [PERFORMANCE_PROFILE.md](PERFORMANCE_PROFILE.md)
（固定开销 5,762 tokens/请求）。

**一句话**：把 41 个低频工具的 schema 从"每轮都发"改成"用时才发现"，理论上能把
5,762 tokens 压到约 1,700，但**它改的是交互模型，不是调优**，所以先出提案再动手。

---

## 1. 现状与机会

默认工具面（桌面关）实测：

| | 数量 | schema | tokens |
| --- | --- | --- | --- |
| 基础集（几乎每轮都用） | 10 | 5,660 字符 | **≈1,415** |
| 其余 | 41 | 17,390 字符 | **≈4,347** |
| 合计 | 51 | 23,050 字符 | ≈5,762 |

基础集取：`bash` `read` `write` `edit` `apply_patch` `grep` `glob` `ls`
`todo_write` `todo_read`。

其余 41 个按组：

| 组 | tokens | 典型工具 |
| --- | --- | --- |
| 杂项 | 1,278 | `move_paths` `remind` `web_fetch` `background_output` `delete_paths` `checkpoint` |
| 任务/团队 | 755 | `task` `spawn_teammate` `send_message` `request_plan` |
| 文档/图片 | 564 | `read_document` `render_document` `view_image` |
| 定时 | 423 | `schedule_cron` `list_crons` `cancel_cron` |
| 记忆 | 412 | `remember` `forget` `recall` |
| 会话 | 288 | `find_sessions` `rename_session` |
| 屏幕 | 284 | `screen_capture` `screen_ocr` |
| worktree | 226 | `create_worktree` … |
| 剪贴板 | 114 | `clipboard_read` `clipboard_write` |

**理论收益：−4,347 tokens/请求（−75%）**。

---

## 2. 三种做法

### 方案 A：静态分组 + 环境探测（成本最低，收益 0）

已落地（第 88 节）：桌面控制关着就不发那 6 个。**不减低频工具**——它们在任何环境下
都可能被用到，静态判断不了。

### 方案 B：`tool_search` + 延迟注册（业界主流）

```
每轮必发：基础 10 个 + tool_search            ≈1,600 tokens
模型调用 tool_search("定时任务") → 返回 3 个 cron 工具的 schema
下一轮起这些 schema 进入工具面（会话内粘性）
```

pi 的 `codemode` / MCP 的 `tool_search` 都是这个形态。

**收益**：稳态 −3,500～−4,000 tokens（模型学会以后很少搜）。
**成本**：多一次往返；模型要理解"先查再调"；首次用某类工具的那一轮反而更贵（1,600 + 3 个 cron ≈ 2,000，仍比 5,762 便宜）。

### 方案 C：按意图预加载（模型侧判定，隐藏状态）

按上一轮对话关键词预先把可能用到的组塞进工具面。**不推荐**：同一句话在不同上下文里
得到不同工具，行为不可复现，调试困难——这正是第 88 节明确拒绝过的方向。

---

## 3. 推荐：方案 B，但分两步

### 第一步（低风险，可独立验收）：只延迟"整组使用"的工具

只把 8 个整组（任务/团队、文档、定时、记忆、会话、屏幕、worktree、剪贴板）
纳入延迟，**杂项 1,278 保持常发**——它们零散、模型很难猜到组名。

实测这 8 组合计 **12,275 字符 ≈ 3,068 tokens**：

```
预期：5,762 → ≈2,693 tokens（保留 46%，−53%）
```

**验收**：跑 20 个真实任务，统计
- `tool_search` 调用率（目标 < 15% 的回合）；
- 因"找不到工具"导致的失败（目标 0）。

### 第二步（需观察后再定）：把杂项也纳入

只有当第一步的 `tool_search` 调用率稳定在低位，且没有出现"模型不知道有这个工具"
的失败，才继续。

---

## 4. 风险与对策

| 风险 | 对策 |
| --- | --- |
| **模型不知道有某个工具**（最坏失败模式：功能等于不存在） | `tool_search` 的结果要带"这一类还有哪些"，且系统提示里列**工具名清单**（只要名字，约 300 字符 ≈ 75 tokens） |
| 多一次往返 | 只在模型第一次需要该组时发生；会话内粘性 |
| 与 `HARNESS_*` 白名单/审批交互 | 延迟注册只改"发不发 schema"，不改 `tool_registry`，策略层照旧 |
| 指纹/缓存前缀抖动 | 粘性集合进 `_tools_fingerprint`；同一会话内稳定 |
| 子代理/teammate 的工具面 | 它们本来是白名单子集，不受影响 |
| 破坏现有测试 | `test_tool_surface.py` / `test_tool_schemas.py` 需要适配（子集语义已经在那里了） |

---

## 5. 为什么现在不做

三条理由，按重要性：

1. **它改的是交互模型。** 前面四项优化都是"同样的交互少花钱"，这一项要模型多学一步。
   一次性混进性能优化里，出问题时无法归因。
2. **失败模式更糟。** 多花 token 是可见的成本；"模型不知道有这个工具"是**静默的功能
   缺失**——用户看到的是"它就是不会用定时任务"。
3. **需要真实使用数据验收。** 目标指标（`tool_search` 调用率、找不到工具的失败率）
   只能在真实会话里测，而现在本机 API 余额不足（κ 校准也卡在这里）。

---

## 6. 如果要做，第一步的实现要点

```python
# tool_schemas.py
BASE_TOOLS = frozenset({'bash', 'read', 'write', 'edit', 'apply_patch',
                        'grep', 'glob', 'ls', 'todo_write', 'todo_read'})
DEFERRED_GROUPS = {
    'cron': ('schedule_cron', 'list_crons', 'cancel_cron'),
    'memory': ('remember', 'forget', 'recall'),
    ...
}

# tools.py：会话内粘性集合，由 tool_search 往里加
_active = set(BASE_TOOLS)
def activate(group): ...   # 返回该组 schema，并让它们进入后续请求

# tool_schemas：tool_search 自身常发
```

三处必须一起改，否则模型会拿到"半个工具栏"：

1. `tools.TOOLS` 按 `_active` 过滤（现在已按能力开关过滤，加一层）；
2. `_tools_fingerprint` 必须包含 `_active`（否则缓存归因会说"工具表变了"）；
3. `tool_search` 的实现要返回 **schema**，不只是名字——只给名字，模型还得猜参数。

---

## 7. 一句话

**这是唯一还有"量级"的杠杆（−50%～−75%），但它把"工具是常量"变成"工具是状态"。**
该做，但该单独做、单独验收、单独回滚。
