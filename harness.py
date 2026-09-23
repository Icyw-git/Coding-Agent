import argparse
import os 
from dotenv import load_dotenv
import openai
import json
import time
import threading
import sys
from pathlib import Path
from typing import Optional, List

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from background_task import (should_run_background, start_background_task, collect_background_results,
                             background_lock, background_tasks)
import recovery
from task_system import create_task, list_tasks, get_task, claim_task, complete_task
from cron_scheduler import (schedule_job, cancel_job, consume_cron_queue,
                            cron_lock, scheduled_jobs, cron_scheduler_loop,
                            load_durable_jobs, agent_lock,
                            queue_processor_loop, wait_for_cron_idle)
from agent_teams import spawn_teammate_thread, BUS, active_teammates
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
from session_store import SessionEventStore, SessionReplayError
from checkpoints import CheckpointManager, CheckpointError
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


def validate_runtime_config(environ=None) -> None:
    """Fail early with an actionable error when required model settings are absent."""
    environ = os.environ if environ is None else environ
    missing = [name for name in ('LLM_API_KEY', 'LLM_MODEL_ID') if not environ.get(name)]
    if missing:
        raise RuntimeError(
            'Missing required configuration: ' + ', '.join(missing)
            + '. Set them in .env before starting Harness Agent.'
        )


def list_session_paths(limit: int = 20) -> list[Path]:
    """Return the most recently modified persisted session logs."""
    if not SESSIONS_DIR.is_dir():
        return []
    sessions = SESSIONS_DIR.glob('session_*.jsonl')
    return sorted(sessions, key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def resolve_session_path(session_id: str) -> Path:
    """Resolve a session ID without accepting arbitrary filesystem paths."""
    if not session_id or not session_id.replace('-', '').replace('_', '').isalnum():
        raise ValueError('Invalid session ID')
    path = SESSIONS_DIR / f'session_{session_id}.jsonl'
    if not path.is_file():
        raise FileNotFoundError(f'Session {session_id} not found')
    return path


def format_session_list(limit: int = 20) -> str:
    rows = list_session_paths(limit)
    if not rows:
        return '  (no saved sessions)'
    lines = []
    generated = False
    for path in rows:
        store = SessionEventStore.open(path)
        title = store.session_title()
        if title == '(untitled session)' and not generated:
            history = store.rebuild_openai_history()
            if _has_session_title_source(history):
                generated = True
                title = generate_session_title(history)
                if title != '(untitled session)':
                    store.append_session_title(title)
        lines.append(f'  {store.session_id}  {title}  events={store.last_seq}  '
                     f'updated={time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))}')
    return '\n'.join(lines)


def generate_session_title(messages: list[dict]) -> str:
    """Ask the configured model for a compact session topic, with a local fallback."""
    exchanges = []
    has_tool_context = False
    for message in messages:
        role = message.get('role')
        content = message.get('content')
        if role in ('user', 'assistant') and isinstance(content, str) and content.strip():
            exchanges.append(f"{role}: {content.strip()[:600]}")
        if role == 'assistant':
            for call in message.get('tool_calls') or []:
                function = call.get('function') or {}
                name = function.get('name', '')
                arguments = function.get('arguments', '')
                if name:
                    exchanges.append(f"tool call: {name} {str(arguments)[:300]}")
                    has_tool_context = True
    fallback = next((
        ' '.join(line.split()).strip()[:60]
        for message in messages
        if message.get('role') == 'user' and isinstance(message.get('content'), str)
        for line in message['content'].splitlines()
        if _is_session_title_candidate(line)
    ), None)
    if fallback is None:
        fallback = next((
            ' '.join(line.split()).strip()[:60]
            for message in messages
            if message.get('role') == 'assistant' and isinstance(message.get('content'), str)
            for line in message['content'].splitlines()
            if _is_session_title_candidate(line)
        ), '(untitled session)')
    if fallback == '(untitled session)' and not has_tool_context:
        return fallback
    if not exchanges:
        return '(untitled session)'
    try:
        response = client.chat.completions.create(
            model=os.getenv('LLM_MODEL_ID'),
            messages=[
                {'role': 'system', 'content': (
                    '为这段 coding agent 会话概括一个简短主题。只输出标题，使用用户原语言，'
                    '控制在 20 个汉字或 8 个英文词以内。忽略“继续、好的、go、do it”等确认语，'
                    '根据实际任务概括；如果记录没有提供足够信息，只输出 (untitled session)，不要猜测。'
                )},
                {'role': 'user', 'content': '\n'.join(exchanges[-8:])},
            ],
            temperature=0,
            max_tokens=40,
        )
        title = (response.choices[0].message.content or '').strip().strip('"“”\'')
        if title:
            return ' '.join(title.split())[:60]
    except Exception as exc:
        print(f'  [session title] using local fallback: {exc}')
    return fallback


def _is_session_title_candidate(line: str) -> bool:
    text = ' '.join(line.split()).strip()
    normalized = text.casefold()
    if len(text) < 12 or text.startswith(('[', '<', '- out:')):
        return False
    if normalized.startswith((
        'tool results executed before compaction:',
        'done after compact',
        'final answer',
        'recovered',
        '[scheduled]',
    )):
        return False
    words = normalized.split()
    return not (len(words) >= 4 and len(set(words)) == 1)


def _has_session_title_source(messages: list[dict]) -> bool:
    if any(
        message.get('role') == 'assistant' and message.get('tool_calls')
        for message in messages
    ):
        return True
    return any(
        _is_session_title_candidate(line)
        for message in messages
        if message.get('role') in ('user', 'assistant')
        and isinstance(message.get('content'), str)
        for line in message['content'].splitlines()
    )


def format_status(session_store) -> str:
    """Summarize local runtime state for the interactive REPL."""
    manager = get_checkpoint_manager(session_store)
    with cron_lock:
        cron_count = len(scheduled_jobs)
    with background_lock:
        background_count = len(background_tasks)
    return (
        f'  session={session_store.session_id} events={session_store.last_seq}\n'
        f'  model={os.getenv("LLM_MODEL_ID", "(not configured)")} workspace={WORKDIR}\n'
        f'  checkpoints={len(manager.list(limit=10_000))} tasks={len(list_tasks())} '
        f'cron={cron_count} background={background_count} teammates={len(active_teammates)}'
    )


def run_doctor() -> int:
    """Report startup prerequisites without contacting the model provider."""
    checks = [
        ('LLM_API_KEY', bool(os.getenv('LLM_API_KEY'))),
        ('LLM_MODEL_ID', bool(os.getenv('LLM_MODEL_ID'))),
        ('workspace', WORKDIR.is_dir()),
        ('git repository', (WORKDIR / '.git').exists()),
    ]
    for directory in (SESSIONS_DIR, CHECKPOINT_DIR, MEMORY_DIR, TOOL_RESULTS_DIR):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            checks.append((f'writable {directory.relative_to(WORKDIR)}', True))
        except OSError:
            checks.append((f'writable {directory}', False))
    for label, passed in checks:
        print(f'[{"ok" if passed else "missing"}] {label}')
    return 0 if all(passed for _, passed in checks) else 1

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
DESTRUCTIVE=["rm ","del ","rmdir ","rd ","remove-item ","erase ",
             "clear-content ","> /etc/","chmod 777"]

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
SESSIONS_DIR=WORKDIR/'.cache'/'sessions'

# ---------- Checkpoint / Rewind 配置 ----------
CHECKPOINT_DIR=WORKDIR/'.cache'/'checkpoints'
CHECKPOINT_MAX_NODES=20          # 保留最近 N 个节点（由 /checkpoint-gc 触发回收）
CHECKPOINT_KEEP_LABELED=True     # 带 label 的显式节点不被 GC
CHECKPOINT_CAPTURE_FILES=True    # 关闭后退化为纯会话回溯
CHECKPOINT_IGNORE=['.cache','.git','.worktrees','.mailbox','__pycache__','.pytest_cache']
CHECKPOINT_SECRETS=['.env','*.pem','*_rsa']
CHECKPOINT_MANAGER=None          # 当前活跃 manager；tools 的 write/edit 通过 _h() 读取它抓 pre-image

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


def build_system()->str: #暴露给外部调用，返回系统提示
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

def pre_compact_messages(messages, skip_micro_rounds=0, on_compact=None): #压缩逻辑，每轮开头的压缩预处理：budget → snip → micro →（仍超限）LLM 摘要。返回 (压缩后的 messages, 新的 skip_micro_rounds)。
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
        if on_compact:
            on_compact(messages, 'auto')
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
    else: #判断是否为后台任务，若为后台任务则启动后台任务，否则直接执行工具
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

def _record_compaction(session_store, compacted_messages, strategy):
    summary = next(
        (str(message.get('content', '')) for message in compacted_messages
         if message.get('role') == 'user' and str(message.get('content', '')).startswith('[')),
        '',
    )
    for prefix in ('[Compacted]\n\n', '[Reactive compact]\n\n'):
        if summary.startswith(prefix):
            summary = summary[len(prefix):]
            break
    session_store.append_compaction(summary, strategy)


# ============================================================
# Checkpoint / Rewind（设计见 CHECKPOINT_DESIGN.md）
# ============================================================

def get_checkpoint_manager(session_store):
    """按 session 复用 manager，并把它暴露给 tools.run_write/run_edit 抓 pre-image。"""
    global CHECKPOINT_MANAGER
    manager=CHECKPOINT_MANAGER
    if manager is None or manager.session_id!=session_store.session_id:
        manager=CheckpointManager(
            CHECKPOINT_DIR,WORKDIR,session_store.session_id,
            max_nodes=CHECKPOINT_MAX_NODES,
            keep_labeled=CHECKPOINT_KEEP_LABELED,
            capture_files=CHECKPOINT_CAPTURE_FILES,
            ignore=CHECKPOINT_IGNORE,
            secrets=CHECKPOINT_SECRETS,
        )
        CHECKPOINT_MANAGER=manager
    return manager


def _safe_checkpoint(action,*args,**kwargs):
    """快照/回收失败不能打断主循环，只告警。"""
    try:
        return action(*args,**kwargs)
    except Exception as e:
        print(f"  \033[33m[checkpoint] skipped: {e}\033[0m")
        return None


def _snapshot_path(manager, cp_id:str)->Path:
    return manager.snapshot_path(cp_id)


def format_checkpoint_list(manager)->str:
    rows=manager.list()
    if not rows:
        return '  (no checkpoints)'
    head=manager.head()
    lines=[]
    for row in rows:
        mark='*' if row['cp_id']==head else ' '
        label=f" {row['label']}" if row.get('label') else ''
        lines.append(f"  {mark} {row['cp_id']}  {row['status']:<6} {row['trigger']:<6} "
                     f"files={len(row['files'])}{label}")
    return '\n'.join(lines)


def format_restore_report(plan:dict)->str:
    lines=[f"  \033[36m[rewind] {plan['cp_id']}"
           f"{(' ' + plan['label']) if plan.get('label') else ''} "
           f"(chain={len(plan['chain'])}, scope={plan.get('scope','all')})\033[0m"]
    for entry in plan['actions']:
        lines.append(f"    {entry['action']:<9} {entry['path']}  ({entry['reason']})")
    for title,items in (('conflicts',plan['conflicts']),
                        ('unverified (节点未 seal，无法校验)',plan['unverified']),
                        ('unrestorable by bash/external',plan['drift'])):
        if items:
            lines.append(f"    \033[33m{title}: {', '.join(items[:10])}\033[0m")
    side=plan['side_effects']
    lines.append(f"    \033[90mnot rolled back: memories={side['memories']} "
                 f"crons={side['crons_file']} tasks={side['tasks']} "
                 f"worktrees={side['worktrees']} background={side['background_tasks']}\033[0m")
    if not plan['ok']:
        lines.append(f"    \033[31m{plan['reason']}\033[0m")
    return '\n'.join(lines)


def rewind_to(messages:list,session_store,manager,ref:str,*,force=False,scope='all',report=True)->bool:
    """把 messages 就地替换成回溯后的历史，并把 rewind 事件写进 session。

    report=True 时打印完整计划+结果（直接调用时用）；命令层已经打印过计划，传 report=False，
    这里只输出「实际做了什么」，避免同一份报告刷两遍。
    """
    try:
        cp_id=manager.resolve(ref)
    except CheckpointError as e:
        print(f"  \033[31m[rewind] {e}\033[0m")
        return False
    if not _snapshot_path(manager, cp_id).exists():
        print(f"  \033[31m[rewind] snapshot missing for {cp_id}\033[0m")
        return False

    plan=manager.restore(cp_id,force=force,scope=scope)
    if report:
        print(format_restore_report(plan))
    if not plan['ok']:
        print(f"  \033[33m[rewind] 未执行任何改动；确认可覆盖后重试 "
              f"/rewind {cp_id} --force\033[0m")
        return False

    session_store.append_rewind(cp_id,scope=scope,
                                 snapshot_path=str(_snapshot_path(manager, cp_id)),
                                restored_files=plan['applied'])
    messages[:]=session_store.rebuild_openai_history()
    applied=', '.join(plan['applied']) if plan['applied'] else '(no file change)'
    print(f"  \033[36m[rewind] {cp_id} applied: {applied}\033[0m")
    print(f"  \033[36m[rewind] session history restored to {cp_id}\033[0m")
    return True


def _confirm(prompt:str)->bool:
    try:
        return input(f"{prompt} (y/n) ").strip().lower() in ('y','yes')
    except (EOFError,KeyboardInterrupt):
        return False


def handle_command(line:str,messages:list,session_store)->None:
    """REPL 里的 /命令；回溯需要调用方已持有 agent_lock。"""
    parts=line.split()
    command=parts[0].lower()
    manager=get_checkpoint_manager(session_store)

    if command=='/checkpoints':
        print(format_checkpoint_list(manager))

    elif command == '/status':
        print(format_status(session_store))

    elif command == '/sessions':
        print(format_session_list())

    elif command=='/checkpoint':
        label=' '.join(parts[1:])
        cp_id=_safe_checkpoint(manager.begin_turn,messages,session_store,label=label,trigger='manual')
        if cp_id:
            print(f"  \033[36m[checkpoint] {cp_id}{(' ' + label) if label else ''}\033[0m")

    elif command=='/rewind':
        ref=next((p for p in parts[1:] if not p.startswith('--')),'last')
        force='--force' in parts
        scope='session' if '--session' in parts else 'all'
        try:
            cp_id=manager.resolve(ref)
        except CheckpointError as e:
            print(f"  \033[31m[rewind] {e}\033[0m")
            return
        if not _snapshot_path(manager, cp_id).exists():
            print(f"  \033[31m[rewind] snapshot missing for {cp_id}\033[0m")
            return

        # 先预览（plan_restore 不落盘），再确认执行
        preview=_safe_checkpoint(manager.plan_restore,cp_id)
        if preview is None:
            return
        preview['scope']=scope
        print(format_restore_report(preview))
        if preview['conflicts'] and not force:
            print(f"  \033[33m[rewind] 未执行任何改动；确认可覆盖后重试 "
                  f"/rewind {cp_id} --force\033[0m")
            return
        if '--yes' not in parts and not _confirm('Apply rewind?'):
            print('  [rewind] cancelled')
            return
        _safe_checkpoint(rewind_to,messages,session_store,manager,cp_id,
                         force=force,scope=scope,report=False)

    elif command=='/checkpoint-gc':
        removed=manager.gc()
        print(f"  \033[36m[checkpoint] gc removed {len(removed)} node(s)\033[0m")

    else:
        print(f"  unknown command: {command}")


def agent_loop(messages:list, session_store=None):
    """Run one OpenAI session, recording an append-only event stream for recovery."""
    if session_store is None:
        session_store = SessionEventStore(SESSIONS_DIR)
        for initial_message in messages:
            session_store.append_message(initial_message)
        print(f'[session log:{session_store.path}]')
    context=update_context({},messages) #循环前更新上下文，包含所有对话记录
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
    checkpoint_manager=get_checkpoint_manager(session_store) # 节点按 session 复用；tools 的 write/edit 通过它抓 pre-image

    while True:
        # 每轮开头留一个可回溯节点：快照必须在 pre_compact 重新绑定 messages 之前取
        _safe_checkpoint(checkpoint_manager.begin_turn,messages,session_store)

        pre_compress=[m if isinstance(m,dict) else {'role':m.get('role',''),'content':str(m.get('content',''))} for m in messages] #压缩前的快照：纯净对话（不含 cron/提醒），供会话结束提炼记忆
        

        messages, skip_micro_rounds = pre_compact_messages(
            messages, skip_micro_rounds,
            on_compact=lambda compacted, strategy: _record_compaction(session_store, compacted, strategy),
        ) #压缩预处理流程

        if rounds_since_todo >=8 and messages:
            reminder = {
                'role':'user',
                'content':"<reminder>Update your todos.</reminder>",
            }
            messages.append(reminder)
            session_store.append_message(reminder)
            rounds_since_todo=0

        fired=consume_cron_queue() #消费 cron 任务列
        for job in fired:
            cron_message = {
                'role':'user',
                'content':f'[Scheduled] {job.prompt}'
            }
            messages.append(cron_message)
            session_store.append_message(cron_message)
            print(f"  \033[35m[inject cron] {job.prompt[:50]}\033[0m")

        




        # 记忆注入 + system 前置（副本不污染 messages；SDK 不支持 system= 关键字参数）
        request_messages = inject_memories(messages, memories_content, system)

        def _create(): #创建模型回复函数
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

        try: #错误恢复逻辑
            response=recovery.with_retry(_create,recovery_state)
            reactive_retries=0
        except Exception as e:
            if recovery.is_prompt_too_long_error(e) and reactive_retries<MAX_REACTIVE_RETRIES:
                reactive_retries+=1
                print('[reactive compact]')
                messages[:]=reactive_compact(messages) #保存完整对话记录，保留最近五条消息，返回压缩后的消息
                _record_compaction(session_store, messages, 'reactive')
                skip_micro_rounds=3
                continue
            raise


        message=response.choices[0].message
        assistant_message = message.model_dump()
        messages.append(assistant_message) #添加模型回复到上下文
        session_store.append_message(assistant_message)

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
            session_store.append('turn_completed')
            if not session_store.saved_session_title():
                title = generate_session_title(messages)
                if title != '(untitled session)':
                    session_store.append_session_title(title)
            _safe_checkpoint(checkpoint_manager.seal)   # 固化本节点：记录改动文件写入后的 hash
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
            if tool_call.function.name == 'checkpoint':
                # 当前节点在本轮请求前已创建，快照是完整 OpenAI 历史。只给它加标签，不能
                # 在 assistant tool_calls 与对应 tool 结果之间新建快照，否则 rewind 后历史无法重放。
                # 与 compact 一样不进 tool_registry —— 它是 loop 行为，不是普通工具。
                try:
                    label=str(json.loads(tool_call.function.arguments or '{}').get('label',''))
                except json.JSONDecodeError:
                    label=''
                cp_id=_safe_checkpoint(checkpoint_manager.label_active,label,session_store,
                                       trigger='manual')
                tool_messages.append({
                    'role':'tool','tool_call_id':tool_call.id,
                    'content':(f'Checkpoint created: {cp_id}' if cp_id
                               else 'Checkpoint failed; continue without one.'),
                })
                session_store.append_message(tool_messages[-1])
                continue
            if tool_call.function.name == 'compact':
                # compact_history 会替换整个上下文，原 assistant 的 tool_calls 被删除，
                # 之前已执行的 tool 结果不能以 role=tool 保留（会变成 orphan），
                # 改为文本附到新上下文里，避免执行结果丢失。
                messages[:] = compact_history(messages)
                _record_compaction(session_store, messages, 'tool')
                skip_micro_rounds=3
                note = '[Compacted] Conversation history has been summarized.'
                if tool_messages:
                    done = '\n'.join(f"- {str(tm['content'])[:1000]}" for tm in tool_messages)
                    note += '\n\nTool results executed before compaction:\n' + done
                messages.append({'role': 'user', 'content': note})
                session_store.append_message(messages[-1])
                compacted = True
                break

            session_store.append_tool_started(tool_call)
            _, _, tool_message = exec_tool_call(tool_call)   # hook 权限 → 后台/直接执行 → 兜底
            tool_messages.append(tool_message)
            session_store.append_message(tool_message)

        if compacted: #压缩后工具丢失，需要重新执行
            continue

        notifications=collect_background_results() #收集后台任务结果
        if notifications:
            for notification in notifications:
                # 后台任务完成通知：作为 user 消息注入（孤儿 role=tool 会触发 API 400）
                tool_messages.append(
                    {
                        'role':'user',
                        'content':notification,
                        
                    }
                )
                session_store.append_message(tool_messages[-1])
            print(f"  \033[32m[inject] {len(notifications)} background "
                  f"notification(s)\033[0m")

        messages.extend(tool_messages)
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        context=update_context(context,messages) # 更新上下文，包含工具、工作目录、记忆、技能等
        system=get_system_prompt(context) #更新系统提示，包含身份、工具、工作目录、记忆、技能、规则等


def resume_agent_loop(session_path: Path):
    """Resume a persisted OpenAI session without automatically repeating pending tools."""
    session_store = SessionEventStore.open(session_path)
    messages = session_store.rebuild_openai_history()
    recovery_message = session_store.recovery_message()
    if recovery_message:
        messages.append(recovery_message)
        session_store.append_message(recovery_message)
    return agent_loop(messages, session_store=session_store)


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


def run_repl(initial_messages:list, session_store=None):
    """最小交互循环：普通输入走 agent_loop，/ 开头的走 checkpoint 命令（见 handle_command）。

    agent_loop 每轮结束就返回，canonical history 由 session_store 维护，
    因此每轮结束后用 rebuild_openai_history() 刷新本地 messages —— 这样 /rewind 与多轮对话共用同一份历史。
    """
    messages=[dict(m) for m in initial_messages]
    if session_store is None:
        session_store=SessionEventStore(SESSIONS_DIR)
        for message in messages:
            session_store.append_message(message)
    print(f'[session log:{session_store.path}]')
    print('  \033[90mcommands: /status /sessions /resume <id> /checkpoints /checkpoint [label] '
          '/rewind <id|last|head> [--force] [--session] [--yes] /checkpoint-gc\033[0m')

    while True:
        try:
            line=input('\n> ').strip()
        except (EOFError,KeyboardInterrupt):
            print('\n[会话已结束]')
            break
        if not line:
            continue
        if line.startswith('/'):
            if line.split()[0].lower() in ('/exit','/quit'):
                break
            if line.split()[0].lower() == '/resume':
                parts = line.split(maxsplit=1)
                if len(parts) != 2:
                    print('  usage: /resume <session_id>')
                    continue
                try:
                    session_store = SessionEventStore.open(resolve_session_path(parts[1]))
                    messages = session_store.rebuild_openai_history()
                    print(f'  [session] resumed {session_store.session_id}')
                except (FileNotFoundError, ValueError, SessionReplayError) as exc:
                    print(f'  [session] {exc}')
                continue
            handle_command(line,messages,session_store)
            continue
        messages.append({'role':'user','content':line})
        session_store.append_message(messages[-1])
        try:
            agent_loop(messages,session_store=session_store)
        except KeyboardInterrupt:
            print('\n[会话已中断]')
        messages[:]=session_store.rebuild_openai_history()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Harness Agent')
    parser.add_argument('--doctor', action='store_true', help='check local configuration and exit')
    parser.add_argument('--resume', metavar='SESSION_ID', help='resume a saved session')
    args = parser.parse_args()

    if args.doctor:
        raise SystemExit(run_doctor())
    try:
        validate_runtime_config()
    except RuntimeError as exc:
        raise SystemExit(f'[config] {exc}') from exc

    # 启动 cron 调度线程（生产队列）+ 队列处理器（推送执行）；测试直接调 agent_loop 不受影响
    load_durable_jobs()
    threading.Thread(target=cron_scheduler_loop, daemon=True).start()
    threading.Thread(target=queue_processor_loop,
                     args=(run_agent_turn_locked,), daemon=True).start()

    # 接入 MCP：连接配置的 server，工具动态注册进 tool_registry + TOOLS
    TOOLS.extend(register_mcp_tools(tool_registry))

    # 主会话持 agent_lock：队列处理器只会在主会话结束后才推送 cron 任务；
    # /rewind 等命令也在锁内执行，避免与 cron turn 并发改 messages。
    with agent_lock:
        try:
            if args.resume:
                store = SessionEventStore.open(resolve_session_path(args.resume))
                run_repl(store.rebuild_openai_history(), session_store=store)
            else:
                run_repl([])
        except (FileNotFoundError, ValueError, SessionReplayError) as exc:
            print(f'[session] {exc}')
        except KeyboardInterrupt:
            print('\n[会话已中断]')

    # 保持进程存活直到 cron 任务全部处理完（无任务立即退出）；Ctrl+C 退出
    try:
        wait_for_cron_idle()
    except KeyboardInterrupt:
        print('\n[会话已中断]')
