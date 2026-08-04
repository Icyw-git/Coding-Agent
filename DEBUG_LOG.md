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
