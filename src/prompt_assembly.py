"""系统提示组装与上下文采集。

提示词分段（身份 / 工具 / 工作区 / 技能 / 规则 / 项目说明）属于请求前缀：
只在内容变化时重建，由 `_prompt_cache` 按 context 指纹缓存。本模块不持有状态，
client、工具表、技能与工作区都通过 `_h()` 回读 harness，测试里 monkeypatch
harness 上的入口仍然生效。
"""
from __future__ import annotations

import json
import os
from contextvars import ContextVar

import project_context


def _h():
    import harness
    return harness


def project_prompt_section(workspace=None) -> str:
    """读取工作区的 AGENTS.md/CLAUDE.md 并渲染成 system prompt 段落。"""
    instructions = project_context.load_project_instructions(
        workspace or _h().get_workdir())
    return project_context.format_project_instructions(instructions)


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
    ),
    'rules_sub':(
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
    'skills_usage':(
        'Skills available:\n{catalog}\n'
        'Use load_skill to load a skill. E.g. load_skill("file-summarizer")'
    ),
}


def assemble_system_prompt(context: dict) -> str:
    """组装 system prompt：身份、工具、工作区、技能、规则、项目说明。"""
    workspace = context.get('workspace', _h().get_workdir())
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
            print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return cached[1]
    prompt = assemble_system_prompt(context)
    _prompt_cache.set((key, prompt))
    loaded = ['identity', 'tools', 'workspace', 'rules']
    if context.get('skills'):
        loaded.append('skills')
    if context.get('project_instructions'):
        loaded.append('project')
    if os.getenv('HARNESS_DEBUG') == '1':
        print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return prompt


def update_context(context: dict, messages: list) -> dict:
    """收集随回合变化的前缀内容：工具表、工作区、技能目录与项目说明。"""
    # ⚠ 记忆不再进 system prompt：housekeeping 每轮更新 MEMORY_INDEX，若嵌在
    # system 里，请求前缀每轮逐字变化，供应商前缀缓存（KV 复用）全失效。
    # 记忆由 inject_memories 作为请求末尾的独立 system 消息注入。
    return {
        'enabled_tools': list(_h().tool_registry.keys()),
        'workspace': str(_h().get_workdir()),
        'skills': _h().list_skills(),
        'project_instructions': project_prompt_section(),
    }


def build_system() -> str:
    """返回完整的 system prompt（外部调用 / 调试用）。"""
    return assemble_system_prompt(update_context({}, []))


def build_sub_system() -> str:
    """子代理的 system prompt：身份 + 规则 + 项目说明 + 技能目录。"""
    sections = [
        PROMPT_SECTIONS['identity_sub'].format(workdir=_h().get_workdir()),
        PROMPT_SECTIONS['rules_sub'].format(workdir=_h().get_workdir()),
    ]
    project = project_prompt_section()
    if project:
        sections.append(project)
    sections.append(
        PROMPT_SECTIONS['skills_usage'].format(catalog=_h().list_skills()))
    return '\n\n'.join(sections)
