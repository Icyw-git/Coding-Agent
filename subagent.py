"""子代理：精简版 agent loop，复用 harness 的 client / 提示词与 tools 的工具集。
"""
import json
import os

from agent_core import trigger_hooks, _extract_text
from tools import TOOLS, tool_registry


def _h():
    import harness
    return harness


SUB_TOOLS=TOOLS.copy()

SUB_HANDLERS=tool_registry.copy()

def spawn_subagent(description:str)->str:
    print(f'\n\033[35m[Subagent spawned]\033[0m')
    messages=[{'role':'system','content':_h().build_sub_system()},{'role':'user','content':description}]


    for _ in range(30):

        response=_h().client.chat.completions.create(
            model=os.getenv("LLM_MODEL_ID"),
            messages=messages,
            tools=SUB_TOOLS,
            temperature=0.7,
            max_tokens=8000,
        )
        message=response.choices[0].message
        messages.append(message.model_dump())

        if response.choices[0].finish_reason != "tool_calls":
            force=trigger_hooks('Stop',messages)
            if force:
                messages.append({'role':'user','content':force})
                continue
            if message.content:
                print(message.content)
            return message.content or "No output"

        tool_messages = []
        for tool_call in message.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            blocked = trigger_hooks("PreToolUse", name, args)
            if blocked:
                output = blocked
            elif name not in SUB_HANDLERS:
                output = f"Error: unknown tool {name}"
            else:
                output = SUB_HANDLERS[name](**args)

            trigger_hooks("PostToolUse", name, args, output)

            tool_messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })

        messages.extend(tool_messages)

    result=_extract_text(messages[-1]['content']) if isinstance(messages[-1].get('content'), str) else ""
    if not result:
        for m in reversed(messages):
            if m['role']=='assistant' and isinstance(m.get('content'), str):
                result=_extract_text(m['content'])
                if result:
                    break
        if not result:
            result="Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result
