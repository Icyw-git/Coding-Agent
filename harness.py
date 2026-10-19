import argparse
import os
from dotenv import load_dotenv
import json
import time
import threading
import subprocess
import concurrent.futures
import contextvars
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
                        _bound_tool_output, write_transcript, summarize_history,
                        _summary_input, _tail_within_budget,
                        compact_history, reactive_compact, inject_memories)
from tools import TOOLS, tool_registry, run_bash
from skill_system import scan_skills, list_skills, load_skill
from subagent import spawn_subagent
from session_store import SessionEventStore, SessionReplayError
from agent_runtime import AgentRuntime
from checkpoints import CheckpointManager, CheckpointError
from cli_commands import parse_command, command_error, format_command_help
from exec_policy import load_policy, set_tier
import approval_view   # 审批面的参数渲染（/approvals 列表要显示完整命令）
import sandbox
import hooks   # 副作用：注册 6 个 hook 回调（权限/日志/上下文/总结）
from perf import segment as perf_segment, record as perf_record, reset as perf_reset



load_dotenv()
WORKDIR = Path(os.getcwd()).resolve()


def get_workdir() -> Path:
    return current_workspace(WORKDIR)


SKILLS_DIR=WORKDIR/"skills"
scan_skills()   # 扫描技能目录 → SKILL_REGISTRY（skill_system 内）



class _LazyLLMClient:
    """延迟构造 OpenAI 客户端：openai SDK 的 import 约 1.5s，CLI/测试进程
    启动时不该为它买单。属性访问透明委托，首次真正调用 LLM 时才加载。"""

    def __init__(self):
        self._real = None

    def _instance(self):
        if self._real is None:
            import openai   # 首次使用时加载（import 即 ~1.5s）
            self._real = openai.OpenAI(
                api_key=os.getenv("LLM_API_KEY"),
                base_url=os.getenv("LLM_BASE_URL"),
                timeout=120,   # API 挂起时最多等 120s 抛错，避免静默阻塞 600s（SDK 默认）
            )
        return self._real

    def __getattr__(self, name):
        return getattr(self._instance(), name)

    def warmup(self):
        """后台预热：提前完成 openai SDK import 与客户端构造（~1.5s），
        用户敲第一条消息时不再等待。"""
        self._instance()


client = _LazyLLMClient()


def __getattr__(name):
    """模块级懒加载（PEP 562）：mcp_bridge 的 import 约 1s，只延迟到真正
    触及 MCP 的那一刻（initialize_mcp_tools / 生命周期关闭）。"""
    if name in ('register_mcp_tools', 'shutdown_mcp'):
        import mcp_bridge
        return getattr(mcp_bridge, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


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
    """Ask the configured model for a compact session topic, with a local fallback.

    取材是「会话在做什么」而非「最后说了什么」：首条有效 user（原始目标）+
    最近一条 assistant 结论 + 最近用过的工具名。旧实现 exchanges[-8:] 取尾部，
    长会话标题会变成最后一条助手回复的复述（实测 '工作区当前是干净的…'）。"""
    from session_store import is_injected_user_content
    first_user = next((m for m in messages
                       if m.get('role') == 'user' and isinstance(m.get('content'), str)
                       and m['content'].strip()
                       and not is_injected_user_content(m['content'])), None)
    last_assistant_text = next((m['content'].strip() for m in reversed(messages)
                                if m.get('role') == 'assistant'
                                and isinstance(m.get('content'), str)
                                and m['content'].strip()), '')
    tool_names = []
    for m in reversed(messages):
        for call in m.get('tool_calls') or []:
            name = (call.get('function') or {}).get('name', '')
            if name and name not in tool_names:
                tool_names.append(name)
    has_tool_context = bool(tool_names)
    exchanges = []
    if first_user is not None:
        exchanges.append(f"user（目标）: {first_user['content'].strip()[:600]}")
    if last_assistant_text:
        exchanges.append(f"assistant（最近结论）: {last_assistant_text[:400]}")
    if tool_names:
        exchanges.append("最近使用的工具: " + ', '.join(tool_names[:8]))
    fallback = next((
        ' '.join(line.split()).strip()[:60]
        for message in messages
        if message.get('role') == 'user' and isinstance(message.get('content'), str)
        and not is_injected_user_content(message['content'])
        for line in message['content'].splitlines()
        if _is_session_title_candidate(line)
    ), None)
    if fallback is None:
        fallback = next((
            ' '.join(line.split()).strip()[:60]
            for message in messages
            if message.get('role') == 'assistant' and isinstance(message.get('content'), str)
            and not is_injected_user_content(message['content'])
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
                    '为这段 coding agent 会话概括一个主题标题。只输出标题本体（名词短语，'
                    '不加句号、前缀或引号），使用用户原语言，不超过 20 个汉字或 8 个英文词。'
                    '概括的是整个会话在做什么，不是复述最后一条消息或结论。'
                    '忽略 <reminder>、<task_notification> 等系统注入和“继续、好的、go”等确认语；'
                    '如果记录没有提供足够信息，只输出 (untitled session)，不要猜测。'
                )},
                {'role': 'user', 'content': '\n'.join(exchanges)},
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


# ---------- 执行档位（/sandbox）----------
# 档位只描述「策略层怎么判」，不代表内核强制：见 exec_policy 模块头的边界说明。
SANDBOX_MODE_HELP = (
    ('read-only', '只放公认只读的命令，写改与其余命令都要审批'),
    ('workspace-write', '默认档：工作区内写改放行，破坏性命令询问'),
    ('danger-full-access', '取消提示级限制与工作区路径约束，只保留黑名单硬拒'),
)
APPROVAL_POLICY_HELP = (
    ('untrusted', '每条 bash 都要审批'),
    ('on-request', '默认档：只在策略层判定有风险时询问'),
    ('never', '不打断；除 danger-full-access 外，需要审批的操作直接失败'),
)


def format_sandbox_tier() -> str:
    """档位清单 + 当前选中项，/status 与 --doctor 都复用这段说明。"""
    policy = load_policy()
    lines = [
        f'  执行档位：{policy.sandbox_mode} · 审批档位：{policy.approval_policy}',
        f'  OS 沙箱：{sandbox.status_line()}（{sandbox.ENV}）',
    ]
    for title, options, current in (
            ('执行档位（/sandbox <档位>）', SANDBOX_MODE_HELP, policy.sandbox_mode),
            ('审批档位（/sandbox approvals <档位>）', APPROVAL_POLICY_HELP,
             policy.approval_policy)):
        lines.append(f'  {title}')
        lines.extend(f'    {"*" if name == current else " "}{name:<20}{note}'
                     for name, note in options)
    lines.append('  说明：档位是策略级判定（四个执行入口同一套规则），本身不做 OS 强制，'
                 '不隔离文件读取与网络。')
    lines.append(f'  OS 沙箱独立于此开关：require 时沙箱不可用则拒绝执行；'
                 f'启用后命令以 Low IL 运行，工作区外写入被内核拒绝，读取与网络仍不隔离。')
    return '\n'.join(lines)


def apply_sandbox_tier(argument: str) -> str:
    """/sandbox <档位> 与 /sandbox approvals <档位>。参数已由 command_error 校验。"""
    # 档位名大小写不敏感（load_policy 也是 casefold 读的）；分支判定同样要归一，
    # 否则 '/sandbox APPROVALS never' 校验通过却走进执行档位分支。
    parts = [part.casefold() for part in argument.split()]
    if parts[0] == 'approvals':
        policy = set_tier(approval_policy=parts[1])
        changed = f'审批档位 → {policy.approval_policy}'
    else:
        policy = set_tier(sandbox_mode=parts[0])
        changed = f'执行档位 → {policy.sandbox_mode}'
    # 档位写进环境：load_policy 即时读取，下一条工具调用就生效，子进程也继承
    return (f'  已切换 {changed}'
            f'（当前 {policy.sandbox_mode} · {policy.approval_policy}）')


def format_status(session_store) -> str:
    """Summarize local runtime state for the interactive REPL."""
    from turn_progress import relative_time
    manager = get_checkpoint_manager(session_store)
    with cron_lock:
        cron_count = len(scheduled_jobs)
    with background_lock:
        background_count = len(background_tasks)
    activity = [f'{count} 个{label}' for label, count in (
        ('任务', len(list_tasks())), ('定时任务', cron_count),
        ('后台任务', background_count), ('协作成员', len(active_teammates))) if count]
    summary = session_store.session_summary()
    usage = session_store.usage_totals()   # 从 usage 事件聚合，跨重启准确
    policy = load_policy()
    usage_line = (f'\n  tokens：{_fmt_tokens(usage["total"])}'
                  f'（输入 {_fmt_tokens(usage["prompt"])} · 输出 {_fmt_tokens(usage["completion"])}）'
                  f'{_cache_hit_rate_line(usage)}'
                  if usage.get('total') else '')
    return (
        f"  会话：{summary['title']}\n"
        f"  最近活跃：{relative_time(summary['updated'])}\n"
        f'  工作区：{WORKDIR}\n'
        f'  模型：{os.getenv("LLM_MODEL_ID", "未配置")}\n'
        f'  执行档位：{policy.sandbox_mode} · 审批：{policy.approval_policy} · '
        f'OS 沙箱：{sandbox.configured_mode()}\n'
        f'  快照：{len(manager.list(limit=10_000))} 个\n'
        f'  任务概况：{" · ".join(activity) if activity else "暂无任务"}'
        f'{usage_line}'
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
    text = '\n'.join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + '\n  （diff 过长，已截断…）'
    return text


def format_pending_approvals() -> str:
    """Show actionable approval context without exposing internal request IDs."""
    pending = hooks.GATE.pending()
    if not pending:
        return '  没有待审批操作。'
    lines = ['  待审批操作：']
    for index, request in enumerate(pending, 1):
        reason = str(request.reason or '').strip() or '未提供原因'
        tool = str(request.tool or '未知操作')
        lines.append(f'    {index}. {approval_view.safe_text(f"{tool} · {reason}")}')
        # 批的就是这条命令：只列工具名等于让人盲批（旧版就是这样）
        detail = approval_view.detail(tool, request.args)
        lines.extend(f'         {line}' for line in detail.splitlines())
    lines.append('  请使用飞书 /approve 或审批卡片处理；CLI 可回复 /approvals <序号> y|n 决议。')
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
    # 档位属于运行时配置，诊断输出里必须看得见：否则「以为开了只读其实没开」
    # 这类误差只能在事故发生后才发现。
    policy = load_policy()
    print(f'[policy] sandbox_mode={policy.sandbox_mode} '
          f'approval_policy={policy.approval_policy}（策略级判定）')
    print(f'[os-sandbox] {sandbox.ENV}={sandbox.configured_mode()}'
          f'（Windows 低完整性，仅约束 shell 子进程）')
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
# DeepSeek 官方（api-docs.deepseek.com/models）：V4.1-Flash / V4-Pro 上下文均为 1M；
# deepseek-chat / deepseek-reasoner / deepseek-v4-flash 为已退役旧名（仍被接受，
# 路由到 V4.1-Flash）——窗口同为 1M，勿按旧文档写 64K/128K。
MODEL_WINDOWS={
    'deepseek-flash':1_048_576,
    'deepseek-v4-flash':1_048_576,
    'deepseek-v4-pro':1_048_576,
    'deepseek-chat':1_048_576,
    'deepseek-reasoner':1_048_576,
}
OUTPUT_RESERVE=8192        # 给 max_tokens=8000 输出预留
CTX_SAFETY=0.8             # 留余量，防 prompt_too_long

def _default_context_limit()->int:
    model=os.getenv('LLM_MODEL_ID','')
    window=os.getenv('LLM_CONTEXT_WINDOW') or MODEL_WINDOWS.get(model)
    if window is None:
        # Codex 行为：模型信息表 + 告警，绝不静默降级（此前 deepseek-flash 缺注册
        # 曾静默回落 64K，压缩阈值被低估 16 倍）
        print(f'[ctx] 未知模型 {model!r}：按 64K 窗口保守处理，'
              f'可用 LLM_CONTEXT_WINDOW 显式配置', file=sys.stderr)
        window=65536
    return max(4000,int((int(window)-OUTPUT_RESERVE)*CTX_SAFETY))

CONTEXT_LIMIT=_default_context_limit()
KEEP_RECENT=6
PERSIST_THERSHOLD=10000
TOOL_RESULT_CHAR_LIMIT=int(os.getenv('HARNESS_TOOL_RESULT_CHARS') or 24_000)  # 工具输出入口截断线（≈7k tokens）

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
MAX_REACTIVE_RETRIES=3   # 逐次缩尾重试（keep/budget 见 agent_loop 的 attempts），仍超限才 raise


def get_system_prompt(context:dict)->str: #获取上下文，如果上下文没有改变，直接返回缓存的系统提示，否则重新组装系统提示
    key=json.dumps(context,sort_keys=True,ensure_ascii=False,default=str)
    cached = _prompt_cache.get()
    if cached is not None and cached[0] == key:
        if os.getenv('HARNESS_DEBUG') == '1':
            print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return cached[1]
    prompt = assemble_system_prompt(context)
    _prompt_cache.set((key, prompt))

    loaded=['identity','tools','workspace','rules']
    if context.get('skills'):
        loaded.append('skills')
    if os.getenv('HARNESS_DEBUG') == '1':
        print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return prompt

def update_context(context:dict,messages:list)->dict: # 更新上下文：工具、工作目录、技能
    # ⚠ 记忆不再进 system prompt：housekeeping 每轮更新 MEMORY_INDEX，若嵌在
    # system 里，请求前缀每轮逐字变化，供应商前缀缓存（KV 复用）全失效。
    # 记忆由 inject_memories 作为请求末尾的独立 system 消息注入。
    return {
        'enabled_tools':list(tool_registry.keys()),
        'workspace':str(get_workdir()),
        'skills':list_skills(),
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

def pre_compact_messages(messages, skip_micro_rounds=0, on_compact=None, session_store=None,
                         size=None, snip_state=None): #压缩逻辑，每轮开头的压缩预处理：micro → snip → budget →（仍超限）LLM 摘要。返回 (压缩后的 messages, 新的 skip_micro_rounds)。
    """每轮开头的压缩预处理，判据优先用真实用量（P0）：

    - size：上一次请求的 prompt_tokens（供应商权威值）+ 本回合新增消息的估算
      增量，由 agent_loop 逐调用维护；None 时回落 estimate_size（teammate /
      直调 / 会话首轮）。旧实现每轮对全量历史逐字符估算（4M 字符 ≈ 279ms）
      且 CJK 高估 1.69 倍——判据是猜的，现在只有增量是估的。
    - 顺序 micro → snip → budget：先便宜可恢复的（占位符），再破坏性的
      （整段截断），budget 因入口截断降级为净兜底。
    - 任一层发生改写即重算 size（改写会缩小上下文，旧值会让 auto 门槛误触发）。
    - snip_state（agent_loop 持有的 dict）：snip 触发迟滞——触发后下次触发线 =
      压缩后条数 + 阈值/4。实测无迟滞时越线后每回合滑动（marker 每次变文本 +
      尾段位移），22 次调用回合命中率仅 56%、实测会话低至 6%；迟滞让两次触发
      之间前缀逐字稳定，每次触发只断一次（对齐 Codex「越线一次性替换」手感）。"""
    if size is None:
        size = estimate_size(messages)
    micro_info = {}
    if skip_micro_rounds > 0:
        skip_micro_rounds -= 1   # 压缩后前几轮跳过 micro_compact，保留刚拿到的工具结果
    else:
        messages = micro_compact(messages, size, session_store=session_store, info=micro_info)
        if micro_info.get('rewrote'):
            size = estimate_size(messages)
    snip_info = {}
    compacted = snip_compact(messages, max_messages=(snip_state or {}).get('fire_at'),
                             info=snip_info)
    if snip_info:
        # snip 触发：截断决策持久化为索引事件（head_end/tail_start/snipped，
        # rebuild 用同一切片表达式逐字还原，marker 冻结到下次触发）
        if session_store is not None:
            session_store.append_history_snip(**snip_info)
        messages = compacted
        size = estimate_size(messages)
        if snip_state is not None:
            snip_state['fire_at'] = len(messages) + max(8, len(messages) // 4)
    budget_info = {}
    messages = tool_result_budget(messages, session_store=session_store, info=budget_info)
    if budget_info.get('rewrote'):
        size = estimate_size(messages)
    if size > CONTEXT_LIMIT:
        print("  [compact] 上下文接近上限，已自动压缩历史（完整记录已保存）")
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
    # 入口 middle-out 截断：历史里从此不存在超大工具结果（事件流存的即最终
    # 形态，前缀天然稳定；全量已落盘可恢复）。hooks 拿到的仍是全量输出。
    return name, args, {"role": "tool", "tool_call_id": tool_call.id,
                        "content": _bound_tool_output(tool_call.id, output)}


# 无副作用工具白名单：模型一轮返回多个 tool_call 时这些可以并行执行（Codex 行为）。
# 可变工具（bash/write/edit/任务写入/cron 变更/子代理等）与 loop 行为永不并行。
PARALLEL_SAFE_TOOLS = frozenset({
    'read', 'glob', 'list_tasks', 'get_task', 'list_crons', 'check_inbox', 'load_skill',
})
PARALLEL_MAX_WORKERS = 4


def _run_tool_batch(tool_calls, session_store, tool_messages):
    """执行一批 tool_call：全部只读时并行，任一可变则整批串行兜底。

    started 事件与结果**始终按模型给定的原序**写回 store —— append-only
    回放要求 tool 结果顺序与 assistant.tool_calls 对齐，乱序会产生孤儿。
    线程内用 copy_context 传播 checkpoint/perf 的 ContextVar。"""
    parallel = (len(tool_calls) > 1
                and all(tc.function.name in PARALLEL_SAFE_TOOLS for tc in tool_calls))
    if parallel:
        for tool_call in tool_calls:
            session_store.append_tool_started(tool_call)   # 原序登记全部 started
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(PARALLEL_MAX_WORKERS, len(tool_calls))) as pool:
            executed = list(pool.map(
                lambda tc: contextvars.copy_context().run(exec_tool_call, tc),
                tool_calls))
    else:
        executed = []
        for tool_call in tool_calls:
            session_store.append_tool_started(tool_call)
            executed.append(exec_tool_call(tool_call))
    for _, _, tool_message in executed:   # 原序写回结果
        tool_messages.append(tool_message)
        session_store.append_message(tool_message)

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
    # tail（压缩保留的最近工作现场）一并写入事件：此前 reactive 压缩保留的
    # 最近 5 条只活在当前进程，重启回放后消失（只剩摘要）。
    tail = []
    marker_seen = False
    for message in compacted_messages:
        if (not marker_seen and message.get('role') == 'user'
                and str(message.get('content', '')).startswith('[')):
            marker_seen = True   # 摘要 marker 本身不入 tail
            continue
        tail.append(message)
    session_store.append_compaction(summary, strategy, tail=tail)


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
    from turn_progress import relative_time
    lines=[]
    for row in rows:
        mark='*' if row['cp_id']==head else ' '
        label = row.get('label') or ('手动快照' if row['trigger'] == 'manual' else '任务快照')
        status = ' · 尚未完成' if row['status'] == 'open' else ''
        current = ' · 当前' if mark == '*' else ''
        # 时间锚点：没有它无法分辨快照是「刚才」还是「两小时前」的
        when = f" · {relative_time(row.get('created_at'))}" if row.get('created_at') else ''
        lines.append(f"  {label}{current}{status} · {len(row['files'])} 个文件{when}\n"
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

    elif command == '/sandbox':
        print(apply_sandbox_tier(argument) if argument else format_sandbox_tier())

    elif command == '/approvals':
        tokens = argument.split()
        if tokens:
            index, choice = int(tokens[0]) - 1, tokens[1]
            pending = hooks.GATE.pending()
            if not 0 <= index < len(pending):
                print('  没有该序号的待审批操作；先运行 /approvals 查看列表。')
                return
            req = pending[index]
            record = hooks.GATE.resolve(req.req_id, choice == 'y', by='cli')
            print(f'  已决议：{record.status} · {req.tool}')
        else:
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
        # compaction 事件回放时会重建 marker 消息本身，不能再追加同一条
        print(f'  已压缩会话上下文：{before} 条消息 → {len(messages)} 条。完整记录已保存。')

    elif command == '/find':
        if not argument:
            print('  用法：/find <关键词>（跨会话搜索历史内容）')
            return
        rows = SessionEventStore.search_all(SESSIONS_DIR, argument, limit=8)
        if not rows:
            print(f'  没有包含「{argument}」的会话。')
            return
        from turn_progress import relative_time
        for row in rows:
            print(f"  {row['id'][:12]}  {relative_time(row.get('updated'))}  {row['title']}")
            print(f"    {row.get('matched', '')}")

    elif command == '/rename':
        if session_store is None:
            print('  [rename] 当前没有会话上下文。')
            return
        title = session_store.append_session_title(argument.strip()[:80], source='user')
        print(f"  会话主题已设置为：{title['title']}")

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


def _post_turn_housekeeping(pre_compress, messages_snapshot, session_store, turn_no=1):
    """回合结束后的副通道作业：会话标题生成/重生成 + 记忆提炼/合并。

    每项各自要走一次 LLM——放后台线程执行，回复立即返回给调用方，
    不让飞书/SDK 用户多等几秒到几十秒。异常只告警，绝不影响主流程。
    标题：首次生成 + 每 8 回合异步重生成（主题演化后旧标题会过时）；
    用户 /rename 设过（source='user'）的会话永不重生成。
    """
    try:
        saved = session_store.saved_session_title()
        source = session_store.saved_session_title_source()
        if source != 'user' and (not saved or (turn_no > 1 and turn_no % 8 == 0)):
            title = generate_session_title(messages_snapshot)
            if title != '(untitled session)' and title != saved:
                session_store.append_session_title(title, source='auto')
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


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f'{n / 1_000_000:.2f}M'
    if n >= 1000:
        return f'{n / 1000:.1f}k'
    return str(n)


def _parse_usage(usage) -> dict | None:
    """解析一次 LLM 调用的 usage（缺失/无效时返回 None）。

    同时采集缓存命中 token：OpenAI 为 prompt_tokens_details.cached_tokens，
    DeepSeek 为扁平字段 prompt_cache_hit_tokens —— 没有它命中率不可观测。"""
    if not usage:
        return None
    prompt = int(getattr(usage, 'prompt_tokens', 0) or 0)
    completion = int(getattr(usage, 'completion_tokens', 0) or 0)
    total = int(getattr(usage, 'total_tokens', 0) or prompt + completion)
    if total <= 0:
        return None
    details = getattr(usage, 'prompt_tokens_details', None)
    cached = getattr(details, 'cached_tokens', None) if details is not None else None
    if cached is None:
        cached = getattr(usage, 'prompt_cache_hit_tokens', None)
    return {'prompt': prompt, 'completion': completion, 'total': total,
            'cached': int(cached or 0), 'cached_seen': cached is not None}


def _cache_hit_rate_line(bucket: dict) -> str:
    """命中率后缀：供应商未回传 cached 字段时不显示（避免把「未知」当 0）。"""
    if not bucket.get('cached_seen') or not bucket.get('prompt'):
        return ''
    rate = bucket['cached'] / bucket['prompt']
    return f' · 缓存命中 {rate:.0%}'


def _stream_completion(create_kwargs, on_delta):
    """Collect a streamed completion while preserving the existing response shape."""
    _t0 = time.perf_counter()
    _ttfb_done = False
    # include_usage：流式默认不带 usage，显式索取后随收尾块返回（Deepseek/OpenAI 兼容）
    stream = client.chat.completions.create(**create_kwargs, stream=True,
                                            stream_options={'include_usage': True})
    if not hasattr(stream, '__iter__'):
        # 客户端不支持流式（测试假件/兼容层返回非迭代对象）：整段作为单次
        # 增量交付并按非流式形状返回，调用方无需区分。
        response = stream
        content = getattr(response.choices[0].message, 'content', None)
        if content:
            on_delta(content)
        return response
    text_parts = []
    tool_calls = {}
    finish_reason = None
    usage = None
    for chunk in stream:
        usage = getattr(chunk, 'usage', None) or usage
        if not getattr(chunk, 'choices', None):
            continue   # include_usage 的收尾块只带 usage、choices 为空
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
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
    )


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
    streamed_parts=[] #最近一次 API 调用已流式输出的文本（抑制最终重复打印）
    turn_tokens=0 #本回合累计 tokens（含工具轮次）
    turn_usage={'prompt':0,'completion':0,'cached':0,'total':0,'cached_seen':False} #本回合明细（回合末打印）
    ctx_tokens=None #压缩判据：上次请求的 prompt_tokens（供应商权威值）+ 新增消息估算增量
    snip_state={} #snip 触发迟滞：触发后基线+阈值/4 才允许再次触发（无迟滞则越线后每回合滑动断前缀）
    turn_no=0 #已完成回合数（每 8 回合异步重生成一次会话标题）
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
            session_store=session_store,
            size=ctx_tokens,
            snip_state=snip_state,
        ) #压缩预处理流程（判据优先用真实用量，见 pre_compact_messages）

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

            def _delta_hook(part):
                streamed_parts.append(part)
                on_delta(part)

            streamed_parts.clear()   # 标记只反映最近一次调用
            return _stream_completion(kwargs, _delta_hook)

        try: #错误恢复逻辑
            with perf_segment('llm.main'):
                response=recovery.with_retry(_create,recovery_state)
            reactive_retries=0
            detail=_parse_usage(getattr(response, 'usage', None))
            if detail:
                turn_tokens += detail['total']
                for _k in ('prompt','completion','cached','total'):
                    turn_usage[_k] += detail[_k]
                turn_usage['cached_seen'] = turn_usage['cached_seen'] or detail['cached_seen']
                # 埋点：逐调用 usage 事件（供应商权威值；命中率可逐调用分析，
                # 聚合口径由 usage_totals() 求和保持不变）
                session_store.append('usage', **detail)
                ctx_tokens = detail['prompt']   # 压缩判据锚点：本请求真实大小
        except Exception as e:
            if recovery.is_prompt_too_long_error(e) and reactive_retries<MAX_REACTIVE_RETRIES:
                # 逐次缩尾：keep/budget 收紧（500KB 尾部单靠条数压不住）
                attempts=((6,None),(3,12_000),(1,4_000))
                keep,budget=attempts[min(reactive_retries,len(attempts)-1)]
                reactive_retries+=1
                print(f'  [compact] 请求超限，已自动压缩历史并重试'
                      f'（第 {reactive_retries}/{MAX_REACTIVE_RETRIES} 次，保留 {keep} 条）')
                messages[:]=reactive_compact(messages, keep=keep, budget_tokens=budget)
                _record_compaction(session_store, messages, 'reactive')
                skip_micro_rounds=3
                if ctx_tokens is not None:
                    ctx_tokens = estimate_size(messages)   # 历史被整体替换，重估避免 auto 误触发
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
        if ctx_tokens is not None:   # 增量：只有新增部分走估算（assistant 回复较小）
            ctx_tokens += estimate_size([assistant_message])

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
            if ctx_tokens is not None:
                ctx_tokens += estimate_size([continuation])
            continue


        if response.choices[0].finish_reason != "tool_calls":
            force=trigger_hooks('Stop',messages)
            if force:
                messages.append({'role':'user','content':force})
                if ctx_tokens is not None:
                    ctx_tokens += estimate_size([messages[-1]])
                continue
            if message.content:
                if streamed_parts:
                    print()   # 流式已逐段输出到终端，只补换行
                else:
                    print(message.content)
            if turn_tokens:
                cum = session_store.usage_totals()   # 逐调用 usage 事件聚合
                print(f'  [tokens] 本轮 {_fmt_tokens(turn_tokens)} · '
                      f'会话累计 {_fmt_tokens(cum["total"])}'
                      f'{_cache_hit_rate_line(cum)}')
            write_transcript(messages)   # 正常结束也落盘，便于复盘
            session_store.append('turn_completed')
            _safe_checkpoint(checkpoint_manager.seal)   # 固化本节点：记录改动文件写入后的 hash
            # 标题/记忆提炼移入后台线程（_post_turn_housekeeping）：回复立即返回，
            # 副通道各自的 LLM 调用不再阻塞端到端延迟。快照防后续轮次改写。
            turn_no += 1
            housekeeping = threading.Thread(
                target=_post_turn_housekeeping,
                args=(pre_compress, list(messages), session_store, turn_no),
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
        # 连续的普通 tool_call 攒成 batch（全只读并行 / 含可变串行，见 _run_tool_batch），
        # 遇 checkpoint/compact 特殊工具先 flush 再处理。
        tool_messages = []
        compacted = False
        used_todo = False
        batch = []

        def _flush_batch():
            _run_tool_batch(batch, session_store, tool_messages)
            batch.clear()

        for tool_call in message.tool_calls:
            if tool_call.function.name == 'todo_write':
                used_todo = True
            if tool_call.function.name == 'checkpoint':
                # 当前节点在本轮请求前已创建，快照是完整 OpenAI 历史。只给它加标签，不能
                # 在 assistant tool_calls 与对应 tool 结果之间新建快照，否则 rewind 后历史无法重放。
                # 与 compact 一样不进 tool_registry —— 它是 loop 行为，不是普通工具。
                _flush_batch()
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
                _flush_batch()
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
                if ctx_tokens is not None:
                    ctx_tokens = estimate_size(messages)   # 历史被整体替换，重估避免 auto 误触发
                break

            batch.append(tool_call)
        _flush_batch()

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
        if ctx_tokens is not None and tool_messages:   # 增量：新增工具结果（已入口截断，单条有界）
            ctx_tokens += estimate_size(tool_messages)
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
            # 优先取模块命名空间里的 register_mcp_tools（测试可打补丁）；
            # 未注入时才懒加载 mcp_bridge（import 约 1s，见模块级 __getattr__）
            register = globals().get('register_mcp_tools')
            if register is None:
                from mcp_bridge import register_mcp_tools as register
            schemas = register(tool_registry)
            TOOLS.extend(schemas)
            # 第三方工具的副作用不在策略层的已知集合里，登记后默认询问
            # （hooks.EXTERNAL_TOOLS → exec_policy 的 external_tools 判定）。
            hooks.register_external_tools(
                schema['function']['name'] for schema in schemas)
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
            from turn_progress import relative_time
            yield row['id'], (f"{row['title']} · {relative_time(row.get('updated'))}"
                              f" · {row['workspace'] or '工作目录未记录'} · {row['preview']}")

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
                argument = parts[1].strip() if len(parts) > 1 else ''
                try:
                    if argument == 'last':
                        rows = session_rows(limit=1, include_empty=False)
                        if not rows:
                            print('  [session] 没有可恢复的会话。')
                            continue
                        if rows[0]['id'] == session_store.session_id:
                            print(f"  [session] 最近会话就是当前会话：{rows[0]['title']}")
                            continue
                        path = resolve_session_path(rows[0]['id'])
                    elif not argument:
                        identifier = choose_session(session_rows(limit=None, include_empty=True),
                                                    session_store.session_id, WORKDIR)
                        if identifier is None:
                            continue
                        path = resolve_session_path(identifier)
                    else:
                        rows = session_rows(include_empty=False, query=argument)
                        matches = [SESSIONS_DIR / f"session_{row['id']}.jsonl" for row in rows]
                        if len(matches) == 1:
                            path = matches[0]
                        elif len(matches) > 1:
                            selected = choose_session(rows, session_store.session_id)
                            if selected is None:
                                continue
                            path = resolve_session_path(selected)
                        else:
                            path = resolve_session_path(argument)
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
            if command == '/fork':
                parts = line.split(maxsplit=1)
                upto = None
                if len(parts) > 1:
                    if not parts[1].strip().isdigit():
                        print('  [fork] 用法：/fork [事件序号]，缺省复制全部历史。')
                        continue
                    upto = int(parts[1].strip())
                try:
                    forked = SessionEventStore.fork(session_store, upto_seq=upto)
                except (FileExistsError, OSError, SessionReplayError) as exc:
                    print(f'  [fork] 分叉失败：{exc}')
                    continue
                session_store = forked
                messages = session_store.rebuild_openai_history()
                print(f'  已分叉出新会话 {forked.session_id}（原会话保留），后续对话在新会话进行。')
                continue
            handle_command(line,messages,session_store)
            continue
        messages[:] = session_store.rebuild_openai_history(recover_pending=True)
        messages.append({'role':'user','content':line})
        session_store.append_message(messages[-1])
        try:
            # CLI 直连流式：模型文本逐段上屏（栈内 gateway 早已用同一条路径）
            agent_loop(messages, session_store=session_store,
                       on_delta=lambda part: print(part, end='', flush=True))
        except KeyboardInterrupt:
            print('\n[会话已中断]')
        messages[:]=session_store.rebuild_openai_history(recover_pending=True)


if __name__ == '__main__':
    # Keep ``python harness.py`` working while the real process entrypoint lives
    # in a module that can be tested and reused independently.
    from entrypoint import main

    main()
