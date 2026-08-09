import os 
from dotenv import load_dotenv
import openai
import json
import time
import threading
from pathlib import Path
from typing import Optional, List
from background_task import should_run_background, start_background_task, collect_background_results
import recovery
from task_system import create_task, list_tasks, get_task, claim_task, complete_task
from cron_scheduler import (schedule_job, cancel_job, consume_cron_queue,
                            cron_lock, scheduled_jobs, cron_scheduler_loop,
                            load_durable_jobs, agent_lock,
                            queue_processor_loop, wait_for_cron_idle)
from agent_teams import spawn_teammate_thread, BUS
from agent_core import (HOOKS, register_hook, trigger_hooks, _extract_text,
                        _parse_frontmatter, write_memory_file, _rebuild_index,
                        read_memory_index, read_memory_file, list_memory_files,
                        select_relevant_memories, load_memories, extract_memories,
                        consolidate_memories, estimate_size, _message_has_tool_use,
                        _is_tool_result_message, collect_tool_results, snip_compact,
                        micro_compact, persist_large_output, tool_result_budget,
                        write_transcript, summarize_history, compact_history,
                        reactive_compact, inject_memories)
from tools import TOOLS, tool_registry, run_bash
from skill_system import scan_skills, list_skills, load_skill
from subagent import spawn_subagent
import hooks   # 副作用：注册 6 个 hook 回调（权限/日志/上下文/总结）



load_dotenv()
WORKDIR = Path(os.getcwd()).resolve()
SKILLS_DIR=WORKDIR/"skills"
scan_skills()   # 扫描技能目录 → SKILL_REGISTRY（skill_system 内）



client=openai.OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL"),
    timeout=120,   # API 挂起时最多等 120s 抛错，避免静默阻塞 600s（SDK 默认）
)

PROMPT_SECTIONS={
    'identity_main':(
        'You are a coding agent working on Windows at {workdir}. '
        'Use the bash tool to execute shell commands (Windows cmd syntax works, e.g. dir instead of ls). '
    ),
    'identity_sub':(
        'You are a focused sub-agent working at {workdir} on behalf of a parent agent. '
        'Complete the task you are given and return a concise final answer to the parent agent. '
    ),
    'rules_main':(
        'Rules: '
        '(1) If the user denies or rejects an operation, STOP permanently. Do NOT retry it and do NOT find alternative '
        'commands or workarounds (del/rm/Remove-Item/powershell/etc.) to achieve the same result. '
        '(2) Prefer safe, non-destructive commands for inspection (dir, type, findstr). '
        'To read file contents prefer the read tool (supports limit); avoid PowerShell Get-Content, '
        'which mis-decodes UTF-8 files on this system. Only modify files when asked. '
    ),
    'rules_sub':(
        'Rules: '
        '(1) Stay strictly within the workspace; use only relative paths under {workdir}. '
        '(2) Prefer safe commands (dir, type, findstr) and the read/glob tools for inspection. '
        '(3) If the parent task asks you to write/edit/delete files, still ask yourself whether it is '
        'destructive; if it is, do NOT execute it silently — report back that user confirmation is required. '
        '(4) If you hit an error (tool fails, path missing), do not loop on the same command. Try one '
        'reasonable alternative, then report the situation honestly to the parent. '
        '(5) Keep the final answer short and structured: what you did, what you found, any issues. '
        'Do not re-plan the parent task and do not spawn further sub-agents. '
    ),
    'skills_usage':(
        'Skills available:\n{catalog}\n'
        'Use load_skill to load a skill. E.g. load_skill("file-summarizer")'
    ),
}

def assemble_system_prompt(context:dict)->str: # 组装系统提示，包含身份、工具、工作目录、记忆、技能、规则等
    sections=[]
    sections.append(PROMPT_SECTIONS['identity_main'].format(
        workdir=context.get('workspace',WORKDIR)
    ))
    tools=','.join(context.get('enabled_tools',[]))
    if tools:
        sections.append(f'Available tools:{tools}.')
    sections.append(f'Working directory:{context.get("workspace",WORKDIR)}.')

    memories=context.get('memories','')
    if memories:
        sections.append(f'Relevant memories:\n{memories}')
    skills=context.get('skills','')
    if skills:
        sections.append(PROMPT_SECTIONS['skills_usage'].format(catalog=skills))
    sections.append(PROMPT_SECTIONS['rules_main'])
    return '\n\n'.join(sections)

_last_context_key=None # 缓存键，用于判断是否需要重新组装系统提示
_last_prompt=None # 缓存系统提示，用于判断是否需要重新组装系统提示

# ============================================================
# 全局配置集中区
# ============================================================

# ---------- 记忆系统配置 ----------
MEMORY_TYPES=['user','feedback','project','reference']
MEMORY_DIR=WORKDIR/'.cache'/'memories'
MEMORY_INDEX=MEMORY_DIR/'MEMORY.md'
CONSOLIDATE_THRESHOLD=10

# ---------- 安全策略 ----------
DENY_LIST=['rm -rf /','sudo','shutdown','reboot','> /dev/']
DESTRUCTIVE=["rm ","del ","rmdir ","rd ","Remove-Item ","erase ","> /etc/","chmod 777"]

# ---------- 模型上下文配置 ----------
MODEL_WINDOWS={
    'deepseek-v4-flash':131072,
    'deepseek-chat':65536,
    'deepseek-reasoner':65536,
}
OUTPUT_RESERVE=8192        # 给 max_tokens=8000 输出预留
CTX_SAFETY=0.8             # 留余量，防 prompt_too_long

def _default_context_limit()->int:
    window=int(os.getenv('LLM_CONTEXT_WINDOW') or
               MODEL_WINDOWS.get(os.getenv('LLM_MODEL_ID',''),65536))
    return max(4000,int((window-OUTPUT_RESERVE)*CTX_SAFETY))

CONTEXT_LIMIT=_default_context_limit()
KEEP_RECENT=6
PERSIST_THERSHOLD=10000

# ---------- 缓存目录 ----------
TOOL_RESULTS_DIR=WORKDIR/'.cache'/'tool_results'
TRANSCRIPT_DIR=WORKDIR/'.cache'/'transcripts'

# ---------- 重试配置 ----------
MAX_REACTIVE_RETRIES=1


def get_system_prompt(context:dict)->str: #获取上下文，如果上下文没有改变，直接返回缓存的系统提示，否则重新组装系统提示
    global _last_context_key,_last_prompt
    key=json.dumps(context,sort_keys=True,ensure_ascii=False,default=str)
    if key==_last_context_key and _last_prompt:
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    _last_context_key=key
    _last_prompt=assemble_system_prompt(context)

    loaded=['identity','tools','workspace','rules']
    if context.get('memories'):
        loaded.append('memory')
    if context.get('skills'):
        loaded.append('skills')
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt

def update_context(context:dict,messages:list)->dict: # 更新上下文，包含工具、工作目录、记忆、技能等
    memories=''
    if MEMORY_INDEX.exists():
        content=MEMORY_INDEX.read_text(encoding='utf-8',errors='replace').strip()
        if content:
            memories=content
    skills=list_skills()
    return {
        'enabled_tools':list(tool_registry.keys()),
        'workspace':str(WORKDIR),
        'memories':memories,
        'skills':skills,
    }


def build_system()->str:
    return assemble_system_prompt(update_context({},[]))


def build_sub_system()->str:
    return (
        PROMPT_SECTIONS['identity_sub'].format(workdir=WORKDIR) +
        '\n\n' +
        PROMPT_SECTIONS['rules_sub'].format(workdir=WORKDIR) +
        '\n\n' +
        PROMPT_SECTIONS['skills_usage'].format(catalog=list_skills())
    )


# ── 记忆 / 压缩 / hooks 逻辑已提取到 agent_core.py ──


















# Task tools











# ── Cron Tools ──







# ── Team Tools ──





















tool_registry['task']=spawn_subagent



tool_registry['load_skill']=load_skill

# compact 工具：让模型在上下文过长时主动触发历史摘要
# （不注册进 tool_registry，由 agent_loop 特殊处理）

# ============================================================
# 可复用模块：压缩预处理 / 记忆注入 / 工具执行
# 压缩原语（estimate_size/snip_compact/micro_compact/compact_history 等）
# 与记忆、hooks 已提取到 agent_core.py，此处只保留 agent_loop 的编排函数
# ============================================================

def pre_compact_messages(messages, skip_micro_rounds=0):
    """每轮开头的压缩预处理：budget → snip → micro →（仍超限）LLM 摘要。
    返回 (压缩后的 messages, 新的 skip_micro_rounds)。"""
    messages = tool_result_budget(messages)
    messages = snip_compact(messages)
    if skip_micro_rounds > 0:
        skip_micro_rounds -= 1   # 压缩后前几轮跳过 micro_compact，保留刚拿到的工具结果
    else:
        messages = micro_compact(messages)
    if estimate_size(messages) > CONTEXT_LIMIT:
        print("[auto compact]")
        messages = compact_history(messages)
        skip_micro_rounds = 3
    return messages, skip_micro_rounds


def exec_tool_call(tool_call, registry=None, *, hooks=True, background=True):
    """执行单个 tool_call：PreToolUse 权限 hook → 后台/直接执行 → PostToolUse → 异常兜底。
    返回 (name, args, tool 消息 dict)。主循环与 teammate 共用同一条安全/兜底路径。
    hooks=False：teammate 线程无法交互询问，跳过 hook（由工具集内层安全检查兜底）。"""
    name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)
    if registry is None:
        registry = tool_registry
    if hooks:
        blocked = trigger_hooks("PreToolUse", name, args)
    else:
        blocked = None
    if blocked:
        output = blocked
    elif name not in registry:
        output = f"Error: unknown tool {name}"
    else:
        try:
            if background and should_run_background(name, args):
                bg_id = start_background_task(tool_call, registry)
                output = (f"[Background task {bg_id} started] "
                          f"Command: {args.get('command', '')}. "
                          f"Result will be available when complete.")
            else:
                output = registry[name](**args)
        except Exception as e:
            # 模型可能传 schema 之外的参数（如 offset），**args 展开时在函数体外抛错
            output = f"Error: {e}"
    if hooks:
        trigger_hooks("PostToolUse", name, args, output)
    return name, args, {"role": "tool", "tool_call_id": tool_call.id, "content": output}

def agent_loop(messages:list):
    context=update_context({},messages)
    recovery_state=recovery.RecoveryState()
    if messages:
        trigger_hooks('UserPromptSubmit',messages[-1]['content'])
    system=get_system_prompt(context)
    reactive_retries=0 #响应重试次数
    skip_micro_rounds=0 #跳过 micro_compact 轮数
    rounds_since_todo=0 #距离上次 todo 提醒的轮数
    # 记忆加载：整个 Loop 只在此处加载一次（LLM 从 .cache/memories 目录筛选相关记忆）。
    # ⚠️ 提醒：循环中途不会再次刷新 —— 本轮新产生的信息不会进入 memories_content；
    #    要等会话结束 extract_memories 落盘后，下一次会话的 load_memories 才能召回。
    memories_content=load_memories(messages)   # 相关记忆块（空串 = 无相关记忆）

    while True:
        pre_compress=[m if isinstance(m,dict) else {'role':m.get('role',''),'content':str(m.get('content',''))} for m in messages] #压缩前的快照：纯净对话（不含 cron/提醒），供会话结束提炼记忆
        

        messages, skip_micro_rounds = pre_compact_messages(messages, skip_micro_rounds)

        if rounds_since_todo >=8 and messages:
            messages.append({
                'role':'user',
                'content':"<reminder>Update your todos.</reminder>",
            })
            rounds_since_todo=0

        fired=consume_cron_queue()
        for job in fired:
            messages.append({
                'role':'user',
                'content':f'[Scheduled] {job.prompt}'
            })
            print(f"  \033[35m[inject cron] {job.prompt[:50]}\033[0m")

        




        # 记忆注入 + system 前置（副本不污染 messages；SDK 不支持 system= 关键字参数）
        request_messages = inject_memories(messages, memories_content, system)

        def _create():
            max_tokens=(recovery.ESCALATED_MAX_TOKENS
                        if recovery_state.has_escalated
                        else recovery.DEFAULT_MAX_TOKENS)
            return client.chat.completions.create(
                model=recovery_state.current_model or os.getenv("LLM_MODEL_ID"),
                messages=request_messages,
                tools=TOOLS,
                temperature=0.7,
                max_tokens=max_tokens,
            )

        try:
            response=recovery.with_retry(_create,recovery_state)
            reactive_retries=0
        except Exception as e:
            if recovery.is_prompt_too_long_error(e) and reactive_retries<MAX_REACTIVE_RETRIES:
                reactive_retries+=1
                print('[reactive compact]')
                messages[:]=reactive_compact(messages) #保存完整对话记录，保留最近五条消息，返回压缩后的消息
                skip_micro_rounds=3
                continue
            raise


        message=response.choices[0].message
        messages.append(message.model_dump()) #添加模型回复到上下文

        # 输出被截断：升级到加大 max_tokens 并提示续写
        if response.choices[0].finish_reason=='length' and not recovery_state.has_escalated:
            recovery_state.has_escalated=True
            print('  [length] escalating max_tokens and requesting continuation')
            messages.append({'role':'user','content':recovery.CONTINUATION_PROMPT})
            continue


        if response.choices[0].finish_reason != "tool_calls":
            force=trigger_hooks('Stop',messages)
            if force:
                messages.append({'role':'user','content':force})
                continue
            if message.content:
                print(message.content)
            write_transcript(messages)   # 正常结束也落盘，便于复盘
            # extract_memories(pre_compress)：从「压缩前快照」提炼记忆（压缩会丢细节，快照保证不漏）。
            # 提炼对象：user(用户偏好) / feedback(引导性反馈) / project(项目事实) / reference(外部参考)。
            # ⚠️ 提醒：只负责落盘到 .cache/memories/*.md，不会反馈回当前 Loop ——
            #    本会话的新记忆要等下一次会话 load_memories 才会被召回。
            extract_memories(pre_compress)   # 从压缩前快照提炼记忆，压缩不丢信息
            # consolidate_memories()：仅当记忆文件数 ≥ CONSOLIDATE_THRESHOLD(10) 时触发，
            #    合并重复/过期记忆；LLM 返回空数组时会中止，防止删光所有记忆。
            consolidate_memories()
            return message.content or "No output"
            

        # compact 工具：在 for 循环内按顺序处理——compact 之前的 tool_call 先正常执行，
        # 遇到 compact 时压缩历史并重开一轮。
        tool_messages = []
        compacted = False
        used_todo = False
        for tool_call in message.tool_calls:
            if tool_call.function.name == 'todo_write':
                used_todo = True
            if tool_call.function.name == 'compact':
                # compact_history 会替换整个上下文，原 assistant 的 tool_calls 被删除，
                # 之前已执行的 tool 结果不能以 role=tool 保留（会变成 orphan），
                # 改为文本附到新上下文里，避免执行结果丢失。
                messages[:] = compact_history(messages)
                skip_micro_rounds=3
                note = '[Compacted] Conversation history has been summarized.'
                if tool_messages:
                    done = '\n'.join(f"- {str(tm['content'])[:1000]}" for tm in tool_messages)
                    note += '\n\nTool results executed before compaction:\n' + done
                messages.append({'role': 'user', 'content': note})
                compacted = True
                break

            _, _, tool_message = exec_tool_call(tool_call)   # hook 权限 → 后台/直接执行 → 兜底
            tool_messages.append(tool_message)

        if compacted: #压缩后工具丢失，需要重新执行
            continue

        notifications=collect_background_results()
        if notifications:
            for notification in notifications:
                # 后台任务完成通知：作为 user 消息注入（孤儿 role=tool 会触发 API 400）
                tool_messages.append(
                    {
                        'role':'user',
                        'content':notification,
                        
                    }
                )
            print(f"  \033[32m[inject] {len(notifications)} background "
                  f"notification(s)\033[0m")

        messages.extend(tool_messages)
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        context=update_context(context,messages) # 更新上下文，包含工具、工作目录、记忆、技能等
        system=get_system_prompt(context) #更新系统提示，包含身份、工具、工作目录、记忆、技能、规则等


def run_agent_turn_locked():
    """持锁状态下执行一轮对话（供 queue_processor_loop 调用）：
    消费 cron 队列 → 构造 user 消息 → agent_loop。"""
    fired = consume_cron_queue()
    if not fired:
        return
    prompts = [f'[Scheduled] {job.prompt}' for job in fired]
    print(f"  \033[35m[cron turn] {len(fired)} scheduled job(s)\033[0m")
    try:
        agent_loop([{'role': 'user', 'content': '\n'.join(prompts)}])
    except KeyboardInterrupt:
        print('\n[会话已中断]')


if __name__ == '__main__':
    # 启动 cron 调度线程（生产队列）+ 队列处理器（推送执行）；测试直接调 agent_loop 不受影响
    load_durable_jobs()
    threading.Thread(target=cron_scheduler_loop, daemon=True).start()
    threading.Thread(target=queue_processor_loop,
                     args=(run_agent_turn_locked,), daemon=True).start()

    def _run_session(msgs):
        try:
            agent_loop(msgs)
        except KeyboardInterrupt:
            print('\n[会话已中断]')

    # 主会话持 agent_lock：队列处理器只会在主会话结束后才推送 cron 任务
    with agent_lock:
        _run_session([{'role': 'user', 'content': 'Create 3 tasks on the board, then spawn alice and bob. Watch them auto-claim and work.'}])

    # 保持进程存活直到 cron 任务全部处理完（无任务立即退出）；Ctrl+C 退出
    try:
        wait_for_cron_idle()
    except KeyboardInterrupt:
        print('\n[会话已中断]')