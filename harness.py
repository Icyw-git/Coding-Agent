import os 
from dotenv import load_dotenv
import openai
import json
import time
from decimal import Decimal, InvalidOperation
import sys
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
from mcp_bridge import register_mcp_tools
from ui_events import bus, PermissionGate
import renderer   # 副作用：attach 默认 AnsiRenderer（渲染后端由事件流驱动）
import hooks   # 副作用：注册 6 个 hook 回调（权限/日志/上下文/总结）

permission_gate = PermissionGate(bus)   # 权限确认挂起（P1）：Textual 模式由渲染层弹窗应答



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


def serialize_session(messages:list) -> str:
    """把现有会话消息序列化为可读 Markdown，不改变原始 messages。"""
    lines = ['# Harness session', '']
    for index, message in enumerate(messages or [], 1):
        role = str(message.get('role', 'unknown')).title()
        lines.append(f'## {index}. {role}')
        content = message.get('content')
        if content is None:
            content = ''
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        lines.append(content)
        tool_calls = message.get('tool_calls') or []
        for call in tool_calls:
            function = call.get('function', {}) if isinstance(call, dict) else {}
            name = function.get('name', 'tool')
            arguments = function.get('arguments', '{}')
            lines.extend(['', f'**Tool call:** `{name}`', '```json', str(arguments), '```'])
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def export_session(messages:list, path=None):
    """将当前会话导出为 Markdown，返回实际写入路径。"""
    if path is None:
        path = TRANSCRIPT_DIR / f'session_export_{int(time.time())}.md'
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_session(messages), encoding='utf-8')
    return path

# ---------- 重试配置 ----------
MAX_REACTIVE_RETRIES=1


def get_system_prompt(context:dict)->str: #获取上下文，如果上下文没有改变，直接返回缓存的系统提示，否则重新组装系统提示
    global _last_context_key,_last_prompt
    key=json.dumps(context,sort_keys=True,ensure_ascii=False,default=str)
    if key==_last_context_key and _last_prompt:
        bus.emit('debug', msg="  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    _last_context_key=key
    _last_prompt=assemble_system_prompt(context)

    loaded=['identity','tools','workspace','rules']
    if context.get('memories'):
        loaded.append('memory')
    if context.get('skills'):
        loaded.append('skills')
    bus.emit('debug', msg=f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
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
        bus.emit('debug', msg="[auto compact]")
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
    started_at = time.monotonic()
    background_started = False
    bus.emit('activity', state='tool', detail=name)
    bus.emit('tool_start', name=name, args=args)   # 工具实时可见（渲染层执行中即显示）
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
                background_started = True
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
    output_text = str(output)
    failed = output_text.lstrip().lower().startswith('error:')
    denied = output_text == 'Permission denied by user'
    warning = any(line.lstrip().lower().startswith(('warning:', 'warn:'))
                  for line in output_text.splitlines())
    outcome = ('denied' if denied else 'failed' if failed else
               'queued' if background_started else 'warning' if warning else 'completed')
    with _METRICS_LOCK:
        _SESSION_METRICS['tools'] += 1
        _SESSION_METRICS['tool_outcomes'][outcome] = (
            _SESSION_METRICS['tool_outcomes'].get(outcome, 0) + 1)
    bus.emit('tool_end', name=name, args=args, output_preview=output_text[:2000],
             output_len=len(output_text), ok=not (failed or denied),
             outcome=outcome,
             background=background_started,
             duration_ms=round((time.monotonic() - started_at) * 1000))
    bus.emit('activity', state='thinking', detail='Reviewing tool result')
    return name, args, {"role": "tool", "tool_call_id": tool_call.id, "content": output}

# 当前会话累计指标（供 summary_hook / statusline 使用）
# tokens: 真实 usage.total_tokens 累计；cost: 按单价估算累计（元）
# tools: 会话内实际执行工具次数；ctx_tokens: 最近一次请求的真实输入 token（usage.prompt_tokens）
_SESSION_METRICS={
    'tokens': 0,
    'prompt_tokens': 0,
    'completion_tokens': 0,
    'cached_tokens': 0,
    'reasoning_tokens': 0,
    'cost': Decimal('0'),
    'tools': 0,
    'ctx_tokens': 0,
    'ctx_source': 'estimated',
    'cost_source': 'estimated',
    'model': '',
    'calls': 0,
    'recent_usage': [],
    'tool_outcomes': {'completed': 0, 'warning': 0, 'failed': 0,
                      'denied': 0, 'queued': 0},
    'ctx_peak_tokens': 0,
    'rounds': 0,
}
_USAGE_LEDGER=[]
_METRICS_LOCK=threading.RLock()

# 价格单位为 CNY / 1M tokens。可通过 LLM_PRICING_JSON 覆盖，避免把价格散落在代码中。
_MODEL_COSTS={
    'deepseek-v4-flash': {
        'input': Decimal('0.50'), 'output': Decimal('1.01'),
        'cached_input': None, 'reasoning_output': None,
        'context_window': 131072, 'currency': 'CNY', 'version': 'default',
    },
    'deepseek-chat': {
        'input': Decimal('1.01'), 'output': Decimal('2.02'),
        'cached_input': None, 'reasoning_output': None,
        'context_window': 65536, 'currency': 'CNY', 'version': 'default',
    },
    'deepseek-reasoner': {
        'input': Decimal('2.02'), 'output': Decimal('7.92'),
        'cached_input': None, 'reasoning_output': Decimal('7.92'),
        'context_window': 65536, 'currency': 'CNY', 'version': 'default',
    },
}


def _load_model_costs():
    """读取可选的 JSON 价格表；非法配置回退内置价格并保持可启动。"""
    raw = os.getenv('LLM_PRICING_JSON', '').strip()
    if not raw:
        return _MODEL_COSTS
    try:
        configured = json.loads(raw)
        if not isinstance(configured, dict):
            return _MODEL_COSTS
        merged = dict(_MODEL_COSTS)
        for model, spec in configured.items():
            if not isinstance(spec, dict):
                continue
            base = dict(merged.get(str(model).lower(), {}))
            for key, value in spec.items():
                if key in ('input', 'output', 'cached_input', 'reasoning_output'):
                    base[key] = None if value is None else Decimal(str(value))
                else:
                    base[key] = value
            merged[str(model).lower()] = base
        return merged
    except (ValueError, InvalidOperation, TypeError):
        return _MODEL_COSTS


_MODEL_COSTS=_load_model_costs()


def _model_spec(model: str):
    return _MODEL_COSTS.get(str(model or '').lower(), {})


def _context_window(model: str) -> int:
    spec = _model_spec(model)
    configured = os.getenv('LLM_CONTEXT_WINDOW', '').strip()
    if configured:
        try:
            return max(1, int(configured))
        except ValueError:
            pass
    model_key = str(model or '').lower()
    return int(spec.get('context_window') or MODEL_WINDOWS.get(model_key, 65536))


def _estimate_cost(prompt_tokens:int, completion_tokens:int, model:str) -> Decimal:
    """按当前价格表估算成本；返回 Decimal，未知模型返回 0。"""
    spec = _model_spec(model)
    if not spec:
        return Decimal('0')
    prompt = Decimal(max(0, int(prompt_tokens)))
    completion = Decimal(max(0, int(completion_tokens)))
    return (prompt * spec.get('input', Decimal('0')) +
            completion * spec.get('output', Decimal('0'))) / Decimal(1000000)


def _usage_value(obj, name: str, default=0) -> int:
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default) if obj is not None else default
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return default


def _usage_snapshot(usage):
    """兼容 OpenAI/DeepSeek usage 以及 *_details 扩展字段。"""
    prompt = _usage_value(usage, 'prompt_tokens', _usage_value(usage, 'input_tokens'))
    completion = _usage_value(usage, 'completion_tokens', _usage_value(usage, 'output_tokens'))
    total = _usage_value(usage, 'total_tokens', prompt + completion)
    if isinstance(usage, dict):
        prompt_details = usage.get('prompt_tokens_details')
        completion_details = usage.get('completion_tokens_details')
    else:
        prompt_details = getattr(usage, 'prompt_tokens_details', None)
        completion_details = getattr(usage, 'completion_tokens_details', None)
    cached = _usage_value(prompt_details, 'cached_tokens')
    reasoning = _usage_value(completion_details, 'reasoning_tokens')
    return {
        'prompt_tokens': prompt, 'completion_tokens': completion,
        'total_tokens': total, 'cached_tokens': cached,
        'reasoning_tokens': reasoning,
    }


def _cost_from_usage(snapshot: dict, model: str) -> Decimal:
    spec = _model_spec(model)
    if not spec:
        return Decimal('0')
    prompt = Decimal(snapshot['prompt_tokens'])
    cached = Decimal(min(snapshot['cached_tokens'], snapshot['prompt_tokens']))
    completion = Decimal(snapshot['completion_tokens'])
    reasoning = Decimal(min(snapshot['reasoning_tokens'], snapshot['completion_tokens']))
    cached_rate = spec.get('cached_input')
    input_cost = ((cached * cached_rate) + ((prompt - cached) * spec.get('input', Decimal('0')))
                  if cached_rate is not None else prompt * spec.get('input', Decimal('0')))
    reasoning_rate = spec.get('reasoning_output')
    if reasoning_rate is not None:
        output_cost = ((completion - reasoning) * spec.get('output', Decimal('0')) +
                       reasoning * reasoning_rate)
    else:
        output_cost = completion * spec.get('output', Decimal('0'))
    return (input_cost + output_cost) / Decimal(1000000)


def _record_usage(model: str, snapshot: dict, source: str = 'confirmed') -> dict:
    cost = _cost_from_usage(snapshot, model) if source == 'confirmed' else _estimate_cost(
        snapshot['prompt_tokens'], snapshot['completion_tokens'], model)
    entry = dict(snapshot, model=model, cost=cost, source=source,
                 currency=_model_spec(model).get('currency', 'CNY'),
                 pricing_version=_model_spec(model).get('version', 'unknown'))
    with _METRICS_LOCK:
        _USAGE_LEDGER.append(entry)
        _SESSION_METRICS['calls'] += 1
        _SESSION_METRICS['tokens'] += snapshot['total_tokens']
        _SESSION_METRICS['prompt_tokens'] += snapshot['prompt_tokens']
        _SESSION_METRICS['completion_tokens'] += snapshot['completion_tokens']
        _SESSION_METRICS['cached_tokens'] += snapshot['cached_tokens']
        _SESSION_METRICS['reasoning_tokens'] += snapshot['reasoning_tokens']
        _SESSION_METRICS['cost'] += cost
        _SESSION_METRICS['ctx_tokens'] = snapshot['prompt_tokens']
        _SESSION_METRICS['ctx_peak_tokens'] = max(
            _SESSION_METRICS['ctx_peak_tokens'], snapshot['prompt_tokens'])
        _SESSION_METRICS['ctx_source'] = source
        sources = {item['source'] for item in _USAGE_LEDGER}
        _SESSION_METRICS['cost_source'] = (
            'confirmed' if sources == {'confirmed'} else
            'estimated' if sources == {'estimated'} else 'mixed')
        _SESSION_METRICS['model'] = model
        _SESSION_METRICS['recent_usage'] = [
            {
                'call': item.get('call', 0), 'model': item['model'],
                'prompt_tokens': item['prompt_tokens'],
                'completion_tokens': item['completion_tokens'],
                'cost': str(item['cost']), 'source': item['source'],
            }
            for item in (_USAGE_LEDGER[-3:])
        ]
    return entry

def _build_streamed_message(content, tool_acc):
    """把流式收集的 content + tool_calls（分段拼接）重建为兼容 OpenAI message 的对象。
    提供 model_dump() 且 tool_calls 元素用 SimpleNamespace（后续循环按 .function.name 访问）。"""
    from types import SimpleNamespace
    tool_calls = None
    if tool_acc:
        tool_calls = [
            SimpleNamespace(
                id=acc['id'] or f'call_{i}',
                function=SimpleNamespace(name=acc['name'], arguments=acc['arguments']),
            )
            for i, acc in sorted(tool_acc.items())
        ]

    class _StreamedMessage:
        def __init__(self):
            self.content = content
            self.tool_calls = tool_calls

        def model_dump(self):
            d = {'role': 'assistant', 'content': self.content}
            if self.tool_calls:
                d['tool_calls'] = [
                    {'id': tc.id, 'type': 'function',
                     'function': {'name': tc.function.name, 'arguments': tc.function.arguments}}
                    for tc in self.tool_calls
                ]
            return d

    return _StreamedMessage()


def update_panel_status(messages:list, *, model: str = None,
                        ctx_tokens: int = None, ctx_source: str = None) -> None:
    """发送会话状态指标事件（模型 / 工具数 / 上下文占用 / token / 成本）。

    渲染与否由订阅的渲染后端决定（默认 AnsiRenderer 不渲染 status）。"""
    model = model or os.getenv('LLM_MODEL_ID', '')
    if ctx_tokens is None:
        ctx_tokens = estimate_size(messages) if messages else 0
        ctx_source = ctx_source or 'estimated'
    else:
        ctx_source = ctx_source or 'confirmed'
    window = _context_window(model)
    ctx_pct = min(100, int(100 * ctx_tokens / max(1, window)))
    bus.emit('status',
             model=model,
             tools=_SESSION_METRICS.get('tools', 0),
             ctx_pct=ctx_pct,
             ctx_tokens=ctx_tokens,
             ctx_window=window,
             ctx_source=ctx_source,
             tokens=str(_SESSION_METRICS.get('tokens', 0)),
             prompt_tokens=_SESSION_METRICS.get('prompt_tokens', 0),
             completion_tokens=_SESSION_METRICS.get('completion_tokens', 0),
             cached_tokens=_SESSION_METRICS.get('cached_tokens', 0),
             reasoning_tokens=_SESSION_METRICS.get('reasoning_tokens', 0),
             cost=str(_SESSION_METRICS.get('cost', Decimal('0'))),
             cost_source=_SESSION_METRICS.get('cost_source', 'estimated'),
             calls=_SESSION_METRICS.get('calls', 0),
             recent_calls=list(_SESSION_METRICS.get('recent_usage', [])))


def _session_summary() -> dict:
    with _METRICS_LOCK:
        return {
            'rounds': _SESSION_METRICS.get('rounds', 0),
            'calls': _SESSION_METRICS.get('calls', 0),
            'tools': _SESSION_METRICS.get('tools', 0),
            'tool_outcomes': dict(_SESSION_METRICS.get('tool_outcomes', {})),
            'ctx_peak_tokens': _SESSION_METRICS.get('ctx_peak_tokens', 0),
            'tokens': _SESSION_METRICS.get('tokens', 0),
            'cost': str(_SESSION_METRICS.get('cost', Decimal('0'))),
            'cost_source': _SESSION_METRICS.get('cost_source', 'estimated'),
        }

def agent_loop(messages:list):
    bus.emit('activity', state='preparing', detail='Preparing session')
    context=update_context({},messages)
    recovery_state=recovery.RecoveryState()
    if messages:
        trigger_hooks('UserPromptSubmit',messages[-1]['content'])
    system=get_system_prompt(context)
    reactive_retries=0 #响应重试次数
    skip_micro_rounds=0 #跳过 micro_compact 轮数
    rounds_since_todo=0 #距离上次 todo 提醒的轮数
    update_panel_status(messages)
    # 记忆加载：整个 Loop 只在此处加载一次（LLM 从 .cache/memories 目录筛选相关记忆）。
    # ⚠️ 提醒：循环中途不会再次刷新 —— 本轮新产生的信息不会进入 memories_content；
    #    要等会话结束 extract_memories 落盘后，下一次会话的 load_memories 才能召回。
    bus.emit('activity', state='preparing', detail='Loading relevant memory')
    bus.emit('status', phase='thinking')
    try:
        memories_content=load_memories(messages)   # 相关记忆块（空串 = 无相关记忆）
    finally:
        bus.emit('status', phase='idle')

    while True:
        _SESSION_METRICS['rounds'] += 1
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
            bus.emit('debug', msg=f"  \033[35m[inject cron] {job.prompt[:50]}\033[0m")

        




        # 记忆注入 + system 前置（副本不污染 messages；SDK 不支持 system= 关键字参数）
        request_messages = inject_memories(messages, memories_content, system)

        request_model = recovery_state.current_model or os.getenv("LLM_MODEL_ID")

        def _create():
            max_tokens=(recovery.ESCALATED_MAX_TOKENS
                        if recovery_state.has_escalated
                        else recovery.DEFAULT_MAX_TOKENS)
            return client.chat.completions.create(
                model=request_model,
                messages=request_messages,
                tools=TOOLS,
                temperature=0.7,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},   # 流式末尾 chunk 携带 usage
            )

        bus.emit('activity', state='thinking', detail='Requesting model response')
        bus.emit('status', phase='thinking')
        try:
            stream = recovery.with_retry(_create, recovery_state)
            reactive_retries = 0
        except Exception as e:
            if recovery.is_prompt_too_long_error(e) and reactive_retries<MAX_REACTIVE_RETRIES:
                reactive_retries+=1
                bus.emit('debug', msg='[reactive compact]')
                messages[:]=reactive_compact(messages) #保存完整对话记录，保留最近五条消息，返回压缩后的消息
                skip_micro_rounds=3
                continue
            raise
        finally:
            bus.emit('status', phase='idle')

        # ---- 流式收集：逐 delta 发事件（真流式，边生成边显示），同时重建完整消息 ----
        content_parts = []
        tool_acc = {}          # tool_call.index -> {id, name, arguments}（流式分段拼接）
        finish_reason = None
        usage = None
        mid = f'msg_{int(time.time() * 1000)}'
        bus.emit('activity', state='generating', detail='Streaming response')
        bus.emit('assistant_start', mid=mid)
        try:
            for chunk in stream:
                if getattr(chunk, 'usage', None) is not None:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason
                if delta.content:
                    content_parts.append(delta.content)
                    bus.emit('assistant_delta', mid=mid, delta=delta.content)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        acc = tool_acc.setdefault(tc.index, {'id': '', 'name': '', 'arguments': ''})
                        if tc.id:
                            acc['id'] = tc.id
                        if tc.function:
                            if tc.function.name:
                                acc['name'] += tc.function.name
                            if tc.function.arguments:
                                acc['arguments'] += tc.function.arguments
        finally:
            bus.emit('assistant_end', mid=mid)

        message = _build_streamed_message(''.join(content_parts) or None, tool_acc)

        # usage 存在时以 provider 账单口径为准；兼容端点不返回 usage 时明确标记 estimated。
        if usage is not None:
            usage_snapshot = _usage_snapshot(usage)
            usage_source = 'confirmed'
        else:
            provisional_message = _build_streamed_message(
                ''.join(content_parts) or None, tool_acc)
            usage_snapshot = {
                'prompt_tokens': estimate_size(request_messages),
                'completion_tokens': estimate_size([provisional_message.model_dump()]),
                'total_tokens': 0, 'cached_tokens': 0, 'reasoning_tokens': 0,
            }
            usage_snapshot['total_tokens'] = (usage_snapshot['prompt_tokens'] +
                                              usage_snapshot['completion_tokens'])
            usage_source = 'estimated'
        _record_usage(request_model, usage_snapshot, usage_source)
        update_panel_status(messages, model=request_model,
                            ctx_tokens=usage_snapshot['prompt_tokens'],
                            ctx_source=usage_source)
        messages.append(message.model_dump()) #添加模型回复到上下文

        # 输出被截断：升级到加大 max_tokens 并提示续写
        if finish_reason=='length' and not recovery_state.has_escalated:
            recovery_state.has_escalated=True
            print('  [length] escalating max_tokens and requesting continuation')
            messages.append({'role':'user','content':recovery.CONTINUATION_PROMPT})
            continue


        if finish_reason != "tool_calls":
            force=trigger_hooks('Stop',messages)
            if force:
                messages.append({'role':'user','content':force})
                continue
            # 最终回复：流式 delta 已实时渲染，此处只负责收尾（落盘 / 提炼记忆）
            bus.emit('activity', state='saving', detail='Saving transcript and memory')
            bus.emit('session_end', summary=_session_summary())
            write_transcript(messages)   # 正常结束也落盘，便于复盘
            # extract_memories(pre_compress)：从「压缩前快照」提炼记忆（压缩会丢细节，快照保证不漏）。
            # 提炼对象：user(用户偏好) / feedback(引导性反馈) / project(项目事实) / reference(外部参考)。
            # ⚠️ 提醒：只负责落盘到 .cache/memories/*.md，不会反馈回当前 Loop ——
            #    本会话的新记忆要等下一次会话 load_memories 才会被召回。
            bus.emit('status', phase='thinking')
            try:
                extract_memories(pre_compress)   # 从压缩前快照提炼记忆，压缩不丢信息
            finally:
                bus.emit('status', phase='idle')
            # consolidate_memories()：仅当记忆文件数 ≥ CONSOLIDATE_THRESHOLD(10) 时触发，
            #    合并重复/过期记忆；LLM 返回空数组时会中止，防止删光所有记忆。
            consolidate_memories()
            bus.emit('activity', state='complete', detail='Response complete')
            return message.content or "No output"
            

        # compact 工具：在 for 循环内按顺序处理——compact 之前的 tool_call 先正常执行，
        # 遇到 compact 时压缩历史并重开一轮。
        # 批量模式：整轮工具执行（输出 + 状态行刷新）合并为一次 TUI 重绘，减少闪烁。
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
        update_panel_status(messages)   # 每轮工具后刷新状态事件（渲染后端可消费）


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

    # 接入 MCP：连接配置的 server，工具动态注册进 tool_registry + TOOLS
    TOOLS.extend(register_mcp_tools(tool_registry))

    def _run_session(msgs):
        try:
            agent_loop(msgs)
        except KeyboardInterrupt:
            print('\n[会话已中断]')

    main_msgs = [{'role': 'user', 'content': '总结一下这个项目的情况'}]

    # UI 后端选择：HARNESS_UI=textual 且 stdout 为 TTY → Textual 全屏 TUI
    # （agent_loop 在 App 内后台线程执行，权限走事件弹窗）；否则 ANSI 普通打印（默认）。
    if os.getenv('HARNESS_UI', 'ansi') == 'textual':
        if not sys.stdout.isatty():
            print('[ui] HARNESS_UI=textual 但 stdout 非 TTY，回退 ANSI。'
                  '在 Windows Terminal 里运行可启用全屏 TUI。')
        else:
            import ui_textual
            # 多轮对话框：不预设初始消息，由底部输入框驱动（msgs 由用户逐轮填充）
            ui_textual.run([])
    else:
        # 主会话持 agent_lock：队列处理器只会在主会话结束后才推送 cron 任务
        with agent_lock:
            _run_session(main_msgs)

    # 保持进程存活直到 cron 任务全部处理完（无任务立即退出）；Ctrl+C 退出
    try:
        wait_for_cron_idle()
    except KeyboardInterrupt:
        print('\n[会话已中断]')
