"""子代理：精简版 agent loop，复用 harness 的 client / 提示词与 tools 的工具集。
"""
import copy
import json
import os

from agent_core import _extract_text, expand_image_parts, image_attachment_message, trigger_hooks
from tools import TOOLS, ImageResult

# 子代理拿不到父代理的 loop 级能力与协作编排：这些工具要么没有 handler
# （checkpoint/compact 由主循环特判），要么会把父子关系再展开一层
# （task/teammate/cron/worktree）。后台任务的输出也只能由主代理收集。
_SUBAGENT_EXCLUDED = frozenset({
    'task', 'compact', 'checkpoint', 'load_skill',
    'todo_write', 'todo_read',
    'spawn_teammate', 'send_message', 'check_inbox',
    'request_shutdown', 'request_plan', 'review_plan',
    'schedule_cron', 'list_crons', 'cancel_cron',
    'create_worktree', 'remove_worktree', 'keep_worktree',
    'background_output', 'kill_background',
})


def _h():
    import harness
    return harness


def _subagent_surface():
    """按当前实时注册表构造子代理工具面。

    MCP 工具在 initialize_mcp_tools 时才进入 TOOLS/tool_registry；导入期快照
    会把它们漏掉，还会把只有主循环能执行的工具一并广告出去。
    """
    registry = _h().tool_registry
    schemas = []
    handlers = {}
    for schema in TOOLS:
        name = schema['function']['name']
        if name in _SUBAGENT_EXCLUDED or name not in registry:
            continue
        if name == 'bash':
            schema = copy.deepcopy(schema)
            schema['function']['parameters'].get('properties', {}).pop(
                'run_in_background', None)
        schemas.append(schema)
        handlers[name] = registry[name]
    return schemas, handlers


def spawn_subagent(description:str)->str:
    print('\n\033[35m[Subagent spawned]\033[0m')
    messages=[{'role':'system','content':_h().build_sub_system()},{'role':'user','content':description}]
    sub_tools, sub_handlers = _subagent_surface()


    for _ in range(30):

        response=_h().client.chat.completions.create(
            model=os.getenv("LLM_MODEL_ID"),
            # 图片附件在副本里展开，messages 本身只保留 harness_image 引用
            messages=expand_image_parts(messages),
            tools=sub_tools,
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
        attachments = []
        for tool_call in message.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            blocked = trigger_hooks("PreToolUse", name, args)
            if blocked:
                output = blocked
            elif name not in sub_handlers:
                output = f"Error: unknown tool {name}"
            else:
                output = sub_handlers[name](**args)
                if isinstance(output, ImageResult):
                    if output.images:
                        attachments.append(
                            image_attachment_message(output.images))
                    output = output.text

            trigger_hooks("PostToolUse", name, args, output)

            tool_messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })

        # 工具结果连续，图片附件排在整批之后（role=tool 里放图各家支持不一）
        messages.extend(tool_messages)
        messages.extend(attachments)

    result=_extract_text(messages[-1]['content']) if isinstance(messages[-1].get('content'), str) else ""
    if not result:
        for m in reversed(messages):
            if m['role']=='assistant' and isinstance(m.get('content'), str):
                result=_extract_text(m['content'])
                if result:
                    break
        if not result:
            result="Subagent stopped after 30 turns without final answer."
    print("\033[35m[Subagent done]\033[0m")
    return result
