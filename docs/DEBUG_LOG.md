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
              [PreToolUse hook] → 可阻断（黑名单硬拒 / 危险命令询问 / 越界询问）
              → tool_registry[name](**args)   ← 分发；task=子代理 / 慢命令=后台线程
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

## 核心教训总纲

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
