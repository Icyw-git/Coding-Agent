# Harness Agent 演进路径：从简单 agent loop 到 permission hook

记录 `harness.py` 如何从一个最小可用的 agent loop，逐步演进为带权限检查、钩子系统的完整实现。

---

## 第 0 步：最小 agent loop（起点）

最初只有一个 bash 工具 + 死循环，工具调用一次只处理一种情况。

```python
def agent_loop(messages: list):
    messages.append({'role': 'system', 'content': SYSTEM})

    while True:
        response = client.chat.completions.create(
            model=os.getenv("LLM_MODEL_ID"),
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        message = response.choices[0].message
        messages.append(message.model_dump())

        if response.choices[0].finish_reason != "tool_calls":
            print(message.content)
            return

        for tool_call in message.tool_calls:
            if tool_call.function.name == "bash":
                args = json.loads(tool_call.function.arguments)
                output = run_bash(args["command"])
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": output,
                })
```

**问题：**

- 只有 bash 一个工具，模型只能用 `dir` / `ls` 猜测文件内容。
- 工具分发用 `if` 硬编码，加新工具就要改循环体。
- 没有任何安全防护，`del` / `rm` 直接执行。

---

## 第 1 步：多工具注册（tool_registry）

引入 `tool_registry` 字典，把"工具名 → 函数"的映射独立出来，分发逻辑收敛到一行。

```python
tool_registry = {
    'bash': run_bash,
    'read': run_read,
    'write': run_write,
    'edit': run_edit,
    'glob': run_glob,
}

# agent_loop 中
output = tool_registry[name](**args)
```

**收获：**

- 新增工具只需：写函数 + 加一条注册 + 加 TOOLS schema。
- 分发从 `if/elif` 变成字典查找，`name not in tool_registry` 直接报错。

---

## 第 2 步：第一次安全尝试（危险命令黑名单）

在 `run_bash` 里加最朴素的黑名单。

```python
dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
if any(d in command for d in dangerous):
    return "Error: Dangerous command, please do not execute"
```

**问题：**

- 黑名单是**死规则**，模型换个写法就绕过（`Remove-Item`、`del` 都不在列表里）。
- 没有任何交互确认，误删只能事后发现。

---

## 第 3 步：工具与钩子分离（hook 系统诞生）

把"执行前检查"从循环体抽出来，设计成事件驱动的钩子系统，让扩展检查逻辑不再侵入主循环。

```python
HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}

def register_hook(event: str, callback):
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result  # 非 None 即阻断
    return None
```

**设计动机：**

- 4 个事件点覆盖完整生命周期：
  - `UserPromptSubmit` — 用户输入进入
  - `PreToolUse` — 工具执行前（可阻断）
  - `PostToolUse` — 工具执行后
  - `Stop` — 会话结束
- 每个检查做成独立 callback，**注册式**组合，主循环不再需要知道具体检查逻辑。

**agent_loop 接入钩子：**

```python
for tool_call in message.tool_calls:
    name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)

    blocked = trigger_hooks("PreToolUse", name, args)
    if blocked:
        output = blocked                 # 拒绝信息作为 tool 结果返回
    elif name not in tool_registry:
        output = f"Error: unknown tool {name}"
    else:
        output = tool_registry[name](**args)

    trigger_hooks("PostToolUse", name, args, output)

    tool_messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": output,
    })
```

**关键教训（踩过的坑）：**

- ❌ 被阻断时用 `continue` 跳过 → DeepSeek 报 `insufficient tool messages following tool_calls message`。每个 `tool_call_id` **必须**有一条 tool 消息回应。
- ✅ 正确做法：阻断也返回一条 tool 消息，内容为拒绝原因。

---

## 第 4 步：permission hook（权限检查落地）

用钩子承载完整的权限模型：黑名单直接拒绝 + 破坏性命令询问用户 + 越界访问询问用户。

```python
DENY_LIST = ['rm -rf /', 'sudo', 'shutdown', 'reboot', '> /dev/']
DESTRUCTIVE = ["rm ", "del ", "rmdir ", "rd ", "Remove-Item ", "erase ", "> /etc/", "chmod 777"]

def permission_hook(tool: str, args: dict):
    if tool == 'bash':
        for pattern in DENY_LIST:
            if pattern in args.get("command", ""):
                print(f'\033[31m⛔ Blocked: {pattern}\033[0m')
                return f"Error: Dangerous command {pattern}, please do not execute"

        for pattern in DESTRUCTIVE:
            if pattern in args.get("command", ""):
                print(f"\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {tool}({args})")
                choice = input("Allow? (y/n) ").strip().lower()
                if choice not in ('y', 'yes'):
                    return "Permission denied by user"

    elif tool in ["read", "write", "edit"]:
        path = args.get('path', '')
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\033[33m⚠  Access outside workspace\033[0m")
            print(f"   Tool: {tool}({path})")
            choice = input("Allow? (y/n) ").strip().lower()
            if choice not in ('y', 'yes'):
                return "Permission denied by user"

    return None

register_hook("PreToolUse", is_tool_hook)
register_hook("PreToolUse", permission_hook)
```

**权限模型三层结构：**
| 层级 | 行为 | 例子 |
|------|------|------|
| `DENY_LIST` | 直接拒绝，不询问 | `rm -rf /`、`sudo` |
| `DESTRUCTIVE` | 询问用户，n 则拒绝 | `del`、`Remove-Item`、`rmdir` |
| 越界访问 | 询问用户，n 则拒绝 | `read`/`write`/`edit` 访问工作区外 |

---

## 第 5 步：进化中的问题与对策（仍在路上）

### 问题 A：模型换命令绕过询问

用户对 `del` 输入 n，模型改用 `rm`、`powershell -Command "Remove-Item ..."` 最终删除成功。

**对策：**

- `DESTRUCTIVE` 用词边界正则（大小写不敏感）覆盖所有删除变体。
- 引入 `DENIED_PATHS` 会话级拒绝记忆：同一目标路径被拒后，后续任何删除命令直接拒绝不再询问。

```python
DENIED_PATHS: set[str] = set()

def check_permission(tool, args) -> bool:
    ...
    if targets & DENIED_PATHS:
        return False          # 该文件已被拒，硬拒绝
```

### 问题 B：纯代码追不上模型

黑名单永远有漏网之鱼。

**对策：** 增强 SYSTEM 提示词，从行为层约束：

> 用户拒绝后 STOP permanently，禁止重试、禁止换命令（del/rm/Remove-Item/powershell）实现同一目的。

---

## 最终架构图

```
用户输入
   │
   ▼
[UserPromptSubmit hook] ── context_injection
   │
   ▼
agent_loop: client.chat.completions.create()
   │
   ▼
每个 tool_call:
   ├─[PreToolUse hook] ── is_tool_hook / permission_hook（可阻断）
   │      │
   │      ├─ blocked ──► output = 拒绝信息
   │      └─ allowed ──► tool_registry[name](**args)
   │
   ├─[PostToolUse hook] ── log_hook / large_output_hook
   │
   └─► 追加 {role: "tool", tool_call_id, content}（必须有！）
   │
   ▼
finish_reason != tool_calls
   │
   ▼
[Stop hook] ── summary_hook → 结束
```

## 核心教训总结

1. **每个 tool_call 都必须有 tool 结果** —— 阻断 ≠ 跳过，阻断也要返回一条 tool 消息。
2. **安全是分层防御** —— 黑名单（硬）+ 询问（软）+ 拒绝记忆（会话级）+ 提示词（行为约束），单靠一层永远不够。
3. **钩子让安全逻辑可组合** —— 权限、日志、统计都注册为钩子，主循环保持稳定，新策略即插即用。

---

# 第二部分：子代理（Subagent）演进

主循环稳定后，给主代理增加了"派生子代理"的能力 —— 把复杂任务拆给独立运行的子循环完成，结果回报主代理。以下是它的演进路径。

## 第 6 步：子代理最小实现（task 工具）

在主循环之外再写一个独立的 `spawn_subagent` 函数，注册成 `task` 工具。它的核心是**复制一个精简版 agent loop**，用独立的 `SUB_TOOLS` / `SUB_HANDLERS`（不含 `task` 自身，禁止无限嵌套）。

```python
SUB_TOOLS = TOOLS.copy()
SUB_HANDLERS = tool_registry.copy()

def spawn_subagent(description: str) -> str:
    print(f'\033[35m[Subagent spawned]\033[0m')
    messages = [{'role': 'user', 'content': description}]

    for _ in range(30):                      # 30 轮上限，防止死循环
        response = client.chat.completions.create(
            model=os.getenv("LLM_MODEL_ID"),
            messages=messages,
            tools=SUB_TOOLS,
            max_tokens=8000,
        )
        ...
    print(f"\033[35m[Subagent done]\033[0m")
    return result

tool_registry['task'] = spawn_subagent
```

**设计动机：**

- 主代理负责拆解任务、和用户交互；子代理专注单项、独立跑完再汇报。
- `task` 工具让主代理的"思维链"得以表达 —— 它不用自己一步步做，而是委托出去。

## 第 7 步：子代理踩坑修复

### 坑 1：`message` 未定义 → NameError

```python
for tool_call in message.tool_calls:   # ❌ message 从未定义
```

子代理循环里只定义了 `response`，却引用 `message`。触发任何工具调用都会崩溃。

**修复：** 与主循环一致，先解出 message 再使用：

```python
message = response.choices[0].message
messages.append(message.model_dump())
```

### 坑 2：丢了 tool_calls 上下文

```python
messages.append({'role': 'assistant', 'content': response.content})   # ❌
```

`response.content` 是纯文本，`tool_calls` 信息被丢弃。下一轮模型不记得自己调用过哪些工具，上下文断裂。

**修复：** 用 `message.model_dump()` 保留完整结构（含 `tool_calls` 字段），与主循环保持一致。

### 坑 3：主代理返回结果取值混乱

原来 `return messages[-1]['content']`，但最后一轮可能是 tool 消息而非 assistant 文本。修复后统一 `return message.content`，并加 fallback 逻辑：循环结束还没答案时，从最近的 assistant 消息里取文本，否则返回 "Subagent stopped after 30 turns without final answer."。

## 第 8 步：子代理提示词（SUB_SYSTEM）

### 动机

子代理直接复用主代理的 SYSTEM 有两个问题：

- 主代理提示词面向**用户**，子代理的"用户"是父代理，输出对象不同。
- 主代理有"禁止换命令绕过"等强约束，子代理做检查类任务时会被过度束缚。

### 实现

新增独立的 `SUB_SYSTEM`，与主 SYSTEM 分离：

```python
SUB_SYSTEM = (
    f'You are a focused sub-agent working at {os.getcwd()} on behalf of a parent agent. '
    f'Complete the task you are given and return a concise final answer to the parent agent. '
    f'Rules: '
    f'(1) Stay strictly within the workspace; use only relative paths under {os.getcwd()}. '
    f'(2) Prefer safe commands (dir, type, findstr) and the read/glob tools for inspection. '
    f'(3) If the parent task asks you to write/edit/delete files, still ask yourself whether it is '
    f'destructive; if it is, do NOT execute it silently — report back that user confirmation is required. '
    f'(4) If you hit an error (tool fails, path missing), do not loop on the same command. Try one '
    f'reasonable alternative, then report the situation honestly to the parent. '
    f'(5) Keep the final answer short and structured: what you did, what you found, any issues. '
    f'Do not re-plan the parent task and do not spawn further sub-agents.'
)
```

**关键点：**

- 规则 (3)：破坏性操作不静默执行 —— 即使父代理要求，子代理也要回报"需要用户确认"。
- 规则 (5)：禁止嵌套再派发，配合 `SUB_TOOLS` 不含 `task` 实现双重防线。
- `spawn_subagent` 初始消息改为 `[{'role':'system','content':SUB_SYSTEM}, {'role':'user','content':description}]`。

## 第 9 步：子代理测试用例设计

| 用例       | 描述                   | 验证点                      |
| ---------- | ---------------------- | --------------------------- |
| 基础子代理 | glob + read + 返回摘要 | 完整链路走通                |
| 破坏性拦截 | 让子代理删除文件       | 规则 (3) 拒绝静默执行       |
| 错误恢复   | 读取不存在的文件       | 规则 (4) 尝试一次后如实上报 |
| 嵌套拦截   | 让子代理再派子代理     | `task` 不可用 + 规则 (5)    |
| 多步压力   | 审查所有 .py 文件      | 多轮循环 + 长输出           |
| 主代理对照 | 不派子代理直接做       | 主路径未被破坏              |

## 最终架构图（含子代理）

```
用户输入
   │
   ▼
[UserPromptSubmit hook]
   │
   ▼
主 agent_loop
   │
   ▼
每个 tool_call:
   ├─[PreToolUse hook]（可阻断）
   │      ├─ blocked ──► tool 消息 = 拒绝信息
   │      └─ allowed ──► tool_registry[name](**args)
   │                        │
   │                        ├─ task ──► spawn_subagent ──► 独立子循环
   │                        │            （SUB_TOOLS 无 task，禁止嵌套）
   │                        └─ 其他 ──► run_bash / run_read / ...
   ├─[PostToolUse hook]
   └─► 追加 tool 消息（必须有！）
   │
   ▼
[Stop hook] → 结束
```

## 第二部分核心结论

1. **子代理 = 独立 agent loop 的复用** —— 主循环踩过的坑（tool 消息必须成对、model_dump 保留 tool_calls），子代理全都要重踩一遍。
2. **提示词要按角色分离** —— 主代理面向用户，子代理面向父代理，各自有独立的 SYSTEM。
3. **禁止嵌套是双保险** —— 工具层面（SUB_TOOLS 无 task）+ 提示词层面（规则 5）。
4. **错误处理要"如实上报"** —— 子代理不需要自作主张修复一切，诚实报告比假装成功更重要。

---

# 第三部分：Skill 系统演进

让 agent 具备"按需加载领域知识"的能力 —— 通过 `skills/<name>/skill.md` 存放技能模板，agent 在任务中按名称加载后照着执行。

## 第 10 步：Skill 注册与扫描

### 目录约定

```
skills/
└── file-summarizer/
    └── skill.md        # frontmatter: name / description + body 正文
```

扫描逻辑把每个 skill 读入全局注册表：

```python
SKILL_REGISTRY: dict[str, dict] = {}

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    # 解析 YAML frontmatter，返回 (meta, body)
    ...

def _scan_skills():
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "skill.md"
        if manifest.exists():
            raw = manifest.read_text(encoding='utf-8', errors='replace')
            meta, body = _parse_frontmatter(raw)
            name = meta.get('name', d.name)
            SKILL_REGISTRY[name] = {'name': name, 'description': desc, 'body': body}
```

**设计动机：**

- skill 是**按需加载**的，不塞进工具列表，减少模型每次要看的工具数量。
- 一个 skill = 一个目录，方便维护和扩展。

## 第 11 步：踩坑修复

### 坑 1：`is_file()` 过滤掉所有 skill 目录

```python
for d in sorted(SKILLS_DIR.iterdir()):
    if not d.is_file():   # ❌ file-summarizer/ 是目录，全部被跳过
        continue
```

**结果：** `SKILL_REGISTRY` 永远为空 → `list_skills()` 返回 "No skills registered" → 模型只能手动用 bash 找 skill。

**修复：** `if not d.is_dir(): continue`。

### 坑 2：skill.md 编码

`manifest.read_text()` 默认用 GBK 读 UTF-8 文件 → `UnicodeDecodeError`。

**修复：** `read_text(encoding='utf-8', errors='replace')`。

### 坑 3：`build_system()` 的 format 结果被丢弃

```python
SYSTEM.format(catalog=catalog)   # ❌ 没接返回值，白调
```

且 SYSTEM 是 f-string 在顶层引用未定义的 `catalog` → **模块导入即 NameError**。

**修复：** SYSTEM 改为 `.format()` 模板（占位符 `{workdir}`/`{catalog}`/`{skills_dir}`），`build_system()` 真正 return 格式化结果；顶层不再引用 `catalog`。

## 第 12 步：Skill 如何进入 agent 流程

### 关键决策：`list_skills` 不注册为工具

```python
# ❌ 错误做法：把 list_skills 注册成工具
TOOLS.append({"name": "list_skills", ...})

# ✅ 正确做法：skill 目录通过 build_system() 注入提示词
def build_system() -> str:
    catalog = list_skills()
    return SYSTEM.format(workdir=WORKDIR, catalog=catalog, skills_dir=SKILLS_DIR)
```

**为什么：**

- `list_skills` 不是"技能"而是**流程的一环** —— 目录信息在会话开始时就已经在 system prompt 里，模型"天生知道"有哪些 skill，不需要再调工具去查。
- 减少工具数量，降低模型误用概率。

### 最终流程

```
build_system() ──► SYSTEM 含 "Skills available:\n- file-summarizer: ..."
                          │
                          ▼
模型决定用 skill ──► 调用 load_skill("file-summarizer")
                          │
                          ▼
load_skill 返回 body ──► 模型照着 skill 步骤执行
```

### 坑 4：示例误导模型

SYSTEM 里写 `load_skill {skills_dir}/skill.md`，模型以为要传**路径**，于是放弃工具、手动 bash 探索。

**修复：** 示例改为 `load_skill("file-summarizer")`，明确参数是**注册名**；工具 description 也强调"用 Skills available 列表里的准确名称"。

## 第 13 步：Skill 测试观察

测试用例：`请用 file-summarizer 总结 harness.py`。

### 修复前（模型迷路）

```
bash: dir /s /b ...\file-summarizer*     ← 手动找目录
glob: **/file-summarizer/**              ← (no matches)
read: skills/file-summarizer/skill.md    ← 手动读文件
```

模型完全没走工具，靠 shell 摸路，效率低且容易漏。

### 修复后（正确链路）

```
load_skill("file-summarizer")   ← 直接加载 ✓
glob("**/harness.py")           ← 按 skill 步骤
read("harness.py", limit=50)    ← 按 skill 步骤
[Stop] session used 4 tool calls
```

### 坑 5：最终回答被吞

模型跑完 skill 生成了总结报告，但 agent_loop 的结束分支只触发 Stop hook 就 `return`，**从不打印 `message.content`**。

```python
if response.choices[0].finish_reason != "tool_calls":
    force = trigger_hooks('Stop', messages)
    if force:
        messages.append({'role': 'user', 'content': force})
        continue
    if message.content:      # ← 曾丢失的打印
        print(message.content)
    return message.content or "No output"
```

**修复：** 恢复 `print(message.content)`。这个打印逻辑在一次重构中被误删。

## 第 14 步：Stop hook 的 force 机制（当前状态）

### 现状

```python
if response.choices[0].finish_reason != "tool_calls":
    force = trigger_hooks('Stop', messages)
    if force:                              # ← 当前恒为空
        messages.append({'role': 'user', 'content': force})
        continue
    return ...
```

当前注册的 Stop 钩子只有 `summary_hook`，它 `return None`，所以 `force` 永远是空，`if force:` 是**未生效的扩展点**。

### 设计意图

`force` 是"拦截结束"机制：让钩子在模型准备结束时介入，返回内容塞回对话，逼模型再答一轮。

```python
def require_summary_hook(messages):
    if "完成" not in messages[-1].get("content", ""):
        return "请确认：任务是否已完成？请明确回答完成或说明未完成原因。"
    return None
```

---

# 第四部分：上下文压缩（Context Compression）

长会话最大的敌人是上下文爆炸：token 成本、API 的 `prompt_too_long` 报错、模型在噪声里丢掉重点。压缩按**成本从低到高**分三级，能用 0 API 解决的绝不调用 LLM。

## 第 15 步：无 API 的三级预处理（每轮循环开头）

`agent_loop` 每轮调用 API 前先过三道"免费"工序，全部不花 token：

```python
while True:
    messages[:] = tool_result_budget(messages)   # ① 内存预算：大输出落盘换引用
    messages[:] = snip_compact(messages)         # ② 条数裁剪：中间段替换为 marker
    messages[:] = micro_compact(messages)        # ③ 工具结果精简：旧结果换占位符

    if estimate_size(messages) > CONTEXT_LIMIT:  # 仍超限才升级到 LLM 压缩
        messages[:] = compact_history(messages)
```

| 工序 | 触发条件 | 动作 |
|------|----------|------|
| `tool_result_budget` | 所有工具结果合计 > 200KB | 把最大的 >30KB 结果写入 `.cache/tool_results/<id>.txt`，原位替换为 `<persisted-output>` 引用 + 2000 字预览 |
| `snip_compact` | 消息数 > 50 条 | 保留头 3 条 + 尾部 47 条，中间替换为一条 `[snipped N messages]` |
| `micro_compact` | 工具结果 > `KEEP_RECENT`(3) 条 | 更早的 >120 字符结果原地替换为占位符（**原地修改**，省掉重建列表） |

**踩坑：裁剪不能制造孤儿 tool 消息。** OpenAI 硬约束：`role:"tool"` 必须对上一条 assistant 的 `tool_calls`。因此：

- `snip_compact` 保留区边界若落在 tool 结果上，要连同它前面的 assistant 一起保留（多占 1 条配额）；配对调整导致超预算时从尾部继续裁，并跳过孤立的工具结果。
- 头部同理：最后一条 assistant 带 `tool_calls` 时，其工具结果要一起保留。
- 同一问题在 reactive compact 里又踩了一遍（见第 17 步的 off-by-one）。

## 第 16 步：LLM 摘要压缩（第二级成本）

零成本工序挡不住真正的大会话，此时才调用一次 LLM：

```python
def compact_history(messages):                    # LLM 压缩
    transcript_path = write_transcript(messages)  # 完整对话先落盘，信息不丢
    summary = summarize_history(messages)         # LLM 摘要
    return [{'role': 'user', 'content': f'[Compacted]\n\n{summary}'}]
```

`summarize_history` 的摘要模板要求保留 5 类信息：**当前目标、关键发现/决策、读改过的文件、剩余工作、用户约束** —— 让压缩后还能无缝续做。

**两个触发点：**

1. **自动**：循环开头 `estimate_size(messages) > CONTEXT_LIMIT`(50000)。
2. **手动**：模型调 `compact` 工具主动清理（注册在 `TOOLS`，但**不进 `tool_registry`**，由 `agent_loop` 特殊处理）。

**踩坑：compact 在 for 循环内遇到时，之前已执行的工具结果不能保留为 tool 消息。** `compact_history` 替换整个上下文后，原 assistant 的 `tool_calls` 已不存在，之前的 `role:"tool"` 结果留下就是孤儿 → 转成纯文本附到新上下文，`compacted=True` 后 `continue` 重开一轮。

```python
if name == 'compact':
    messages[:] = compact_history(messages)
    note = '[Compacted] Conversation history has been summarized.'
    if tool_messages:                     # 已执行的工具结果转文本，不丢
        done = '\n'.join(f"- {str(tm['content'])[:1000]}" for tm in tool_messages)
        note += '\n\nTool results executed before compaction:\n' + done
    messages.append({'role': 'user', 'content': note})
    compacted = True
    break
```

## 第 17 步：reactive compact（第三级成本，出错才触发）

前两级都拦不住时，API 会直接报 `prompt_too_long`。与其让会话崩溃，不如在被拒后"抢救"一次：

```python
MAX_REACTIVE_RETRIES = 1

try:
    response = client.chat.completions.create(...)
    reactive_retries = 0
except Exception as e:
    if ('prompt_too_long' in str(e).lower() or "too many tokens" in str(e).lower()) \
            and reactive_retries < MAX_REACTIVE_RETRIES:
        reactive_retries += 1
        messages[:] = reactive_compact(messages)  # 完整落盘 + 保留最近 5 条，其余摘要
        continue
    raise
```

**要点：**

- `reactive_compact` 先把完整对话 `write_transcript` 落盘，再**保留最近 5 条**（含 tool_calls 配对），更早的历史全部交给 LLM 摘要 —— 最近的工作现场不压缩，之前只留结论。
- 重试上限 1 次，超限直接 `raise`，防止"压缩 → 又超限 → 再压缩"无限烧钱。
- **坑（off-by-one）**：`tail_start` 若落在 tool 结果上且其 assistant 在保留区内，要往前多保留 1 条，否则出现孤儿消息。

## 第 18 步：mock 驱动的端到端测试

- `FakeCompletions` 按队列提供响应，可模拟任意轮次；Exception 模拟 API 报错。
- **深拷贝快照**是关键：`agent_loop` 全程操作同一个 list 对象（`messages[:]`/append/extend），不深拷贝则所有历史记录都指向最终状态。
- `assert_valid_openai_msgs` 校验每轮快照：每条 `role:"tool"` 必须能对上前面 assistant 的 `tool_calls`，**全程无孤儿消息**。
- 端到端场景 `test_full_session_scenario`：多轮工具 → 自动压缩 → compact 工具 → reactive 压缩 → 最终回答，一条脚本跑完整个链路。通过 **patch `estimate_size` 返回值**精确控制自动压缩触发点（`[50, 10000, 50, 50, 50]`），不依赖脆弱的字符串长度阈值。
- 测试跑出了真 bug：snip 超预算、reactive off-by-one、`_block_type` 被删后测试引用报错。

### 测试基建

| 基建                       | 作用                                                                      |
| -------------------------- | ------------------------------------------------------------------------- |
| `FakeCompletions`          | 队列化 mock 响应 + 异常，记录每轮消息快照                                 |
| `patch_tool`               | 替换 `tool_registry` 处理器，记录工具执行顺序/参数                        |
| `assert_valid_openai_msgs` | 格式合法性校验，失败输出完整快照                                          |
| 模块 logger                | 每轮 API 调用、压缩触发点、工具执行都有带时间戳日志，排查报错直接看 trace |

## 第四部分核心结论

1. **换 API 格式 = 重写消息判定** —— Anthropic 移植代码最容易"静默失效"，判定函数必须按目标格式重写并用测试兜底。
2. **压缩分三级成本** —— 落盘/裁剪/占位符（0 API）→ LLM 摘要（1 API）→ 被动重试（出错才触发），别一上来就花钱。
3. **OpenAI 的 tool 配对是硬约束** —— 上下文被替换后不能留孤儿 `role:"tool"` 消息，结果要么成对保留，要么转成文本。
4. **测试基建比测试本身重要** —— 深拷贝快照、队列化 mock、格式校验器，让 bug 在测试里显形而不是线上崩。
5. **假数据要能精确触发分支** —— patch `estimate_size` 而不是靠真实长度，场景测试才可复现。

---

# 第五部分：实战修复（真实运行暴露的 bug）

第四部分的架构是静态设计的，而以下是它**第一次跑真实会话时逐个炸出来的 bug**。每一条都是"测试全绿 → 真机崩溃/行为异常"的教训。

## 第 19 步：工具参数透传的防御（模型幻觉参数 → TypeError 崩溃）

**现象：** 模型调用 `read` 时传了 schema 之外的多余参数，`TypeError: run_read() got an unexpected keyword argument ...`，整个进程直接崩。

**根因：** `agent_loop` 对模型的 JSON 参数**不校验**就 `tool_registry[name](**args)` 透传。`**args` 展开的 TypeError 发生在**函数体外**，`run_read` 内部的 `try/except Exception` 接不住。

**修复：** 调用点捕获异常，按工具惯例返回错误字符串，让模型自己纠正参数：

```python
try:
    output = tool_registry[name](**args)
except Exception as e:
    output = f"Error: {e}"   # 曾收窄为 TypeError，后放宽为 Exception
```

**教训：** 模型是系统边界，它的 JSON 参数不可信，必须兜底。工具函数内部的 `try` 保护不了"调用点之前"的展开错误。

## 第 20 步：压缩层级失效（budget 死代码 + 摘要截断 + 大输出灌上下文）

**现象：** transcript 里的摘要停在 `Context compression has` 半句话；模型压缩后被迫重读所有文件（`type DEBUG_LOG.md` 全文、`read harness.py` 全文），API 烧双倍。

**根因（三个叠加）：**

1. `tool_result_budget(max_bytes=200000)` **大于** `CONTEXT_LIMIT=50000` → 免费层①号永远先于花钱层④号触发，**死代码**。
2. `summarize_history` 输入 `[:80000]` 截的是**头部**（丢最近消息），`max_tokens=2000` 不够 → 摘要截断。
3. `type`/`read` 大文件全文（30KB 级）作为"最近工具结果"完整保留 → 迅速二次超限。

**修复（最小改动）：**

| 位置 | 改动 |
|------|------|
| `tool_result_budget` | 默认 `max_bytes=int(CONTEXT_LIMIT*0.8)`，免费层先于自动压缩 |
| `PERSIST_THERSHOLD` | 30000 → 10000，大输出第二轮回合就落盘换引用 |
| `run_read` | 无 `limit` 且 >500 行 → 自动截 200 行 + 提示分块读 |
| `summarize_history` | `max_tokens=4000`、`[-80000:]`（保留尾部）、模板强制"写完全部 5 节" |
| `agent_loop` | `just_compacted` 标志：压缩后第一轮跳过 `micro_compact`，保留刚拿到的工具结果 |

## 第 21 步：snip_compact 制造孤儿 tool 消息（400 崩溃）

**现象：** `BadRequestError: Messages with role 'tool' must be a response to a preceding message with 'tool_calls'`。

**根因：** 真实会话是"一个 assistant 带 2-3 个工具"的分组结构。`snip_compact` 裁剪边界落在**组内第 2+ 条 tool** 时，配对调整 `tail_start-=1` 只认"前一条是 assistant"，不生效 → 保留区以孤儿 tool 开头；over-trim 循环 `tail_start+=1` 同样会把边界推进到"组首 tool"而漏检。两条路径都会留下孤儿消息，下一轮 API 直接 400。

**测试为什么没拦住：** [validate_pairs](file:///d:/harness-agent/test_compress.py#L50-L64) 只查 **dangling**（assistant 无结果），不查 **orphan**（结果无 assistant）。修复时两处补齐：

```python
while tail_start < len(messages) and _is_tool_result_message(messages[tail_start]):
    tail_start += 1   # 边界落在工具结果上就无条件推进（其 assistant 必已被裁掉）
```

并给 `validate_pairs` 增加 orphan 断言 + 新增回归用例 `test_snip_compact_tool_groups`（2 工具分组 63 条）。

**教训：** 配对合法性要双向校验——dangling 和 orphan 都是硬错误，缺一边测试就是漏网之鱼。

## 第 22 步：todo 提醒死代码（计数器从不自增）

**现象：** `<reminder>Update your todos.</reminder>` 永远不触发。

**根因：** `rounds_since_todo` 只有"初始化 0 / 判断 >=3 / 重置 0"，**缺 `+=1`**，计数永远停 0。

**修复：** 用 `used_todo` 标志记录本轮是否调过 `todo_write`，每轮工具执行后统一计数，语义修正为"连续 3 轮工具轮没更新 todo 才提醒"：

```python
messages.extend(tool_messages)
rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
```

**教训：** 死代码比错误代码更隐蔽——不崩、不报错，只是什么都不做。计数器类逻辑要一眼能看出自增点。

## 第 23 步：Windows 编码与刷屏（Get-Content 乱码 + 重复打印）

**现象：** 输出一屏乱码（`绗?2 姝ワ細` 这类 UTF-8-as-GBK 典型产物），控制台杂乱；极端情况 `UnicodeEncodeError: 'gbk' codec can't encode '\ufffd'` 直接崩循环。

**根因：**

1. **cache 文件编码是好的**（UTF-8，验证无误）。乱码来自模型用 PowerShell `Get-Content` 读文件——PS 5.1 默认按系统 ANSI 码页（GBK）解码，不认 UTF-8 无 BOM 文件 → 垃圾字符 + U+FFFD。`cmd type` 是原始字节拷贝，`run_bash` 先按 UTF-8 解码，反而正确。
2. **重复打印**：`log_hook` 打 `output[:100]`，agent_loop/子代理又 `print(output[:200])`，同内容刷两遍。

**修复：**

| 位置 | 改动 |
|------|------|
| `run_bash` | 解码后 `out.replace('\ufffd','?')`，杜绝 GBK 控制台打印崩溃 |
| agent_loop / spawn_subagent | 删掉重复的 `print(output[:200])`，只留 `log_hook` |
| SYSTEM | 明确"读文件用 read 工具（支持 limit），避免 PowerShell Get-Content" |

**教训：** Windows 上"文件编码对"不代表"显示对"——工具链默认码页才是隐形杀手。提示词要从源头约束模型命令选择。

## 第五部分核心结论

1. **真机跑一遍顶十轮测试** —— 第 21 步的 orphan、第 22 步的死代码、第 23 步的编码，全是端到端跑出来的，mock 测试全部绿灯。
2. **校验器必须双向** —— dangling 和 orphan 都要断言，只查一边等于没查。
3. **压缩阈值按"触发顺序"设计** —— 免费层（budget/persist）必须先于花钱层（LLM 摘要）触发，否则就是死代码。
4. **模型是系统边界** —— 参数要兜底、命令选择要靠提示词约束，代码和提示词双保险。
5. **计数器要能一眼看到自增点** —— 只有初始化/判断/重置的计数器，多半是忘了 `+=1`。

