import argparse
import os 
from dotenv import load_dotenv
import openai
import json
import time
import threading
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
import sys
from types import SimpleNamespace
from pathlib import Path
from typing import Optional, List

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from workspace_context import current_workspace, use_workspace
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
from mcp_bridge import register_mcp_tools, shutdown_mcp
from session_store import SessionEventStore, SessionReplayError
from agent_runtime import AgentRuntime
from checkpoints import CheckpointManager, CheckpointError
from cli_commands import parse_command, command_error, format_command_help
import hooks   # 副作用：注册 6 个 hook 回调（权限/日志/上下文/总结）
from perf import segment as perf_segment, record as perf_record, reset as perf_reset



load_dotenv()
WORKDIR = Path(os.getcwd()).resolve()


def get_workdir() -> Path:
    return current_workspace(WORKDIR)


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


def list_session_paths(limit: int | None = 20) -> list[Path]:
    """Return logs ordered by conversational activity, not filesystem mtime."""
    return [SESSIONS_DIR / f"session_{row['id']}.jsonl"
            for row in _session_catalog()][:limit]


_session_summary_cache = {}


def _session_catalog():
    """Reuse summaries until the log changes; timestamps only invalidate cache."""
    paths = list(SESSIONS_DIR.glob('session_*.jsonl'))
    live = {path.resolve() for path in paths}
    for path in list(_session_summary_cache):
        if path not in live:
            del _session_summary_cache[path]
    rows = []
    for path in paths:
        key = path.resolve()
        try:
            info = path.stat()
            signature = (info.st_mtime_ns, info.st_size, info.st_ino)
            cached = _session_summary_cache.get(key)
            if cached is None or cached[0] != signature:
                summary = SessionEventStore.open(path).session_summary()
                _session_summary_cache[key] = (signature, summary)
            rows.append(dict(_session_summary_cache[key][1]))
        except (OSError, SessionReplayError) as exc:
            _session_summary_cache.pop(key, None)
            print(f'  [session] 无法读取 {path.stem}: {exc}')
    return sorted(rows, key=lambda row: (row['updated'], row['id']), reverse=True)


def resolve_session_path(session_id: str) -> Path:
    """Resolve a session ID without accepting arbitrary filesystem paths."""
    if not session_id or not session_id.replace('-', '').replace('_', '').isalnum():
        raise ValueError('Invalid session ID')
    path = SESSIONS_DIR / f'session_{session_id}.jsonl'
    if not path.is_file():
        raise FileNotFoundError(f'Session {session_id} not found')
    return path


def set_workspace(path: str) -> Path:
    """Switch the agent workspace without changing the process-wide cwd."""
    global WORKDIR, SKILLS_DIR
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = WORKDIR / candidate
    candidate = candidate.resolve()
    if not candidate.is_dir():
        raise ValueError(f'Workspace does not exist: {candidate}')
    WORKDIR = candidate
    SKILLS_DIR = WORKDIR / 'skills'
    return WORKDIR


def session_rows(limit: int | None = 100, *, include_empty: bool = False,
                 workspace: Path | None = None, query: str = '') -> list[dict]:
    rows = _session_catalog()
    from cli_input import filter_session_rows
    return filter_session_rows(rows, workspace=workspace, include_empty=include_empty,
                               query=query)[:limit]


def format_session_list(limit: int = 20) -> str:
    from cli_input import session_label

    rows = session_rows(max(100, limit), include_empty=False, workspace=WORKDIR)[:limit]
    if not rows:
        rows = session_rows(max(100, limit), include_empty=False)[:limit]
    if not rows:
        return '  (no saved sessions)'
    return '\n\n'.join(session_label(row) for row in rows)


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
    activity = [f'{count} 个{label}' for label, count in (
        ('任务', len(list_tasks())), ('定时任务', cron_count),
        ('后台任务', background_count), ('协作成员', len(active_teammates))) if count]
    return (
        f"  会话：{session_store.session_summary()['title']}\n"
        f'  工作区：{WORKDIR}\n'
        f'  模型：{os.getenv("LLM_MODEL_ID", "未配置")}\n'
        f'  快照：{len(manager.list(limit=10_000))} 个\n'
        f'  任务概况：{" · ".join(activity) if activity else "暂无任务"}'
    )


def format_workspace_diff(workspace=None, path='', *, max_chars=12000) -> str:
    """Return a bounded, read-only view of the workspace changes."""
    workspace = Path(workspace or get_workdir())
    try:
        scope = path.strip() or '.'
        result = subprocess.run(
            ['git', 'diff', 'HEAD', '--no-ext-diff', '--stat', '--', scope],
            cwd=workspace, capture_output=True, text=True, timeout=15,
        )
        status = subprocess.run(
            ['git', 'status', '--short', '--untracked-files=all'],
            cwd=workspace, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f'  无法读取工作区改动：{exc}'
    if result.returncode != 0 or status.returncode != 0:
        return '  当前工作区不是 Git 仓库，无法显示 diff。'
    lines = []
    if status.stdout.strip():
        lines.append('  文件状态：')
        lines.extend(f'    {line}' for line in status.stdout.splitlines())
    if result.stdout.strip():
        lines.append('  已跟踪文件统计：')
        lines.extend(f'    {line}' for line in result.stdout.splitlines())
    if not lines:
        return '  工作区干净，没有未提交改动。'
    return ('\n'.join(lines))[:max_chars]


def format_pending_approvals() -> str:
    """Show actionable approval context without exposing internal request IDs."""
    pending = hooks.GATE.pending()
    if not pending:
        return '  没有待审批操作。'
    lines = ['  待审批操作：']
    for index, request in enumerate(pending, 1):
        reason = str(request.reason or '').strip() or '未提供原因'
        tool = str(request.tool or '未知操作')
        lines.append(f'    {index}. {tool} · {reason}')
    lines.append('  请使用飞书 /approve 或对应审批卡片处理。')
    return '\n'.join(lines)


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
        workdir=context.get('workspace',get_workdir())
    ))
    tools=','.join(context.get('enabled_tools',[]))
    if tools:
        sections.append(f'Available tools:{tools}.')
    sections.append(f'Working directory:{context.get("workspace",get_workdir())}.')

    memories=context.get('memories','')
    if memories:
        sections.append(f'Relevant memories:\n{memories}')
    skills=context.get('skills','')
    if skills:
        sections.append(PROMPT_SECTIONS['skills_usage'].format(catalog=skills))
    sections.append(PROMPT_SECTIONS['rules_main'])
    return '\n\n'.join(sections)

_prompt_cache = ContextVar('prompt_cache', default=None)

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
_checkpoint_manager = ContextVar('checkpoint_manager', default=None)

# ---------- 重试配置 ----------
MAX_REACTIVE_RETRIES=1


def get_system_prompt(context:dict)->str: #获取上下文，如果上下文没有改变，直接返回缓存的系统提示，否则重新组装系统提示
    key=json.dumps(context,sort_keys=True,ensure_ascii=False,default=str)
    cached = _prompt_cache.get()
    if cached is not None and cached[0] == key:
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return cached[1]
    prompt = assemble_system_prompt(context)
    _prompt_cache.set((key, prompt))

    loaded=['identity','tools','workspace','rules']
    if context.get('memories'):
        loaded.append('memory')
    if context.get('skills'):
        loaded.append('skills')
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return prompt

def update_context(context:dict,messages:list)->dict: # 更新上下文，包含工具、工作目录、记忆、技能等
    memories=''
    if MEMORY_INDEX.exists():
        content=MEMORY_INDEX.read_text(encoding='utf-8',errors='replace').strip()
        if content:
            memories=content
    skills=list_skills()
    return {
        'enabled_tools':list(tool_registry.keys()),
        'workspace':str(get_workdir()),
        'memories':memories,
        'skills':skills,
    }


def build_system()->str: #暴露给外部调用，返回系统提示
    return assemble_system_prompt(update_context({},[]))


def build_sub_system()->str:
    return (
        PROMPT_SECTIONS['identity_sub'].format(workdir=get_workdir()) +
        '\n\n' +
        PROMPT_SECTIONS['rules_sub'].format(workdir=get_workdir()) +
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
    try:
        args = json.loads(tool_call.function.arguments)
        if not isinstance(args, dict):
            raise ValueError('tool arguments must be a JSON object')
    except (TypeError, ValueError) as exc:
        return name, {}, {'role': 'tool', 'tool_call_id': tool_call.id,
                          'content': f'Error: invalid tool arguments: {exc}'}
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
    """Reuse the manager within this turn, without sharing it across sessions."""
    manager = active_checkpoint_manager()
    if (manager is None or manager.session_id != session_store.session_id
            or manager.workspace != get_workdir()):
        manager=CheckpointManager(
            CHECKPOINT_DIR,get_workdir(),session_store.session_id,
            max_nodes=CHECKPOINT_MAX_NODES,
            keep_labeled=CHECKPOINT_KEEP_LABELED,
            capture_files=CHECKPOINT_CAPTURE_FILES,
            ignore=CHECKPOINT_IGNORE,
            secrets=CHECKPOINT_SECRETS,
        )
    return manager


def active_checkpoint_manager():
    return _checkpoint_manager.get()


@contextmanager
def _isolated_checkpoint_context():
    token = _checkpoint_manager.set(None)
    try:
        with use_workspace(get_workdir()):
            yield
    finally:
        try:
            manager = active_checkpoint_manager()
            if manager is not None:
                _safe_checkpoint(manager.seal)
        finally:
            _checkpoint_manager.reset(token)


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
        return '  暂无快照。用 /checkpoint 创建，或在执行任务时自动生成。'
    head=manager.head()
    lines=[]
    for row in rows:
        mark='*' if row['cp_id']==head else ' '
        label = row.get('label') or ('手动快照' if row['trigger'] == 'manual' else '任务快照')
        status = ' · 尚未完成' if row['status'] == 'open' else ''
        current = ' · 当前' if mark == '*' else ''
        lines.append(f"  {label}{current}{status} · {len(row['files'])} 个文件\n"
                     f"    /rewind {row['cp_id']}")
    return '\n'.join(lines)


def format_restore_report(plan:dict)->str:
    scope = '仅会话' if plan.get('scope') == 'session' else '会话与文件'
    lines = [f"  回溯预览：{plan.get('label') or plan['cp_id']} · {scope}"]
    for entry in plan['actions']:
        lines.append(f"    {entry['action']:<9} {entry['path']}  ({entry['reason']})")
    for title,items in (('存在冲突',plan['conflicts']),
                        ('快照尚未完成，无法校验',plan['unverified']),
                        ('命令或外部改动，无法自动恢复',plan['drift'])):
        if items:
            remaining = f'（另有 {len(items) - 10} 项）' if len(items) > 10 else ''
            lines.append(f"    \033[33m{title}: {', '.join(items[:10])}{remaining}\033[0m")
    lines.append('    注意：记忆、定时任务、任务进程和 worktree 不随回溯撤销。')
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

    try:
        plan=manager.restore(cp_id,force=force,scope=scope)
    except CheckpointError as exc:
        print(f'  [rewind] {exc}')
        return False
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
    applied = ', '.join(plan['applied']) if plan['applied'] else '无文件改动'
    print(f"  已恢复会话 · {applied}")
    return True


def _confirm(prompt:str)->bool:
    try:
        return input(f"{prompt} (y/n) ").strip().lower() in ('y','yes')
    except (EOFError,KeyboardInterrupt):
        return False


def handle_command(line:str,messages:list,session_store)->None:
    """REPL 里的 /命令；回溯需要调用方已持有 agent_lock。"""
    command, argument = parse_command(line)
    if command == '/undo':
        command, argument = '/rewind', 'last --yes'
    error = command_error(command, argument)
    if error:
        print(f'  {error}')
        return
    parts = [command, *argument.split()]
    if command == '/help':
        print(format_command_help(argument))
        return
    manager = (get_checkpoint_manager(session_store)
               if command in ('/checkpoints', '/checkpoint', '/rewind', '/checkpoint-gc')
               else None)

    if command=='/checkpoints':
        print(format_checkpoint_list(manager))

    elif command == '/status':
        print(format_status(session_store))

    elif command == '/diff':
        print(format_workspace_diff())

    elif command == '/model':
        print(f'  当前模型：{os.getenv("LLM_MODEL_ID", "未配置")}')
        print(f'  服务地址：{os.getenv("LLM_BASE_URL", "默认地址")}')

    elif command == '/approvals':
        print(format_pending_approvals())

    elif command == '/compact':
        if not messages:
            print('  当前会话没有可压缩的内容。')
            return
        before = len(messages)
        try:
            compacted = compact_history(messages)
        except Exception as exc:
            print(f'  [compact] 压缩失败，原会话未改变：{exc}')
            return
        messages[:] = compacted
        _record_compaction(session_store, messages, 'manual')
        session_store.append_message(messages[-1])
        print(f'  已压缩会话上下文：{before} 条消息 → {len(messages)} 条。完整记录已保存。')

    elif command == '/sessions':
        print(format_session_list())

    elif command in ('/archive', '/delete'):
        from cli_input import choose_cleanup_sessions
        from session_cleanup import SessionArchive, cleanup_candidates

        # Finish this process's background writes before moving a session log.
        wait_for_housekeeping()
        with _housekeeping_lock:
            if any(thread.is_alive() for thread in _housekeeping_threads):
                print('  会话后台整理尚未结束，请稍后再清理。')
                return
        archive = SessionArchive(SESSIONS_DIR, CHECKPOINT_DIR)
        deleting = command == '/delete'
        restoring = argument == 'restore'
        rows = (archive.list() if restoring or deleting else cleanup_candidates(
            session_rows(limit=None, include_empty=True), session_store.session_id,
            days=int(argument) if argument.isdigit() else 30,
            empty_only=argument == 'empty'))
        if argument == 'all' and not deleting:
            rows = session_rows(limit=None, include_empty=True)
        rows = [row for row in rows if row['id'] != session_store.session_id]
        selected = choose_cleanup_sessions(rows, restore=restoring, delete=deleting)
        selected_rows = [row for row in rows if row['id'] in selected]
        if not selected_rows:
            return
        action = '永久删除' if deleting else '恢复' if restoring else '归档'
        print(f'  将{action}以下会话（含关联快照）：')
        for row in selected_rows:
            print(f"    {row['title']} · {row['workspace'] or '目录未记录'}")
        print('  请确认其他 CLI/API 服务没有正在使用这些会话。')
        if deleting:
            try:
                confirmed = input('  不可恢复。输入“删除”确认，其他输入取消：').strip() == '删除'
            except (EOFError, KeyboardInterrupt):
                confirmed = False
        else:
            confirmed = _confirm(f'确认{action} {len(selected_rows)} 个会话？')
        if not confirmed:
            print('  已取消，未执行改动。')
            return
        completed = 0
        for row in selected_rows:
            try:
                if deleting:
                    archive.delete(row, session_store.session_id)
                elif restoring:
                    archive.restore(row['id'])
                else:
                    archive.archive(row, session_store.session_id)
                completed += 1
            except (OSError, ValueError) as exc:
                print(f"  未处理 {row['title']}：{exc}")
        print(f'  已{action} {completed} 个会话。' + (
            '记录及关联快照不可恢复，项目文件未改动。' if deleting else
            '用 /archive restore 可恢复；项目文件未改动。' if not restoring else '可用 /resume 继续会话。'))

    elif command == '/pwd':
        print(f'  工作区：{WORKDIR}')

    elif command == '/cd':
        if len(parts) < 2:
            print(f'  工作区：{WORKDIR}')
            return
        try:
            path = argument
            if len(path) >= 2 and path[0] == path[-1] and path[0] in ('"', "'"):
                path = path[1:-1]
            print(f'  工作区：{set_workspace(path)}')
            if session_store is not None:
                session_store.record_workspace(WORKDIR)
        except ValueError as exc:
            print(f'  [workspace] {exc}')

    elif command=='/checkpoint':
        label=' '.join(parts[1:])
        cp_id=_safe_checkpoint(manager.begin_turn,messages,session_store,label=label,trigger='manual')
        if cp_id:
            _safe_checkpoint(manager.seal)
            print(f"  已创建快照{('：' + label) if label else ''}。用 /rewind head 回溯。")

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
        preview=_safe_checkpoint(manager.plan_restore,cp_id,scope=scope)
        if preview is None:
            return
        preview['scope']=scope
        print(format_restore_report(preview))
        if preview['conflicts'] and not force:
            print(f"  \033[33m[rewind] 未执行任何改动；确认可覆盖后重试 "
                  f"/rewind {cp_id} --force\033[0m")
            return
        if '--yes' not in parts and not _confirm('确认回溯？'):
            print('  已取消回溯，未执行改动。')
            return
        _safe_checkpoint(rewind_to,messages,session_store,manager,cp_id,
                         force=force,scope=scope,report=False)

    elif command=='/checkpoint-gc':
        removed=manager.gc()
        print(f"  已清理 {len(removed)} 个旧快照。" if removed else '  无需清理快照。')

    else:
        print(f"  unknown command: {command}")


def _post_turn_housekeeping(pre_compress, messages_snapshot, session_store):
    """回合结束后的副通道作业：会话标题生成 + 记忆提炼/合并。

    每项各自要走一次 LLM——放后台线程执行，回复立即返回给调用方，
    不让飞书/SDK 用户多等几秒到几十秒。异常只告警，绝不影响主流程。
    """
    try:
        if not session_store.saved_session_title():
            title = generate_session_title(messages_snapshot)
            if title != '(untitled session)' and not session_store.saved_session_title():
                session_store.append_session_title(title)
    except Exception as exc:
        print(f'  \033[33m[title] skipped: {exc}\033[0m')
    try:
        extract_memories(pre_compress)
        consolidate_memories()
    except Exception as exc:
        print(f'  \033[33m[memory] skipped: {exc}\033[0m')


_housekeeping_threads: list[threading.Thread] = []
_housekeeping_lock = threading.Lock()


def wait_for_housekeeping(timeout: float = 30.0) -> None:
    """等待回合末尾的后台作业全部结束（测试断言/优雅退出用）。"""
    deadline = time.time() + timeout
    for thread in list(_housekeeping_threads):
        thread.join(timeout=max(0.0, deadline - time.time()))


def _stream_completion(create_kwargs, on_delta):
    """Collect a streamed completion while preserving the existing response shape."""
    _t0 = time.perf_counter()
    _ttfb_done = False
    stream = client.chat.completions.create(**create_kwargs, stream=True)
    text_parts = []
    tool_calls = {}
    finish_reason = None
    for chunk in stream:
        choice = chunk.choices[0]
        finish_reason = choice.finish_reason or finish_reason
        delta = choice.delta
        if getattr(delta, 'content', None):
            if not _ttfb_done:   # 模型首个文本块到达；客户端显示延迟另计
                perf_record('llm.ttfb', time.perf_counter() - _t0)
                _ttfb_done = True
            part = delta.content
            text_parts.append(part)
            on_delta(part)
        for call in getattr(delta, 'tool_calls', None) or []:
            current = tool_calls.setdefault(call.index, {
                'id': call.id or '', 'type': call.type or 'function',
                'function': {'name': '', 'arguments': ''},
            })
            if call.id:
                current['id'] = call.id
            function = getattr(call, 'function', None)
            if function is not None:
                current['function']['name'] += function.name or ''
                current['function']['arguments'] += function.arguments or ''
    calls = [SimpleNamespace(
        id=item['id'], type=item['type'],
        function=SimpleNamespace(**item['function']))
        for _, item in sorted(tool_calls.items())]
    message = SimpleNamespace(
        role='assistant', content=''.join(text_parts) or None,
        tool_calls=calls or None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])


@_isolated_checkpoint_context()
def agent_loop(messages:list, session_store=None, on_delta=None):
    """Run one OpenAI session, recording an append-only event stream for recovery."""
    perf_reset()   # 分段计时清零（HARNESS_PERF=1 时生效）
    if session_store is None:
        session_store = SessionEventStore(SESSIONS_DIR)
        for initial_message in messages:
            session_store.append_message(initial_message)
        print(f'[session log:{session_store.path}]')
    session_store.record_workspace(get_workdir())
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
    with perf_segment('mem.load'):
        memories_content=load_memories(messages)   # 相关记忆块（空串 = 无相关记忆）
    checkpoint_manager = get_checkpoint_manager(session_store)
    _checkpoint_manager.set(checkpoint_manager)  # Only this turn's tools capture pre-images.

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
            kwargs = dict(
                model=recovery_state.current_model or os.getenv("LLM_MODEL_ID"),
                messages=request_messages,
                tools=TOOLS,
                temperature=0.7,
                max_tokens=max_tokens,
            )
            if on_delta is None:
                return client.chat.completions.create(**kwargs)
            return _stream_completion(kwargs, on_delta)

        try: #错误恢复逻辑
            with perf_segment('llm.main'):
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
        if hasattr(message, 'model_dump'):
            assistant_message = message.model_dump()
        else:
            assistant_message = {
                'role': 'assistant',
                'content': message.content,
                'tool_calls': [
                    {'id': call.id, 'type': call.type,
                     'function': {'name': call.function.name,
                                  'arguments': call.function.arguments}}
                    for call in (message.tool_calls or [])
                ] or None,
            }
        messages.append(assistant_message) #添加模型回复到上下文
        session_store.append_message(assistant_message)

        # 输出被截断：升级到加大 max_tokens 并提示续写
        if response.choices[0].finish_reason=='length' and not recovery_state.has_escalated:
            recovery_state.has_escalated=True
            print('  [length] escalating max_tokens and requesting continuation')
            completed = SessionEventStore.close_interrupted_tools(messages)
            for tool_result in completed[len(messages):]:
                session_store.append_message(tool_result)
            messages[:] = completed
            continuation = {'role':'user','content':recovery.CONTINUATION_PROMPT}
            messages.append(continuation)
            session_store.append_message(continuation)
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
            _safe_checkpoint(checkpoint_manager.seal)   # 固化本节点：记录改动文件写入后的 hash
            # 标题/记忆提炼移入后台线程（_post_turn_housekeeping）：回复立即返回，
            # 副通道各自的 LLM 调用不再阻塞端到端延迟。快照防后续轮次改写。
            housekeeping = threading.Thread(
                target=_post_turn_housekeeping,
                args=(pre_compress, list(messages), session_store),
                daemon=True,
            )
            with _housekeeping_lock:
                # 只清理「已启动且已死」的旧线程——is_alive() 对未 start 的线程
                # 也返回 False，必须先清理再注册，否则会把本线程误删
                _housekeeping_threads[:] = [th for th in _housekeeping_threads
                                            if th.is_alive()]
                _housekeeping_threads.append(housekeeping)
            housekeeping.start()
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
    messages = session_store.rebuild_openai_history(recover_pending=True)
    recovery_message = session_store.recovery_message()
    if recovery_message:
        messages.append(recovery_message)
        session_store.append_message(recovery_message)
    return agent_loop(messages, session_store=session_store)


_mcp_init_lock = threading.Lock()
_mcp_initialized = False


def initialize_mcp_tools() -> None:
    """Register configured MCP tools once for all adapters in this process."""
    global _mcp_initialized
    with _mcp_init_lock:
        if not _mcp_initialized:
            TOOLS.extend(register_mcp_tools(tool_registry))
            _mcp_initialized = True


def create_runtime(*, initialize_tools: bool = True) -> AgentRuntime:
    """Build the transport-independent runtime used by SDK and API adapters."""
    if initialize_tools:
        initialize_mcp_tools()

    def before_session_change():
        wait_for_housekeeping()
        with _housekeeping_lock:
            if any(thread.is_alive() for thread in _housekeeping_threads):
                raise RuntimeError('会话后台整理尚未结束，请稍后重试')

    return AgentRuntime(
        run_turn=agent_loop,
        session_store_cls=SessionEventStore,
        sessions_dir=SESSIONS_DIR,
        resolve_session_path=resolve_session_path,
        checkpoints_dir=CHECKPOINT_DIR,
        before_session_change=before_session_change,
        workspace=WORKDIR,
    )


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
        session_store.record_workspace(WORKDIR)
        for message in messages:
            session_store.append_message(message)
    else:
        workspace = session_store.session_summary()['workspace']
        if workspace:
            set_workspace(workspace)
    print(f"  会话：{session_store.session_summary()['title']}")
    print(f'  工作区：{WORKDIR}')
    print('  输入消息开始对话；/ 查看命令，/resume 恢复会话，/quit 退出。')

    from cli_input import create_reader, choose_session

    def session_choices():
        for row in session_rows(limit=None):
            yield row['id'], f"{row['title']} · {row['workspace'] or '工作目录未记录'} · {row['preview']}"

    read_line = create_reader(lambda: WORKDIR, session_choices)

    while True:
        try:
            line=read_line('\n> ').strip()
        except (EOFError,KeyboardInterrupt):
            print('\n[会话已结束]')
            break
        if not line:
            continue
        if line.startswith('/') or line == 'cd' or line.startswith('cd '):
            if line == 'cd' or line.startswith('cd '):
                line = '/' + line
            command, argument = parse_command(line)
            error = command_error(command, argument)
            if error:
                print(f'  {error}')
                continue
            line = f'{command} {argument}'.rstrip()
            if command in ('/exit','/quit'):
                break
            if command in ('/r', '/resume', '/sessions'):
                parts = line.split(maxsplit=1)
                if len(parts) != 2:
                    identifier = choose_session(session_rows(limit=None, include_empty=True),
                                                session_store.session_id, WORKDIR)
                    if identifier is None:
                        continue
                else:
                    identifier = parts[1]
                try:
                    rows = session_rows(include_empty=False, query=identifier)
                    matches = [SESSIONS_DIR / f"session_{row['id']}.jsonl" for row in rows]
                    if len(matches) == 1:
                        path = matches[0]
                    elif len(matches) > 1:
                        selected = choose_session(rows, session_store.session_id)
                        if selected is None:
                            continue
                        path = resolve_session_path(selected)
                    else:
                        path = resolve_session_path(identifier)
                    candidate = SessionEventStore.open(path)
                    history = candidate.rebuild_openai_history(recover_pending=True)
                    summary = candidate.session_summary()
                    if summary['workspace']:
                        set_workspace(summary['workspace'])
                    else:
                        print('  [session] 工作目录未记录，保留当前目录；请用 /cd 确认。')
                    session_store = candidate
                    messages = history
                    print(f"  已恢复：{summary['title']}\n  工作区：{WORKDIR}")
                except (FileNotFoundError, ValueError, SessionReplayError) as exc:
                    print(f'  [session] {exc}')
                continue
            if command in ('/n', '/new'):
                parts = line.split(maxsplit=1)
                requested = parts[1].strip() if len(parts) > 1 else None
                if requested and not requested.replace('-', '').replace('_', '').isalnum():
                    print('  [session] invalid session id')
                    continue
                if requested and (SESSIONS_DIR / f'session_{requested}.jsonl').exists():
                    print(f'  [session] already exists: {requested}; use /r {requested}')
                    continue
                session_store = SessionEventStore(SESSIONS_DIR, session_id=requested)
                session_store.append('session_created')
                session_store.record_workspace(WORKDIR)
                messages = []
                print('  已开始新会话，工作区保持不变。')
                continue
            handle_command(line,messages,session_store)
            continue
        messages[:] = session_store.rebuild_openai_history(recover_pending=True)
        messages.append({'role':'user','content':line})
        session_store.append_message(messages[-1])
        try:
            agent_loop(messages,session_store=session_store)
        except KeyboardInterrupt:
            print('\n[会话已中断]')
        messages[:]=session_store.rebuild_openai_history(recover_pending=True)


if __name__ == '__main__':
    # Keep ``python harness.py`` working while the real process entrypoint lives
    # in a module that can be tested and reused independently.
    from entrypoint import main

    main()
