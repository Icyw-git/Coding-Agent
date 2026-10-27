"""子代理：精简版 agent loop，复用 harness 的 client / 提示词与 tools 的工具集。
"""
import copy
import os

import llm_route
from agent_core import _extract_text, expand_image_parts, image_attachment_message, trigger_hooks
from subagent_events import SubagentRecorder
from tools import TOOLS
from ui_bridge import agent_context

# 子代理拿不到父代理的 loop 级能力与协作编排：这些工具要么没有 handler
# （checkpoint/compact 由主循环特判），要么会把父子关系再展开一层
# （task/teammate/cron/worktree）。后台任务的输出也只能由主代理收集。
_SUBAGENT_EXCLUDED = frozenset({
    'task', 'compact', 'checkpoint', 'load_skill',
    'todo_write', 'todo_read',
    'spawn_teammate', 'send_message', 'check_inbox',
    'request_shutdown', 'request_plan', 'review_plan',
    'schedule_cron', 'list_crons', 'cancel_cron',
    'rename_session', 'revoke_permissions',
    'create_worktree', 'remove_worktree', 'keep_worktree',
    'background_output', 'kill_background',
    'screen_capture', 'screen_ocr',
    'clipboard_read', 'clipboard_write',
    'desktop_snapshot', 'desktop_windows', 'desktop_action',
})


def _h():
    import harness
    return harness


def _main_route():
    """子代理继承 main 角色；配置缺失时退回旧 LLM_MODEL_ID + 模块级 client。"""
    target = llm_route.target_for('main')
    api = llm_route.client_for('main', target)
    model = target.model if target is not None else os.getenv('LLM_MODEL_ID')
    return api, model


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
    recorder = SubagentRecorder(description)
    recorder.log_messages(messages)
    status = 'limit'
    result = ''

    try:
        for _ in range(30):
            recorder.turns += 1
            api, model = _main_route()
            response=api.chat.completions.create(
                model=model,
                # 图片附件在副本里展开，messages 本身只保留 harness_image 引用
                messages=expand_image_parts(messages),
                tools=sub_tools,
                temperature=0.7,
                max_tokens=8000,
            )
            message=response.choices[0].message
            dumped=message.model_dump()
            messages.append(dumped)
            recorder.log_message(dumped)

            if response.choices[0].finish_reason != "tool_calls":
                force=trigger_hooks('Stop',messages)
                if force:
                    injected={'role':'user','content':force}
                    messages.append(injected)
                    recorder.log_message(injected)
                    continue
                if message.content:
                    print(message.content)
                result=message.content or "No output"
                status='completed'
                return result

            tool_messages = []
            attachments = []
            for tool_call in message.tool_calls:
                # 与主循环共用同一条执行路径：审批、入口截断、checkpoint、UI 事件
                # 与后台判定都只在这里维护一份。子代理不再嵌套后台任务。
                with agent_context('subagent'):
                    _, _, tool_message, images = _h().exec_tool_call(
                        tool_call, registry=sub_handlers,
                        hooks=True, background=False)
                tool_messages.append(tool_message)
                recorder.log_message(tool_message)
                if images:
                    attachments.append(image_attachment_message(images))

            # 工具结果连续，图片附件排在整批之后（role=tool 里放图各家支持不一）
            messages.extend(tool_messages)
            messages.extend(attachments)
            recorder.log_messages(attachments)

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
    except BaseException as exc:
        status = 'cancelled' if isinstance(exc, KeyboardInterrupt) else 'error'
        error_type = 'cancelled' if status == 'cancelled' else type(exc).__name__
        recorder.finish(status, error_type=error_type, error=str(exc))
        raise
    finally:
        recorder.finish(status, result=result)
