import json
import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
import openai

load_dotenv()
WORKDIR = Path(os.getcwd()).resolve()
MAILBOX_DIR = WORKDIR / '.mailbox'
MAILBOX_DIR.mkdir(exist_ok=True)

client = openai.OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL"),
)

active_teammates = {}


class MessageBus:
    # 邮箱用 UTF-8 显式读写：GBK 默认码页是 Windows 隐形杀手（见 DEBUG_LOG 第 10 部分）
    def send(self, from_agent: str, to_agent: str, content: str, msg_type: str = 'message'):
        msg = {
            'from': from_agent,
            'to': to_agent,
            'content': content,
            'type': msg_type,
            'ts': time.time(),
        }
        inbox = MAILBOX_DIR / f'{to_agent}.jsonl'
        with open(inbox, 'a', encoding='utf-8') as f:
            f.write(json.dumps(msg, ensure_ascii=False) + '\n')
        print(f"  \033[33m[bus] {from_agent} → {to_agent}: "
              f"{content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list:
        inbox = MAILBOX_DIR / f'{agent}.jsonl'
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in
                inbox.read_text(encoding='utf-8', errors='replace').splitlines() if line.strip()]
        inbox.unlink()
        return msgs

    def peek(self, agent: str) -> bool:
        inbox = MAILBOX_DIR / f'{agent}.jsonl'
        return inbox.exists() and inbox.stat().st_size > 0


BUS = MessageBus()


def _build_team_tools(agent_name: str):
    """teammate 工具集 = harness 核心工具子集（bash/read/write/edit/glob）+ send_message。

    harness 懒导入（在 spawn 时才 import），避免模块级循环导入；
    harness 被 import 后才有 tool_registry / TOOLS 可复用。
    """
    import harness
    names = ['bash', 'read', 'write', 'edit', 'glob']
    tools = [t for t in harness.TOOLS if t['function']['name'] in names]
    tools.append({
        'type': 'function',
        'function': {
            'name': 'send_message',
            'description': "Send a message to another agent (default 'lead'). "
                           "Use it to report progress or final results.",
            'parameters': {
                'type': 'object',
                'properties': {
                    'to': {
                        'type': 'string',
                        'description': "Recipient agent name, default 'lead'"
                    },
                    'content': {
                        'type': 'string'
                    }
                },
                'required': ['content']
            }
        }
    })
    handlers = {n: harness.tool_registry[n] for n in names}

    def safe_bash(command: str) -> str:
        # teammate 线程无人交互：DENY 硬拒 + DESTRUCTIVE 直接拒绝（拒绝比无人应答的询问安全）
        for pattern in harness.DENY_LIST:
            if pattern in command:
                return f"Error: Dangerous command {pattern}, please do not execute"
        for pattern in harness.DESTRUCTIVE:
            if pattern in command:
                return f"Error: Destructive command {pattern} rejected (teammate cannot confirm)"
        return harness.run_bash(command)

    handlers['bash'] = safe_bash

    def send_message(to: str = 'lead', content: str = ''):
        BUS.send(agent_name, to, content, 'message')
        return f"Sent to {to}"

    handlers['send_message'] = send_message
    return tools, handlers


def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    if name in active_teammates:
        return f'Teammate {name} already exists'

    system = (f"You are '{name}', a {role}. "
              f"Use tools to complete tasks. "
              f"Send results via send_message to 'lead'.")

    def run():
        import harness   # 懒导入避免循环导入（harness 顶层已 import agent_teams）
        messages = [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': prompt},
        ]
        sub_tools, sub_handlers = _build_team_tools(name)
        skip_micro_rounds = 0
        memories_content = harness.load_memories(messages)   # 记忆召回（只读，不提炼，避免污染全局记忆）

        for _ in range(10):
            inbox = BUS.read_inbox(name)
            if inbox:
                messages.append({
                    'role': 'user',
                    'content': f"<inbox>{json.dumps(inbox, ensure_ascii=False)}</inbox>",
                })
            # 与主循环同一套压缩预处理（budget → snip → micro → 超限 LLM 摘要）
            messages, skip_micro_rounds = harness.pre_compact_messages(messages, skip_micro_rounds)
            # 记忆注入：只往第一条 user 消息追加，副本不污染 messages
            request_messages = harness.inject_memories(messages, memories_content)
            try:
                response = client.chat.completions.create(
                    model=os.getenv('LLM_MODEL_ID'),
                    messages=request_messages,
                    temperature=0.7,
                    tools=sub_tools,
                    max_tokens=8000,
                )
            except Exception:
                break
            message = response.choices[0].message
            # 保留 tool_calls：只存 content 会丢 tool_calls → 孤儿 tool 消息 → API 400
            messages.append(message.model_dump())

            if response.choices[0].finish_reason != "tool_calls":
                break
            results = []
            for tool_call in message.tool_calls:
                # 复用主循环执行路径：hooks=False（teammate 无人交互，bash 已由 safe_bash 内层兜底）；
                # background=False（teammate 本身已是后台线程，不再嵌套后台任务）
                _, _, tm = harness.exec_tool_call(tool_call, registry=sub_handlers,
                                                  hooks=False, background=False)
                results.append(tm)
            messages.extend(results)   # extend：摊平成多条 tool 消息；append 会嵌套成一条

        # 取最近一条非空 assistant 文本作为最终汇报（content 可能是 str 或 content blocks）
        summary = "Done."
        for msg in reversed(messages):
            if msg.get('role') != 'assistant':
                continue
            content = msg.get('content')
            if isinstance(content, list):
                text = "".join(b.get('text', '') for b in content
                               if isinstance(b, dict) and b.get('type') == 'text')
            elif isinstance(content, str):
                text = content
            else:
                continue
            if text.strip():
                summary = text
                break
        BUS.send(name, 'lead', summary, 'result')
        active_teammates.pop(name, None)
        print(f"  \033[32m[teammate] {name} finished\033[0m")

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} spawned as {role}\033[0m")
    return f"Teammate '{name}' spawned as {role}"
