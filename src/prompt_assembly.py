"""系统提示组装与上下文采集。

提示词分段（身份 / 工具 / 工作区 / 技能 / 规则 / 项目说明）属于请求前缀：
只在内容变化时重建，由 ``_prompt_cache`` 按 context 指纹缓存。

本模块不读取入口的全局状态：工作区、工具名、技能目录与项目说明都由调用方
注入，harness / 网关 / SDK 各自负责采集。这样它可以独立导入和测试，也不会
把 CLI 的全局变量带进其他入口；`_prompt_cache` 是模块自身的执行上下文缓存。
"""

from __future__ import annotations

import json
import os
import sys
from contextvars import ContextVar
from datetime import datetime

import project_context


def project_prompt_section(workspace) -> str:
    """读取工作区的 AGENTS.md/CLAUDE.md 并渲染成 system prompt 段落。"""
    instructions = project_context.load_project_instructions(workspace)
    return project_context.format_project_instructions(instructions)


PROMPT_SECTIONS = {
    'identity_main': (
        'You are a coding agent working on Windows at {workdir}. '
        'Use the bash tool to execute shell commands (Windows cmd syntax works, e.g. dir instead of ls). '
    ),
    'identity_sub': (
        'You are a focused sub-agent working at {workdir} on behalf of a parent agent. '
        'Complete the task you are given and return a concise final answer to the parent agent. '
    ),
    'rules_main': (
        'Rules: '
        '(1) If the user denies or rejects an operation, STOP permanently. Do NOT retry it and do NOT find alternative '
        'commands or workarounds (del/rm/Remove-Item/powershell/etc.) to achieve the same result. '
        '(2) Prefer safe, non-destructive commands for inspection (dir, type, findstr). '
        'To read file contents prefer the read tool (supports limit/offset); avoid PowerShell Get-Content, '
        'which mis-decodes UTF-8 files on this system. For multi-file or structured edits prefer '
        'apply_patch (*** Begin Patch ... *** End Patch); it applies atomically and rolls back on failure. '
        'To read a public web page use web_fetch instead of curl; it enforces outbound approval and '
        'blocks private addresses. '
        'Long-running commands (install/build/test) are '
        'dispatched to the background automatically: use background_output (wait=true to block) to read '
        'their output and kill_background to cancel. Only modify files when asked. '
        '(3) Parallel tool calls: when several independent operations have no dependency between them '
        '(e.g. reading multiple files, searching with glob/grep, running several read-only commands like '
        'git status / git log / ls), issue ALL of them as multiple tool calls in the SAME turn instead of '
        'one per turn — they will run in parallel. Calls whose input depends on an earlier result '
        'must stay sequential. '
        '(4) In chat clients the user usually does not type slash commands. When they ask to be '
        'reminded, to repeat something periodically, to recall an earlier conversation, to rename '
        'this session, or to revoke approvals, use the corresponding tool (schedule_cron / '
        'list_crons / cancel_cron / find_sessions / rename_session / revoke_permissions) instead of '
        'telling them to type a command. Scheduled results are delivered back to the chat '
        'automatically; scheduled jobs are read-only unless the user explicitly authorized side '
        'effects. '
    ),
    'rules_sub': (
        'Rules: '
        '(1) Stay strictly within the workspace; use only relative paths under {workdir}. '
        '(2) Prefer safe commands (dir, type, findstr) and the read/glob tools for inspection. '
        '(3) Independent tool calls with no dependency between them (multiple reads, read-only '
        'commands) should be issued together in one turn as parallel tool calls. '
        '(4) If the parent task asks you to write/edit/delete files, still ask yourself whether it is '
        'destructive; if it is, do NOT execute it silently — report back that user confirmation is required. '
        '(5) If you hit an error (tool fails, path missing), do not loop on the same command. Try one '
        'reasonable alternative, then report the situation honestly to the parent. '
        '(6) Keep the final answer short and structured: what you did, what you found, any issues. '
        'Do not re-plan the parent task and do not spawn further sub-agents. '
    ),
    'skills_usage': (
        'Skills available:\n{catalog}\nUse load_skill to load a skill. E.g. load_skill("file-summarizer")'
    ),
}


def environment_message(now: datetime | None = None) -> str:
    """当前本地时间（尾部 system 消息）。

    不能放进 system prompt 前缀：时间每分钟变化，会让供应商的前缀缓存整段失效。
    作为每轮请求末尾的独立 system 消息注入，主前缀仍逐字稳定。
    """
    local = now or datetime.now().astimezone()
    if local.tzinfo is None:
        local = local.astimezone()
    weekday = '一二三四五六日'[local.weekday()]
    zone = local.tzname() or local.strftime('UTC%z')
    return ('<environment>'
            f'Current local time: {local.strftime("%Y-%m-%d %H:%M:%S")} '
            f'({zone}, weekday {weekday}). '
            'Resolve relative dates such as "tomorrow" or "next Monday" against this '
            'time when scheduling cron jobs.'
            '</environment>')


def assemble_system_prompt(context: dict) -> str:
    """组装 system prompt：身份、工具、工作区、技能、规则、项目说明。"""
    workspace = context['workspace']
    sections = [PROMPT_SECTIONS['identity_main'].format(workdir=workspace)]
    tools = ','.join(context.get('enabled_tools', []))
    if tools:
        sections.append(f'Available tools:{tools}.')
    sections.append(f'Working directory:{workspace}.')
    skills = context.get('skills', '')
    if skills:
        sections.append(PROMPT_SECTIONS['skills_usage'].format(catalog=skills))
    sections.append(PROMPT_SECTIONS['rules_main'])
    project = context.get('project_instructions', '')
    if project:
        sections.append(project)
    return '\n\n'.join(sections)


_prompt_cache = ContextVar('prompt_cache', default=None)


def get_system_prompt(context: dict) -> str:
    """context 未变时复用缓存的 system prompt，否则重新组装。"""
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    cached = _prompt_cache.get()
    if cached is not None and cached[0] == key:
        if os.getenv('HARNESS_DEBUG') == '1':
            print('  \033[90m[cache hit] system prompt unchanged\033[0m', file=sys.stderr)
        return cached[1]
    prompt = assemble_system_prompt(context)
    _prompt_cache.set((key, prompt))
    loaded = ['identity', 'tools', 'workspace', 'rules']
    if context.get('skills'):
        loaded.append('skills')
    if context.get('project_instructions'):
        loaded.append('project')
    if os.getenv('HARNESS_DEBUG') == '1':
        print(f'  \033[32m[assembled] sections: {", ".join(loaded)}\033[0m', file=sys.stderr)
    return prompt


def collect_context(*, workdir, tool_names, skills) -> dict:
    """收集随回合变化的前缀内容；运行时依赖由入口层注入，不在这里读取全局。"""
    # ⚠ 记忆不再进 system prompt：housekeeping 每轮更新 MEMORY_INDEX，若嵌在
    # system 里，请求前缀每轮逐字变化，供应商前缀缓存（KV 复用）全失效。
    # 记忆由 inject_memories 作为请求末尾的独立 system 消息注入。
    return {
        'enabled_tools': list(tool_names),
        'workspace': str(workdir),
        'skills': skills,
        'project_instructions': project_prompt_section(workdir),
    }


def build_system(*, workdir, tool_names, skills) -> str:
    """返回完整的 system prompt（外部调用 / 调试用）。"""
    return assemble_system_prompt(collect_context(workdir=workdir, tool_names=tool_names, skills=skills))


def build_sub_system(*, workdir, skills) -> str:
    """子代理的 system prompt：身份 + 规则 + 项目说明 + 技能目录。"""
    sections = [
        PROMPT_SECTIONS['identity_sub'].format(workdir=workdir),
        PROMPT_SECTIONS['rules_sub'].format(workdir=workdir),
    ]
    project = project_prompt_section(workdir)
    if project:
        sections.append(project)
    sections.append(PROMPT_SECTIONS['skills_usage'].format(catalog=skills))
    return '\n\n'.join(sections)
