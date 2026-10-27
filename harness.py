import os

# 执行/审批档位只认「启动进程时的真实环境」：项目根的 .env 就在工作区里，
# write/edit 在默认档免审，Agent 改一行就能把下次启动的权限降到 never。
# 所以先记住进程起来时环境里有什么，load_dotenv 之后再把这几个键换回去。
POLICY_ENV_KEYS = ('HARNESS_SANDBOX_MODE', 'HARNESS_APPROVAL_POLICY',
                   'HARNESS_APPROVAL_ALL')
_TRUSTED_POLICY_ENV = {key: os.environ[key] for key in POLICY_ENV_KEYS
                       if key in os.environ}


def enforce_trusted_policy_env() -> None:
    """把档位键恢复成进程启动时的样子；.env 提供的档位一律忽略。

    忽略时要说出来而不是悄悄回落：用户以为 `.env` 里写了 read-only，实际跑的是
    默认档，等于一个反向的静默降级（与「档位名写错不静默回落」同一个原则）。
    """
    ignored = set()
    for key in POLICY_ENV_KEYS:
        if key in _TRUSTED_POLICY_ENV:
            os.environ[key] = _TRUSTED_POLICY_ENV[key]
        else:
            if os.environ.get(key):      # 这个值只可能来自 dotenv 文件
                ignored.add(key)
            os.environ.pop(key, None)
    if ignored:
        print(f"[policy] 已忽略配置文件里的 {'、'.join(sorted(ignored))}"
              f"（档位只认启动进程时的环境，运行中用 /sandbox 切换）", file=sys.stderr)


import hashlib
import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import checkpoint_commands
import checkpoints
import cron_delivery
import heartbeat
import hooks  # 副作用：注册 6 个 hook 回调（权限/日志/上下文/总结）
import llm_config
import llm_route
import paths
import recovery
import sandbox
import status_report
import taint
import tool_cache
import turn_control
import turn_files
import workspace_diff
from agent_core import (
    HOOKS,
    _bound_tool_output,
    _extract_text,
    _is_tool_result_message,
    _message_has_tool_use,
    _parse_frontmatter,
    _rebuild_index,
    _summary_input,
    _tail_within_budget,
    collect_tool_results,
    compact_history,
    consolidate_memories,
    estimate_size,
    expand_image_parts,
    extract_memories,
    image_attachment_message,
    inject_memories,
    list_memory_files,
    load_memories,
    micro_compact,
    persist_large_output,
    reactive_compact,
    read_memory_file,
    read_memory_index,
    register_hook,
    select_relevant_memories,
    snip_compact,
    summarize_history,
    tool_result_budget,
    trigger_hooks,
    write_memory_file,
    write_transcript,
)
from agent_runtime import AgentRuntime
from agent_teams import BUS, active_teammates, spawn_teammate_thread
from background_task import (
    background_lock,
    background_tasks,
    collect_background_results,
)
from background_watch import watch_command
from checkpoint_commands import format_checkpoint_list, format_restore_report, rewind_to
from checkpoints import CheckpointError, CheckpointManager
from cli_commands import command_error
from cron_scheduler import (
    agent_lock,
    cancel_job,
    consume_cron_queue,
    cron_lock,
    cron_scheduler_loop,
    load_durable_jobs,
    queue_processor_loop,
    schedule_job,
    scheduled_jobs,
    wait_for_cron_idle,
)
from exec_policy import load_policy
from git_branch import current_branch
from line_repl import prompt_text, run_line_repl
from perf import record as perf_record
from perf import reset as perf_reset
from perf import segment as perf_segment
from prompt_assembly import (
    assemble_system_prompt,
    collect_context,
    environment_message,
    get_system_prompt,
)
from prompt_assembly import build_sub_system as _build_sub_system
from prompt_assembly import build_system as _build_system
from prompt_assembly import project_prompt_section as _project_prompt_section
from prompt_cache_debug import enabled as cache_debug_enabled
from prompt_cache_debug import fingerprints as cache_fingerprints
from prompt_cache_debug import first_divergence as first_cache_divergence
from prompt_cache_debug import sample_every as cache_sample_every
from repl_commands import handle_command
from session_admin import (
    _session_catalog,
    _split_workspace_add,
    _workspace_for_new_session,
    add_workspace_from_argument,
    apply_session_cleanup,
    export_session,
    format_session_list,
    format_workspace_list,
    handle_workspace_command,
    list_session_paths,
    list_transcripts,
    load_transcript,
    registered_workspaces,
    remove_workspace_selector,
    resolve_session_path,
    session_cleanup_plan,
    session_rows,
    set_workspace,
    start_new_session,
    workspace_catalog,
    workspace_registry_path,
)
from session_store import SessionEventStore
from session_title import generate_session_title as _generate_session_title
from skill_system import list_skills, load_skill, scan_skills
from status_report import (
    APPROVAL_POLICY_HELP,
    SANDBOX_MODE_HELP,
    _document_extras_status,
    apply_sandbox_tier,
    format_pending_approvals,
    format_sandbox_tier,
    format_status,
    format_storage_profile,
    format_workspace_diff,
    run_doctor,
    storage_profile_data,
)
from subagent import spawn_subagent
from task_system import claim_task, complete_task, create_task, get_task, list_tasks
from tool_dispatch import (
    PARALLEL_SAFE_TOOLS,
    _print_tool_activity,
    _run_tool_batch,
    exec_tool_call,
    format_tool_output,
)
from tools import TOOLS, restore_todos, run_bash, tool_registry
from workspace_context import current_workspace, use_workspace


# 请求前缀包含 system + tools + 历史：把 tools 指纹写进 usage 事件，事后
# 不用开调试开关也能把缓存跌破归因到「模型 / system / 工具表」哪一项变了。
# MCP 工具在运行期才追加到 TOOLS，所以指纹要在记 usage 时现算。
def _tools_fingerprint() -> str:
    return hashlib.sha1(repr(TOOLS).encode('utf-8', 'replace')).hexdigest()[:12]

def _apply_secure_credentials() -> None:
    """密文凭据优先于 .env：迁移过的密钥不该被同名的明文值盖回去。

    只在 ``load_dotenv`` 之后调用。任何失败都降级为空操作（继续用 .env），
    启动不因为这一步失败——凭据存储是数据保护，不是启动前置条件。
    ``HARNESS_DISABLE_SECURE_CREDENTIALS=1`` 可以强制忽略密文（排障用）。
    """
    if (os.getenv('HARNESS_DISABLE_SECURE_CREDENTIALS') or '').strip() == '1':
        return
    try:
        import credentials

        credentials.apply()
    except Exception as exc:      # 缺 win32crypt / 文件损坏 / 权限：都只提示
        print(f'  \033[33m[credentials] 跳过加密凭据：{type(exc).__name__}: {exc}\033[0m')


load_dotenv()
_apply_secure_credentials()   # 密文凭据优先于 .env，见 credentials.py
enforce_trusted_policy_env()   # 档位不认 .env，见文件顶部的说明
WORKDIR = Path.cwd().resolve()   # CLI 当前工作区的进程级回退值，唯一写入方是 set_workspace
APP_DIR = Path(__file__).resolve().parent
HOME_DIR = paths.home_dir()      # agent 数据目录：HARNESS_HOME，默认仓库根
CACHE_DIR = paths.cache_dir()    # 会话/记忆/快照都在这里，与 cwd 解耦


def get_workdir() -> Path:
    """当前工作区：执行上下文（ContextVar）优先，CLI 全局值为回退。

    任何「当前工作区」的读取都应走这里；直接读 ``WORKDIR`` 会在
    ``use_workspace`` 固定的回合里拿到别的目录。
    """
    return current_workspace(WORKDIR)


SKILLS_DIR=WORKDIR/"skills"                 # 当前工作区的项目技能
BUILTIN_SKILLS_DIR=APP_DIR/"skills"         # 随 harness 分发的内建技能
scan_skills()   # 扫描内建 + 项目技能目录 → SKILL_REGISTRY（skill_system 内）


# ── 提示与标题的依赖注入 ──
# src 中的原语不感知入口；这里把工作区、实时工具表、技能目录与 client 传进去。
def project_prompt_section(workspace=None) -> str:
    return _project_prompt_section(workspace or get_workdir())


def update_context(context: dict, messages: list) -> dict:
    return collect_context(workdir=get_workdir(),
                           tool_names=tool_registry.keys(),
                           skills=list_skills())


def build_system() -> str:
    return _build_system(workdir=get_workdir(),
                         tool_names=tool_registry.keys(),
                         skills=list_skills())


def build_sub_system() -> str:
    return _build_sub_system(workdir=get_workdir(), skills=list_skills())


def generate_session_title(messages: list[dict]) -> str:
    target = llm_route.target_for('title')
    if target is None:
        return _generate_session_title(messages, client, model=None)
    return _generate_session_title(
        messages, client_for('title', target), model=target.model)


# 兼容旧名字：入口与测试把 `client` 当普通 OpenAI 客户端用，角色解析放在
# llm_config 里；默认角色 main，首次请求时才 import openai 并构造客户端。
_LazyLLMClient = llm_config.LazyClient
client = _LazyLLMClient('main')


def model_target(role: str = 'main') -> llm_config.ModelTarget:
    """当前角色最终使用的 provider/model/key。配置错误直接抛出，便于启动校验。"""
    return llm_config.get_settings().resolve_for(role)


def model_for(role: str = 'main') -> str:
    """状态行/诊断用的模型名；配置缺失时退回旧变量，避免 import 期崩溃。"""
    return llm_route.model_for(role)


def client_for(role: str = 'main', target=None):
    """按角色取客户端。

    与 main 同端点同凭据时复用模块级 ``client``——测试一直用
    ``monkeypatch.setattr(harness, 'client', fake)`` 注入，副通道必须继续命中
    这个 patch 点；只有配置了不同 provider 时才另建并缓存客户端。
    """
    target = target if target is not None else model_target(role)
    try:
        main = model_target('main')
    except llm_config.ConfigError:
        main = None
    if main is not None and (
            target.provider, target.base_url, target.api_key_env, target.api_key
    ) == (main.provider, main.base_url, main.api_key_env, main.api_key):
        return client
    return llm_config.get_client(target)


def active_model(state=None) -> str:
    """当前生效模型：回退后显示新模型，未回退时按配置解析。"""
    target = getattr(state, 'current_target', None)
    if target is not None:
        return target.model
    return (getattr(state, 'current_model', None)
            or model_for('main')
            or os.getenv('LLM_MODEL_ID') or '')


def format_main_target() -> str:
    """状态页显示 provider/model；旧配置只显示模型名。"""
    target = llm_route.target_for('main')
    return target.ref if target is not None else (os.getenv('LLM_MODEL_ID') or '未配置')

# UI 事件总线与权限门：Textual / ANSI 渲染层订阅 bus，并在弹窗里应答审批。
# 非 UI 模式下 PermissionGate 仍走 stdin，行为与原 console 审批一致。
from ui_events import PermissionGate, bus  # noqa: E402  UI 依赖放在配置区之后

permission_gate = PermissionGate(bus)
from ui_bridge import (  # noqa: E402
    METRICS,
    set_agent,
    set_session,
    status_payload,
    streaming_enabled,
)
from ui_bridge import emit as ui_emit


def __getattr__(name):
    """模块级懒加载（PEP 562）：mcp_bridge 的 import 约 1s，只延迟到真正
    触及 MCP 的那一刻（initialize_mcp_tools / 生命周期关闭）。"""
    if name in ('register_mcp_tools', 'shutdown_mcp'):
        import mcp_bridge
        return getattr(mcp_bridge, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def validate_runtime_config(environ=None) -> None:
    """Fail early with an actionable error when required model settings are absent.

    旧变量路径保持原有报错文案（LLM_API_KEY, LLM_MODEL_ID）；新配置路径按
    main 角色实际引用的 provider 检查密钥，避免要求一个根本用不到的变量。
    """
    environ = os.environ if environ is None else environ
    settings = llm_config.load_settings(environ)
    try:
        settings.validate_main()
    except llm_config.ConfigError as exc:
        if not environ.get('HARNESS_LLM_CONFIG') and not environ.get('LLM_MODEL_ID'):
            missing = [name for name in ('LLM_API_KEY', 'LLM_MODEL_ID')
                       if not environ.get(name)]
            raise RuntimeError(
                'Missing required configuration: ' + ', '.join(missing)
                + '. Set them in .env before starting Harness Agent.'
            ) from exc
        raise RuntimeError(str(exc)) from exc


# 会话列表摘要缓存：session_admin 经 _h() 读写（测试也会整体替换这个字典）。
_session_summary_cache = {}
_SESSION_SUMMARY_CACHE_MAX = 512


# ============================================================
# 全局配置集中区
# ============================================================

# ---------- 记忆系统配置 ----------
MEMORY_TYPES=['user','feedback','project','reference']
MEMORY_DIR=CACHE_DIR/'memories'
MEMORY_INDEX=MEMORY_DIR/'MEMORY.md'
CONSOLIDATE_THRESHOLD=10

# ---------- 安全策略 ----------
DENY_LIST=['sudo','shutdown','reboot','> /dev/']
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

def _configured_window(model: str) -> int | None:
    """新配置里声明的窗口；旧环境变量/内置表仍可按既有优先级覆盖。"""
    try:
        return llm_config.get_settings().context_window_for(model)
    except llm_config.ConfigError:
        return None


def _default_context_limit()->int:
    model=model_for('main')
    window=(os.getenv('LLM_CONTEXT_WINDOW')
            or _configured_window(model) or MODEL_WINDOWS.get(model))
    if window is None:
        # Codex 行为：模型信息表 + 告警，绝不静默降级（此前 deepseek-flash 缺注册
        # 曾静默回落 64K，压缩阈值被低估 16 倍）
        print(f'[ctx] 未知模型 {model!r}：按 64K 窗口保守处理，'
              f'可用 LLM_CONTEXT_WINDOW 显式配置', file=sys.stderr)
        window=65536
    return max(4000,int((int(window)-OUTPUT_RESERVE)*CTX_SAFETY))

CONTEXT_LIMIT=_default_context_limit()


def _context_window(model: str) -> int:
    """状态行显示用的真实窗口（不是压缩阈值 CONTEXT_LIMIT）。"""
    window = (os.getenv('LLM_CONTEXT_WINDOW')
              or _configured_window(model) or MODEL_WINDOWS.get(model))
    return int(window) if window else CONTEXT_LIMIT


def update_panel_status(messages=None, *, model=None, ctx_tokens=None,
                        ctx_source=None) -> None:
    """把当前用量/上下文占用发给 UI 状态行；无订阅者时是空操作。"""
    model = model or model_for('main')
    if ctx_tokens is None:
        ctx_tokens = estimate_size(messages) if messages else 0
        ctx_source = ctx_source or 'estimated'
    payload = status_payload(
        model=model, workdir=get_workdir(), ctx_tokens=int(ctx_tokens),
        ctx_source=ctx_source or 'confirmed', window=_context_window(model))
    payload['branch'] = current_branch(get_workdir())
    payload['sandbox'] = load_policy().sandbox_mode
    ui_emit('status', **payload)
KEEP_RECENT=6
PERSIST_THERSHOLD=10000
TOOL_RESULT_CHAR_LIMIT=int(os.getenv('HARNESS_TOOL_RESULT_CHARS') or 24_000)  # 工具输出入口截断线（≈7k tokens）

# ---------- 缓存目录 ----------
TOOL_RESULTS_DIR=CACHE_DIR/'tool_results'
SCREENSHOT_DIR=CACHE_DIR/'screenshots'
TRANSCRIPT_DIR=CACHE_DIR/'transcripts'
SESSIONS_DIR=CACHE_DIR/'sessions'

# ---------- Checkpoint / Rewind 配置 ----------
CHECKPOINT_DIR=CACHE_DIR/'checkpoints'
CHECKPOINT_MAX_NODES=20          # 保留最近 N 个节点（由 /checkpoint-gc 触发回收）
CHECKPOINT_KEEP_LABELED=True     # 带 label 的显式节点不被 GC
CHECKPOINT_CAPTURE_FILES=True    # 关闭后退化为纯会话回溯
CHECKPOINT_IGNORE=['.cache','.git','.worktrees','.mailbox','__pycache__','.pytest_cache']
CHECKPOINT_SECRETS=['.env','*.pem','*_rsa']
_checkpoint_manager = ContextVar('checkpoint_manager', default=None)


# ---------- 存储维护（启动后台执行一次，全部幂等）----------

def start_storage_maintenance(emit=print, *, purge_orphans=False) -> None:
    """后台跑一轮存储维护：迁移快照格式并预览孤儿子日志。

    旧快照在内容逐条校验后迁移到 gzip。孤儿日志默认只列出候选；传入
    ``purge_orphans=True`` 才会删除超过保留期且未被父会话引用的日志。
    """
    def _run():
        try:
            from checkpoints import migrate_legacy_checkpoints
            stats = migrate_legacy_checkpoints(CHECKPOINT_DIR, emit=emit)
            if stats['migrated'] or stats['failed']:
                emit(f"  [checkpoint] 旧快照迁移完成：压缩 {stats['migrated']}，"
                     f"跳过 {stats['skipped']}，失败 {stats['failed']}")
        except Exception as exc:
            emit(f'  [checkpoint] 旧快照迁移异常：{exc}')
        try:
            from session_cleanup import orphan_child_logs, purge_orphan_child_logs
            if purge_orphans:
                deleted, failures = purge_orphan_child_logs(SESSIONS_DIR, emit=emit)
                if deleted or failures:
                    extra = f'（{failures} 个删除失败）' if failures else ''
                    emit(f'  [cleanup] 已回收 {len(deleted)} 个孤儿子日志{extra}')
            else:
                candidates = orphan_child_logs(SESSIONS_DIR, emit=emit)
                if candidates:
                    emit(f'  [cleanup] 发现 {len(candidates)} 个过期孤儿子日志；'
                         '本次仅预览，未删除')
        except Exception as exc:
            emit(f'  [cleanup] 孤儿子日志维护异常：{exc}')
        try:
            from screen_capture import prune as prune_screenshots
            removed = prune_screenshots(SCREENSHOT_DIR)
            if removed:
                emit(f'  [cleanup] 已清理 {removed} 张超过保留期的截图')
        except Exception as exc:
            emit(f'  [cleanup] 截图保留维护异常：{exc}')

    threading.Thread(target=_run, name='storage-maintenance', daemon=True).start()

# ---------- 重试配置 ----------
MAX_REACTIVE_RETRIES=3   # 逐次缩尾重试（keep/budget 见 agent_loop 的 attempts），仍超限才 raise


_CACHE_DEBUG_SESSIONS = 32
_CACHE_DEBUG_COUNTS: dict[str, int] = {}
# 只保留消息摘要而不是整份请求：开关开着跑长会话时，内存不随历史增长。
_LAST_REQUESTS: dict[str, tuple[list[str], list[dict]]] = {}


def _message_head(message, limit: int = 80) -> dict:
    """调试记录里的一小段消息摘要：能认出是哪条，又不把长内容写进日志。"""
    content = str((message or {}).get('content') or '')
    return {'role': (message or {}).get('role', ''),
            'chars': len(content),
            'head': content[:limit]}


def _report_cache_prefix(session_id: str, request_messages: list,
                         model: str = '', *, system: str = '') -> None:
    """HARNESS_DEBUG_CACHE=1：相邻请求的「system + 历史」前缀断了就记录。

    只比较 system + 历史：`inject_memories` 把记忆块追加在请求末尾，它每轮的
    位置都不同——把它算进来，每次工具结果追加都会被误报成前缀改写（实测每一轮
    都会报一次）。真正的 `history_snipped` 会改动历史中部，仍然会被抓到。
    只观测、不改请求；stderr 之外再写一份 `.cache/cache_debug.jsonl`。

    `HARNESS_DEBUG_CACHE_SAMPLE=N`（默认 1）只对每 N 个请求采样；首个请求总会
    采样以建立基准。返回 None 表示本次未采样（调用方按“无诊断数据”处理）。
    """
    if not cache_debug_enabled():
        return
    every = cache_sample_every()
    count = _CACHE_DEBUG_COUNTS.get(session_id, 0) + 1
    _CACHE_DEBUG_COUNTS[session_id] = count
    if len(_CACHE_DEBUG_COUNTS) > _CACHE_DEBUG_SESSIONS:
        _CACHE_DEBUG_COUNTS.pop(next(iter(_CACHE_DEBUG_COUNTS)))
    # 采样：首个请求必测（建立基准），之后每 N 个测一次。跳过的请求不计算指纹，
    # 下一次采样与上次「已采样」请求比较——跨度变长，但仍能抓到中段改写。
    if every > 1 and count % every and count != 1:
        return None
    probe = ([{'role': 'system', 'content': system}] if system else [])
    probe.extend(request_messages)
    current = cache_fingerprints(probe)
    heads = [_message_head(message) for message in probe]
    previous = _LAST_REQUESTS.pop(session_id, None)
    _LAST_REQUESTS[session_id] = (current, heads)
    if len(_LAST_REQUESTS) > _CACHE_DEBUG_SESSIONS:
        _LAST_REQUESTS.pop(next(iter(_LAST_REQUESTS)))
    if previous is None:
        return {'cache_prefix_status': 'first',
                'cache_prefix_index': None,
                'cache_prefix_previous_messages': 0,
                'cache_prefix_current_messages': len(current)}
    previous_fingerprints, previous_heads = previous
    index = first_cache_divergence(previous_fingerprints, current)
    previous_len = len(previous_fingerprints)
    current_len = len(current)
    if index == previous_len == current_len:
        return {'cache_prefix_status': 'unchanged',
                'cache_prefix_index': index,
                'cache_prefix_previous_messages': previous_len,
                'cache_prefix_current_messages': current_len}
    if index == previous_len:
        return {'cache_prefix_status': 'appended',
                'cache_prefix_index': index,
                'cache_prefix_previous_messages': previous_len,
                'cache_prefix_current_messages': current_len}
    truncated = index >= current_len
    record = {
        'ts': time.time(),
        'session_id': session_id,
        'index': index,
        'previous_len': previous_len,
        'current_len': current_len,
        'truncated': truncated,
        'system_chars': len(system),
        'system_fingerprint': hashlib.sha1(
            (system or '').encode('utf-8', 'replace')).hexdigest()[:12],
        'model': model,
        'before': (previous_heads[index]
                   if index < len(previous_heads) else None),
        'after': None if truncated else heads[index],
    }
    print(f'  \033[33m[cache] prefix changed at msg {index} '
          f'({record["previous_len"]}→{record["current_len"]}, '
          f'system={record["system_chars"]} chars) session {session_id[:8]}\033[0m',
          file=sys.stderr)
    try:
        log_path = SESSIONS_DIR.parent / 'cache_debug.jsonl'
        with open(log_path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        pass
    return {'cache_prefix_status': 'truncated' if truncated else 'rewritten',
            'cache_prefix_index': index,
            'cache_prefix_previous_messages': previous_len,
            'cache_prefix_current_messages': current_len}


# 子代理与技能工具在这里接入统一注册表。
tool_registry['task']=spawn_subagent
tool_registry['load_skill']=load_skill

# compact 工具：让模型在上下文过长时主动触发历史摘要
# （不注册进 tool_registry，由 agent_loop 特殊处理）

# ============================================================
# 可复用模块：压缩预处理 / 记忆注入 / 工具执行
# 压缩原语（estimate_size/snip_compact/micro_compact/compact_history 等）
# 与记忆、hooks 已提取到 agent_core.py，此处只保留 agent_loop 的编排函数
# ============================================================

# 压缩逻辑，每轮开头的压缩预处理：micro → snip → budget →（仍超限）LLM 摘要。
# 返回 (压缩后的 messages, 新的 skip_micro_rounds)。
def pre_compact_messages(messages, skip_micro_rounds=0, on_compact=None, session_store=None,
                         size=None, snip_state=None):
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


ACTIVE_TICKER = None   # 交互回合进行中的进度行；工具活动行借它原子换行


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
    # todo 工具需要把计划写进当前会话事件流；manager 是唯一跨工具可取的会话锚点。
    manager.session_store = session_store
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


def parse_checkpoint_gc(argument: str) -> tuple[bool, int]:
    """解析 `/checkpoint-gc [--all] [--keep N]`，使用当前保留配置。"""
    return checkpoint_commands.parse_checkpoint_gc(
        argument, default_keep=CHECKPOINT_MAX_NODES)


def checkpoint_gc_preview(manager, argument: str) -> dict:
    """只读预览；TUI 先弹确认，行式 REPL 打印后再执行。"""
    return checkpoint_commands.checkpoint_gc_preview(
        manager, argument, base_dir=CHECKPOINT_DIR,
        default_keep=CHECKPOINT_MAX_NODES,
        keep_labeled=CHECKPOINT_KEEP_LABELED)


def checkpoint_gc_execute(manager, argument: str) -> dict:
    """执行已确认的回收；返回与预览同形的统计。调用方负责先预览。"""
    return checkpoint_commands.checkpoint_gc_execute(
        manager, argument, base_dir=CHECKPOINT_DIR,
        default_keep=CHECKPOINT_MAX_NODES,
        keep_labeled=CHECKPOINT_KEEP_LABELED)


def format_checkpoint_gc(summary: dict, *, done: bool = False) -> str:
    """把预览/执行结果格式化成 TUI 通知和行式 REPL 共用的一段文字。"""
    return checkpoint_commands.format_checkpoint_gc(
        summary, default_keep=CHECKPOINT_MAX_NODES, done=done)


def _confirm(prompt:str)->bool:
    try:
        return input(f"{prompt} (y/n) ").strip().lower() in ('y','yes')
    except (EOFError,KeyboardInterrupt):
        return False


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
                # 回合已结束，但另一个进程可能正在 rename/归档：title 是单次追加，
                # 用带超时的会话锁避免跨进程 seq 竞争；拿不到就跳过本轮标题。
                from session_locks import SessionBusy, session_write
                try:
                    with session_write(session_store.directory,
                                       session_store.session_id,
                                       timeout=5.0, action='写入标题'):
                        session_store.append_session_title(title, source='auto')
                except SessionBusy as exc:
                    print(f'  [33m[title] skipped: {exc}[0m')
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


def housekeeping_busy() -> bool:
    """后台整理是否仍在写会话日志；清理会话前必须为 False。"""
    with _housekeeping_lock:
        return any(thread.is_alive() for thread in _housekeeping_threads)


def housekeeping_pending() -> list[str]:
    """仍在运行的整理作业（线程名带会话 id，便于关闭时定位）。"""
    with _housekeeping_lock:
        _housekeeping_threads[:] = [thread for thread in _housekeeping_threads
                                    if thread.is_alive()]
        return [thread.name for thread in _housekeeping_threads]


def shutdown_housekeeping(timeout: float = 10.0) -> list[str]:
    """退出前等待后台整理；返回超时仍未结束的作业名并打印告警。

    与 ``wait_for_housekeeping`` 的区别是「收尾」：发生超时会留下可诊断的
    warning（线程名带会话 id），不会静默丢弃未写完的标题/记忆。"""
    deadline = time.time() + timeout
    with _housekeeping_lock:
        threads = list(_housekeeping_threads)
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.time()))
    pending = housekeeping_pending()
    if pending:
        shown = '、'.join(pending[:5]) + ('…' if len(pending) > 5 else '')
        print(f'  \033[33m[housekeeping] {len(pending)} 项未在 {timeout:g}s '
              f'内结束：{shown}\033[0m')
    return pending


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
    cache_write = getattr(usage, 'cache_creation_input_tokens', None)
    if cache_write is None and details is not None:
        cache_write = (getattr(details, 'cache_creation_tokens', None)
                       or getattr(details, 'cache_write_tokens', None))
    completion_details = getattr(usage, 'completion_tokens_details', None)
    reasoning = (getattr(completion_details, 'reasoning_tokens', None)
                 if completion_details is not None else None)
    if reasoning is None:
        reasoning = getattr(usage, 'reasoning_tokens', None)
    return {'prompt': prompt, 'completion': completion, 'total': total,
            'cached': int(cached or 0), 'cached_seen': cached is not None,
            'cache_write': int(cache_write or 0),
            'reasoning': int(reasoning or 0)}


def _cache_hit_rate_line(bucket: dict) -> str:
    """命中率后缀：供应商未回传 cached 字段时不显示（避免把「未知」当 0）。"""
    if not bucket.get('cached_seen') or not bucket.get('prompt'):
        return ''
    rate = bucket['cached'] / bucket['prompt']
    return f' · 缓存命中 {rate:.0%}'


def _stream_completion(create_kwargs, on_delta, api=None):
    """Collect a streamed completion while preserving the existing response shape."""
    _t0 = time.perf_counter()
    _ttfb_done = False
    api = api if api is not None else client
    # include_usage：流式默认不带 usage，显式索取后随收尾块返回（Deepseek/OpenAI 兼容）
    stream = api.chat.completions.create(**create_kwargs, stream=True,
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
    reasoning_chars = 0
    last_reasoning_note = 0.0
    for chunk in stream:
        usage = getattr(chunk, 'usage', None) or usage
        if not getattr(chunk, 'choices', None):
            continue   # include_usage 的收尾块只带 usage、choices 为空
        choice = chunk.choices[0]
        finish_reason = choice.finish_reason or finish_reason
        delta = choice.delta
        reasoning = getattr(delta, 'reasoning_content', None)
        if reasoning:
            # 推理模型先吐 reasoning、后吐正文：这段时间 UI 只有 Working，
            # 看起来像卡住。只暴露“思考中 + 字数”，不把推理内容写进会话/日志；
            # 同时在这里检查取消，否则思考阶段的 Esc 要等正文开始才生效。
            if turn_control.cancel_requested():
                raise KeyboardInterrupt
            reasoning_chars += len(reasoning)
            now = time.monotonic()
            if now - last_reasoning_note >= 0.5:
                last_reasoning_note = now
                ui_emit('activity', state='thinking',
                        detail=f'模型思考中（{reasoning_chars} 字）')
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


@dataclass
class _TurnState:
    """一次 agent_loop 调用的显式状态。

    之前这些值散落在 agent_loop 的局部变量里：压缩状态机、回合统计、快照基线与
    工作区都必须同进同退。收进对象后，跨迭代的可变状态只有这一处，请求执行与
    工具批处理可以按状态读写，不再靠闭包捕获。

    字段全部是「本回合内」状态；跨进程真相仍在 session_store 的事件流里。
    """

    messages: list
    session_store: SessionEventStore
    on_delta: object
    on_event: object
    steer_provider: object
    file_channel: object
    recovery_state: object
    context: dict
    system: str
    checkpoint_manager: CheckpointManager
    turn_workspace: Path
    memories_content: str
    memory_load_ms: float
    turn_cp_start: object
    turn_started: float = field(default_factory=time.monotonic)
    reactive_retries: int = 0
    skip_micro_rounds: int = 0
    rounds_since_todo: int = 0
    streamed_parts: list[str] = field(default_factory=list)
    turn_tokens: int = 0
    turn_usage: dict = field(default_factory=lambda: {
        'prompt': 0, 'completion': 0, 'cached': 0, 'total': 0,
        'reasoning': 0, 'cached_seen': False})
    ctx_tokens: int | None = None
    snip_state: dict = field(default_factory=dict)
    turn_no: int = 0
    memory_profile_pending: bool = True
    turn_baseline: object = None
    baseline_taken: bool = False


def _turn_cancelled(state: _TurnState) -> bool:
    """进程内取消位 + 跨进程 cancel 文件（另一个入口的 /interrupt）。"""
    if turn_control.cancel_requested():
        return True
    channel = state.file_channel
    return bool(channel is not None and channel.cancel_requested())


def _drain_steering(state: _TurnState) -> bool:
    """把 TUI 运行中的 steer 消息注入当前上下文（下一轮请求前）。"""
    if state.steer_provider is None:
        return False
    try:
        texts = state.steer_provider() or []
    except Exception:
        return False
    injected = False
    for text in texts:
        if not text:
            continue
        message = {'role': 'user', 'content': str(text)}
        state.messages.append(message)
        state.session_store.append_message(message)
        ui_emit('user_message', text=str(text))
        injected = True
    return injected


def _system_fingerprint(system: str) -> str:
    return hashlib.sha1((system or '').encode('utf-8', 'replace')).hexdigest()[:12]


def _run_llm_request(state: _TurnState, request_messages: list, profile_timing: dict,
                     stream_probe: dict, request_started: float):
    """执行一次（含内部重试的）模型请求，流式增量直接写回状态与 UI。"""
    def _create():
        max_tokens = (recovery.ESCALATED_MAX_TOKENS
                      if state.recovery_state.has_escalated
                      else recovery.DEFAULT_MAX_TOKENS)
        target = getattr(state.recovery_state, 'current_target', None)
        if target is None:
            try:
                target = model_target('main')
            except llm_config.ConfigError:
                target = None
        if target is not None:
            selected_model = target.model
            api = client_for('main', target)
        else:
            selected_model = state.recovery_state.current_model or os.getenv("LLM_MODEL_ID")
            api = client
        state.recovery_state.stream_started = False
        kwargs = dict(
            model=selected_model,
            messages=request_messages,
            tools=TOOLS,
            temperature=0.7,
            max_tokens=max_tokens,
        )
        if state.on_delta is None and (not streaming_enabled()
                                       or (target is not None
                                           and not target.supports('stream'))):
            return api.chat.completions.create(**kwargs)

        def _delta_hook(part):
            if 'ttfb_ms' not in profile_timing:
                profile_timing['ttfb_ms'] = round(
                    (time.monotonic() - request_started) * 1000, 3)
            if _turn_cancelled(state):
                raise KeyboardInterrupt
            # 已经向用户交付过文本：重试会把同一段再放一遍，recovery 据此拒绝重放
            state.recovery_state.stream_started = True
            if not stream_probe['started']:
                stream_probe['started'] = True
                ui_emit('assistant_start', mid=stream_probe['mid'])
            ui_emit('assistant_delta', mid=stream_probe['mid'], delta=part)
            state.streamed_parts.append(part)
            if state.on_delta is not None:
                state.on_delta(part)

        state.streamed_parts.clear()   # 标记只反映最近一次调用
        stream_t0 = time.monotonic()
        try:
            if api is client:   # 保持测试对 _stream_completion(kwargs, hook) 的 patch 签名
                response = _stream_completion(kwargs, _delta_hook)
            else:
                response = _stream_completion(kwargs, _delta_hook, api=api)
        except Exception:
            # 已经开过流就把 UI 的 streaming 状态收尾，否则气泡会一直转圈
            if stream_probe['started']:
                ui_emit('assistant_end', mid=stream_probe['mid'])
            raise
        profile_timing['stream_ms'] = round(
            (time.monotonic() - stream_t0) * 1000, 3)
        return response

    with perf_segment('llm.main'):
        return recovery.with_retry(_create, state.recovery_state)


def _record_request_result(state: _TurnState, response, *, profile_timing: dict,
                           request_started: float, request_ms: float,
                           prompt_message_count: int) -> None:
    """记录成功请求：usage 事件、回合累计与状态行。"""
    detail = _parse_usage(getattr(response, 'usage', None))
    if detail is None:
        completed_profile = {
            **profile_timing,
            'request_ms': request_ms,
            'ttfb_ms': profile_timing.get('ttfb_ms'),
            'prompt_message_count': prompt_message_count,
            'usage_available': False,
            'model': active_model(state.recovery_state),
            'system_fp': _system_fingerprint(state.system),
            'tools_fp': _tools_fingerprint(),
            'finish_reason': getattr(response.choices[0], 'finish_reason', None),
        }
        if state.memory_profile_pending:
            completed_profile['memory_load_ms'] = state.memory_load_ms
            state.memory_profile_pending = False
        state.session_store.append('model_request_completed', **completed_profile)
        return
    # 归因字段：缓存跌破到底是换了模型、system 改了，还是工具表变了。
    detail['model'] = active_model(state.recovery_state)
    detail['system_fp'] = _system_fingerprint(state.system)
    detail['tools_fp'] = _tools_fingerprint()
    detail.update(profile_timing)
    if state.memory_profile_pending:
        detail['memory_load_ms'] = state.memory_load_ms
        state.memory_profile_pending = False
    detail['request_ms'] = request_ms
    detail.setdefault('ttfb_ms', None)
    detail['prompt_message_count'] = prompt_message_count
    state.turn_tokens += detail['total']
    for key in ('prompt', 'completion', 'cached', 'total', 'reasoning'):
        state.turn_usage[key] += detail[key]
    state.turn_usage['cached_seen'] = (
        state.turn_usage['cached_seen'] or detail['cached_seen'])
    # 埋点：逐调用 usage 事件（供应商权威值；命中率可逐调用分析，
    # 聚合口径由 usage_totals() 求和保持不变）
    state.session_store.append('usage', **detail)
    state.ctx_tokens = detail['prompt']   # 压缩判据锚点：本请求真实大小
    METRICS.record_usage(detail)
    ui_emit('round_end', round=state.turn_no,
            prompt_tokens=detail['prompt'],
            completion_tokens=detail['completion'],
            total_tokens=detail['total'],
            cached_tokens=detail['cached'],
            reasoning_tokens=detail.get('reasoning', 0),
            cost=str(METRICS.cost),
            cost_source=METRICS.cost_source,
            duration_ms=round((time.monotonic() - request_started) * 1000),
            compressed=False)
    update_panel_status(state.messages, model=detail['model'],
                        ctx_tokens=detail['prompt'],
                        ctx_source='confirmed')


def _record_request_failure(state: _TurnState, exc, *, profile_timing: dict,
                            request_ms: float, prompt_message_count: int) -> None:
    """记录失败请求画像；是否重试由调用方按异常类型决定。"""
    response = getattr(exc, 'response', None)
    status_code = (getattr(exc, 'status_code', None)
                   or getattr(response, 'status_code', None))
    failure_profile = {
        **profile_timing,
        'request_ms': request_ms,
        'error_type': type(exc).__name__,
        'provider_status': status_code,
        'prompt_too_long': recovery.is_prompt_too_long_error(exc),
        'prompt_message_count': prompt_message_count,
    }
    if state.memory_profile_pending:
        failure_profile['memory_load_ms'] = state.memory_load_ms
        state.memory_profile_pending = False
    state.session_store.append('model_request_failed', **failure_profile)


def _continue_after_length(state: _TurnState, response) -> bool:
    """输出被截断时升级 max_tokens 并续写；返回 True 表示回到主循环。"""
    if (response.choices[0].finish_reason != 'length'
            or state.recovery_state.has_escalated):
        return False
    state.recovery_state.has_escalated = True
    print('  [length] escalating max_tokens and requesting continuation')
    completed = SessionEventStore.close_interrupted_tools(state.messages)
    for tool_result in completed[len(state.messages):]:
        state.session_store.append_message(tool_result)
    state.messages[:] = completed
    continuation = {'role': 'user', 'content': recovery.CONTINUATION_PROMPT}
    state.messages.append(continuation)
    state.session_store.append_message(continuation)
    if state.ctx_tokens is not None:
        state.ctx_tokens += estimate_size([continuation])
    return True


def _append_turn_terminal(session_store, token, status: str, *,
                          error_type: str | None = None,
                          detail: str | None = None) -> dict:
    """记录回合终态；这是 watcher/通知该消费的唯一 `turn_terminal` 事件。

    ``turn_completed`` 保留给既有消费者（网关进度、测试），终态事件紧跟其后，
    一次成功回合因此只会有一个 ``turn_terminal(status='completed')``。
    """
    payload = {'status': status}
    if token is not None:
        payload['turn_id'] = token.turn_id
        payload['source'] = token.source
    if error_type:
        payload['error_type'] = error_type
    if detail:
        payload['detail'] = str(detail)[:200]
    return session_store.append('turn_terminal', **payload)


def _finish_response(state: _TurnState, message, *, pre_compress: list,
                     housekeeping: bool) -> str | None:
    """收尾一个非工具回复；返回 None 表示本轮还要继续（steer / Stop 注入）。"""
    if _drain_steering(state):
        return None
    force = trigger_hooks('Stop', state.messages)
    if force:
        state.messages.append({'role': 'user', 'content': force})
        if state.ctx_tokens is not None:
            state.ctx_tokens += estimate_size([state.messages[-1]])
        return None
    if message.content:
        if state.on_delta is not None or state.streamed_parts:
            print()   # 调用方已逐段交付文本，只补换行，避免重复打印
        else:
            print(message.content)
    if state.turn_tokens:
        cum = state.session_store.usage_totals()   # 逐调用 usage 事件聚合
        from turn_progress import format_duration
        ctx_note = ''
        if state.ctx_tokens is not None:
            window = _context_window(active_model(state.recovery_state))
            ctx_note = f' · ctx {min(100, 100 * state.ctx_tokens // window)}%'
        print(f'  [tokens] 本轮 {_fmt_tokens(state.turn_tokens)} · '
              f'会话累计 {_fmt_tokens(cum["total"])}'
              f' · 用时 {format_duration(time.monotonic() - state.turn_started)}'
              f'{ctx_note}'
              f'{_cache_hit_rate_line(cum)}')
    write_transcript(state.messages)   # 正常结束也落盘，便于复盘
    _safe_checkpoint(state.checkpoint_manager.seal)   # 固化本节点：记录改动文件写入后的 hash
    # 回合结束清单：Git 基线能看见 bash 造成的改动；非 Git 工作区回退到
    # 本回合 checkpoint 记录的 write/edit/apply_patch 文件。
    fallback = _safe_checkpoint(
        state.checkpoint_manager.changed_files_since, state.turn_cp_start) or {}
    try:
        changes = [] if not state.baseline_taken else workspace_diff.turn_changes(
            state.turn_workspace, state.turn_baseline, fallback, CHECKPOINT_IGNORE)
    except Exception as exc:   # 汇总失败不影响本轮结果
        print(f'  [diff] 无法汇总本轮改动：{exc}')
        changes = []
    if changes:
        print(workspace_diff.format_changes(changes))
        state.session_store.append('files_changed', files=[
            {'code': entry['code'], 'path': entry['path'],
             'old': entry.get('old')}
            for entry in changes
        ])
    token = turn_control.current_token()
    state.session_store.append('turn_completed', **(
        {'turn_id': token.turn_id, 'source': token.source} if token else {}))
    _append_turn_terminal(state.session_store, token, 'completed')
    # 标题/记忆提炼移入后台线程（_post_turn_housekeeping）：回复立即返回，
    # 副通道各自的 LLM 调用不再阻塞端到端延迟。快照防后续轮次改写。
    # 无头一次性进程没有「下一次启动」可等，跑这些线程只会被退出截断，
    # 所以由调用方显式关闭。
    if housekeeping:
        state.turn_no += 1
        housekeeping_thread = threading.Thread(
            target=_post_turn_housekeeping,
            args=(pre_compress, list(state.messages), state.session_store, state.turn_no),
            name=f'housekeeping:{state.session_store.session_id}:{state.turn_no}',
            daemon=True,
        )
        with _housekeeping_lock:
            # 只清理「已启动且已死」的旧线程——is_alive() 对未 start 的线程
            # 也返回 False，必须先清理再注册，否则会把本线程误删
            _housekeeping_threads[:] = [th for th in _housekeeping_threads
                                        if th.is_alive()]
            _housekeeping_threads.append(housekeeping_thread)
        housekeeping_thread.start()
    ui_emit('session_end', summary=METRICS.snapshot())
    ui_emit('activity', state='complete', detail='Response complete')
    return message.content or "No output"


def _run_tool_calls(state: _TurnState, message) -> bool:
    """执行一条 assistant 消息里的全部 tool_calls；返回 True 表示历史已被压缩。"""
    tool_messages: list[dict] = []
    compacted = False
    used_todo = False
    batch: list = []

    def _flush_batch() -> None:
        # 只在本轮内同步调用：batch/tool_messages 不跨轮存活
        if batch:
            if not state.baseline_taken:   # 首个工具执行前记录工作区基线
                state.turn_baseline = workspace_diff.snapshot(
                    state.turn_workspace, CHECKPOINT_IGNORE)
                state.baseline_taken = True
            # 批级合并审查：批内多条会 ASK 的 bash 打包成一次 LLM 审查，
            # 放行结果进缓存（enforce 单条路径零开销命中）；升级的照旧问人
            hooks.preflight_judge(batch)
            _run_tool_batch(batch, state.session_store, tool_messages,
                            on_event=state.on_event)
            ui_emit('activity', state='thinking', detail='Reviewing tool result')
            if state.ctx_tokens is not None:   # 只复用已知用量
                update_panel_status(state.messages, ctx_tokens=state.ctx_tokens,
                                    ctx_source='confirmed')
        batch.clear()

    for tool_call in message.tool_calls:
        name = tool_call.function.name
        if name == 'todo_write':
            used_todo = True
        if name == 'checkpoint':
            # 当前节点在本轮请求前已创建，快照是完整 OpenAI 历史。只给它加标签，不能
            # 在 assistant tool_calls 与对应 tool 结果之间新建快照，否则 rewind 后历史无法重放。
            # 与 compact 一样不进 tool_registry —— 它是 loop 行为，不是普通工具。
            _flush_batch()
            try:
                label = str(json.loads(tool_call.function.arguments or '{}').get('label', ''))
            except json.JSONDecodeError:
                label = ''
            cp_id = _safe_checkpoint(state.checkpoint_manager.label_active, label,
                                     state.session_store, trigger='manual')
            tool_messages.append({
                'role': 'tool', 'tool_call_id': tool_call.id,
                'content': (f'Checkpoint created: {cp_id}' if cp_id
                            else 'Checkpoint failed; continue without one.'),
            })
            state.session_store.append_message(tool_messages[-1])
            continue
        if name == 'compact':
            # compact_history 会替换整个上下文，原 assistant 的 tool_calls 被删除，
            # 之前已执行的 tool 结果不能以 role=tool 保留（会变成 orphan），
            # 改为文本附到新上下文里，避免执行结果丢失。
            _flush_batch()
            state.messages[:] = compact_history(state.messages)
            _record_compaction(state.session_store, state.messages, 'tool')
            state.skip_micro_rounds = 3
            note = '[Compacted] Conversation history has been summarized.'
            if tool_messages:
                done = '\n'.join(f"- {str(tm['content'])[:1000]}" for tm in tool_messages)
                note += '\n\nTool results executed before compaction:\n' + done
            state.messages.append({'role': 'user', 'content': note})
            state.session_store.append_message(state.messages[-1])
            compacted = True
            if state.ctx_tokens is not None:
                state.ctx_tokens = estimate_size(state.messages)   # 整体替换历史后重估
            break
        batch.append(tool_call)
    _flush_batch()

    if compacted:   # 压缩后工具丢失，需要重新执行
        return True

    notifications = collect_background_results()
    if notifications:
        for notification in notifications:
            # 后台任务完成通知：作为 user 消息注入（孤儿 role=tool 会触发 API 400）
            tool_messages.append({'role': 'user', 'content': notification})
            state.session_store.append_message(tool_messages[-1])
        print(f"  \033[32m[inject] {len(notifications)} background "
              f"notification(s)\033[0m")

    state.messages.extend(tool_messages)
    if state.ctx_tokens is not None and tool_messages:   # 增量：新增工具结果（已入口截断，单条有界）
        state.ctx_tokens += estimate_size(tool_messages)
    state.rounds_since_todo = 0 if used_todo else state.rounds_since_todo + 1
    state.context = update_context(state.context, state.messages)
    state.system = get_system_prompt(state.context)   # 工具面/工作区可能已变
    return False


@_isolated_checkpoint_context()
def agent_loop(messages:list, session_store=None, on_delta=None, *,
               on_event=None, housekeeping=True, steer_provider=None,
               turn_source='cli', turn_id=None):
    """Run one OpenAI session, recording an append-only event stream for recovery.

    ``on_event`` mirrors tool execution for one-shot transports; ``housekeeping``
    can be disabled by short-lived processes that would otherwise start title /
    memory LLM calls in daemon threads and die before they finish.
    """
    perf_reset()   # 分段计时清零（HARNESS_PERF=1 时生效）
    # 污点是回合级状态：上回合读过密钥，不该让本回合每一次联网都变红（那样
    # 几次之后用户就会无脑点通过，比不做更糟）。
    taint.reset()
    if session_store is None:
        session_store = SessionEventStore(SESSIONS_DIR)
        for initial_message in messages:
            session_store.append_message(initial_message)
        print(f'[session log:{session_store.path}]')
    session_store.record_workspace(get_workdir())
    token = turn_control.begin_turn(session_id=session_store.session_id,
                                    turn_id=turn_id or '',
                                    source=turn_source)
    session_store.append('turn_started', turn_id=token.turn_id,
                         source=token.source)
    try:
        channel = turn_files.TurnChannel(SESSIONS_DIR.parent,
                                         session_store.session_id, token).start()
    except BaseException:
        turn_control.end_turn(token)   # 启动失败也要释放 token，不能让会话卡成“有回合”
        raise
    wait_for_mcp_tools()   # 交互模式：MCP 在后台连接，首轮请求前确保工具面完整
    set_session(session_store.session_id)
    set_agent('lead')   # 子代理会临时覆盖，退出后恢复
    context = update_context({}, messages)   # 循环前更新上下文，包含所有对话记录
    if messages:
        trigger_hooks('UserPromptSubmit', messages[-1]['content'])
    system = get_system_prompt(context)
    turn_started = time.monotonic()   # 本回合起始（tokens 行显示总用时）
    # 记忆加载：整个 Loop 只在此处加载一次（LLM 从 .cache/memories 目录筛选相关记忆）。
    # ⚠️ 提醒：循环中途不会再次刷新 —— 本轮新产生的信息不会进入 memories_content；
    #    要等会话结束 extract_memories 落盘后，下一次会话的 load_memories 才能召回。
    memory_load_started = time.perf_counter()
    with perf_segment('mem.load'):
        memories_content = load_memories(messages)   # 相关记忆块（空串 = 无相关记忆）
    memory_load_ms = round((time.perf_counter() - memory_load_started) * 1000, 3)
    checkpoint_manager = get_checkpoint_manager(session_store)
    _checkpoint_manager.set(checkpoint_manager)  # Only this turn's tools capture pre-images.
    restore_todos(session_store)   # 会话恢复后 todo 仍可读（事件流是唯一持久源）
    def combined_steer():
        texts = []
        if steer_provider is not None:
            try:
                texts.extend(steer_provider() or [])
            except Exception:
                pass
        texts.extend(channel.drain_steer())
        return texts

    state = _TurnState(
        messages=messages,
        session_store=session_store,
        on_delta=on_delta,
        on_event=on_event,
        steer_provider=combined_steer if steer_provider is not None else channel.drain_steer,
        file_channel=channel,
        recovery_state=recovery.RecoveryState(),
        context=context,
        system=system,
        checkpoint_manager=checkpoint_manager,
        turn_workspace=get_workdir(),
        memories_content=memories_content,
        memory_load_ms=memory_load_ms,
        turn_started=turn_started,
        # 基线延迟到首个工具批次：纯文本回合不需要扫一遍 git status。
        turn_cp_start=_safe_checkpoint(checkpoint_manager.head),
    )

    outcome = {'status': 'completed'}
    try:
        while True:
            if _turn_cancelled(state):   # TUI/REPL 的 Esc、Ctrl-C 取消
                raise KeyboardInterrupt
            _drain_steering(state)
            # 每轮开头留一个可回溯节点：快照必须在 pre_compact 重新绑定 messages 之前取
            _safe_checkpoint(checkpoint_manager.begin_turn, state.messages, session_store)

            # 压缩前的快照：纯净对话（不含 cron/提醒），供会话结束提炼记忆
            pre_compress = [m if isinstance(m, dict)
                            else {'role': m.get('role', ''), 'content': str(m.get('content', ''))}
                            for m in state.messages]

            compact_started = time.perf_counter()
            state.messages, state.skip_micro_rounds = pre_compact_messages(
                state.messages, state.skip_micro_rounds,
                on_compact=lambda compacted, strategy: _record_compaction(session_store, compacted, strategy),
                session_store=session_store,
                size=state.ctx_tokens,
                snip_state=state.snip_state,
            ) #压缩预处理流程（判据优先用真实用量，见 pre_compact_messages）
            compact_ms = round((time.perf_counter() - compact_started) * 1000, 3)

            if state.rounds_since_todo >= 8 and state.messages:
                reminder = {
                    'role': 'user',
                    'content': "<reminder>Update your todos.</reminder>",
                }
                state.messages.append(reminder)
                session_store.append_message(reminder)
                state.rounds_since_todo = 0

            fired = _consume_cron_for_running_turn()   # 只取心跳与显式可写任务
            for job in fired:
                cron_message = {
                    'role': 'user',
                    'content': f'[Scheduled] {job.prompt}',
                }
                state.messages.append(cron_message)
                session_store.append_message(cron_message)
                print(f"  \033[35m[inject cron] {job.prompt[:50]}\033[0m")

            # 记忆注入 + system 前置（副本不污染 messages；SDK 不支持 system= 关键字参数）
            # 图片引用在同一副本里展开成 data URI：历史/事件流仍只存小引用，
            # 每轮请求现读缓存文件（见 agent_core.expand_image_parts）。
            prompt_assembly_started = time.perf_counter()
            request_messages = expand_image_parts(inject_memories(
                state.messages, state.memories_content, state.system,
                environment=environment_message()))
            prompt_assembly_ms = round(
                (time.perf_counter() - prompt_assembly_started) * 1000, 3)
            prompt_message_count = len(request_messages)
            prepare_ms = compact_ms + prompt_assembly_ms
            # 诊断只看 system + 历史：记忆块在请求尾部，每轮位置都变，不参与比较。
            cache_prefix_profile = _report_cache_prefix(
                session_store.session_id, state.messages,
                active_model(state.recovery_state),
                system=state.system) or {}

            request_started = time.monotonic()
            profile_timing = {'prepare_ms': prepare_ms,
                              'compact_ms': compact_ms,
                              'prompt_assembly_ms': prompt_assembly_ms,
                              **cache_prefix_profile}
            stream_probe = {'started': False, 'mid': f'm{time.time_ns()}'}
            ui_emit('activity', state='thinking', detail='Requesting model response')

            try:   # 错误恢复逻辑
                response = _run_llm_request(state, request_messages, profile_timing,
                                            stream_probe, request_started)
                state.reactive_retries = 0
                request_ms = round((time.monotonic() - request_started) * 1000, 3)
                _record_request_result(
                    state, response, profile_timing=profile_timing,
                    request_started=request_started, request_ms=request_ms,
                    prompt_message_count=prompt_message_count)
            except Exception as e:
                request_ms = round((time.monotonic() - request_started) * 1000, 3)
                _record_request_failure(
                    state, e, profile_timing=profile_timing,
                    request_ms=request_ms, prompt_message_count=prompt_message_count)
                if (recovery.is_prompt_too_long_error(e)
                        and state.reactive_retries < MAX_REACTIVE_RETRIES):
                    # 逐次缩尾：keep/budget 收紧（500KB 尾部单靠条数压不住）
                    attempts = ((6, None), (3, 12_000), (1, 4_000))
                    keep, budget = attempts[min(state.reactive_retries, len(attempts) - 1)]
                    state.reactive_retries += 1
                    print(f'  [compact] 请求超限，已自动压缩历史并重试'
                          f'（第 {state.reactive_retries}/{MAX_REACTIVE_RETRIES} 次，保留 {keep} 条）')
                    state.messages[:] = reactive_compact(
                        state.messages, keep=keep, budget_tokens=budget)
                    _record_compaction(session_store, state.messages, 'reactive')
                    state.skip_micro_rounds = 3
                    if state.ctx_tokens is not None:
                        state.ctx_tokens = estimate_size(state.messages)   # 整体替换历史后重估
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
            state.messages.append(assistant_message)   # 添加模型回复到上下文
            session_store.append_message(assistant_message)
            if stream_probe['started']:
                ui_emit('assistant_end', mid=stream_probe['mid'])
            elif assistant_message.get('content'):
                ui_emit('assistant_text', text=str(assistant_message['content']))
            if state.ctx_tokens is not None:   # 增量：只有新增部分走估算（assistant 回复较小）
                state.ctx_tokens += estimate_size([assistant_message])

            # 输出被截断：升级到加大 max_tokens 并提示续写
            if _continue_after_length(state, response):
                continue

            if response.choices[0].finish_reason != "tool_calls":
                result = _finish_response(state, message, pre_compress=pre_compress,
                                          housekeeping=housekeeping)
                if result is None:
                    continue
                return result


            # compact 工具、checkpoint 标签与普通工具批次的顺序处理都在 _run_tool_calls；
            # 返回 True 表示历史已被 compact，需要重开一轮。
            if _run_tool_calls(state, message):
                continue


    except BaseException as exc:
        outcome['status'] = ('cancelled' if isinstance(exc, KeyboardInterrupt)
                             else 'failed')
        outcome['error_type'] = type(exc).__name__
        raise
    finally:
        if outcome['status'] != 'completed':
            _append_turn_terminal(session_store, token, outcome['status'],
                                  error_type=outcome.get('error_type'))
        try:
            channel.close()
        finally:
            turn_control.end_turn(token)


def resume_agent_loop(session_path: Path):
    """Resume a persisted OpenAI session without automatically repeating pending tools."""
    session_store = SessionEventStore.open(session_path)
    # 先取未闭合的调用再 recover：recover_pending 会写入 tool_recovery 事件，
    # 之后 pending_tool_calls() 看到的就是已闭合的空集合。
    recovery_message = session_store.recovery_message()
    messages = session_store.rebuild_openai_history(recover_pending=True)
    if recovery_message:
        messages.append(recovery_message)
        session_store.append_message(recovery_message)
    return agent_loop(messages, session_store=session_store)


_mcp_init_lock = threading.Lock()
_mcp_initialized = False
_mcp_ready = threading.Event()     # 连接/注册完成（成功或失败都置位：不让首条消息永久等待）
_mcp_async_started = threading.Event()


def initialize_mcp_tools() -> None:
    """Register configured MCP tools once for all adapters in this process."""
    global _mcp_initialized
    with _mcp_init_lock:
        if _mcp_initialized:
            _mcp_ready.set()
            return
        try:
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
        finally:
            # 失败也放行：首条消息最多等 wait_for_mcp_tools 的超时，不会卡死。
            _mcp_ready.set()


def start_mcp_initialization_async() -> None:
    """交互模式下把 MCP 连接放到后台：先把界面交给用户，首条消息前再等它。"""
    if _mcp_async_started.is_set():
        return
    _mcp_async_started.set()
    threading.Thread(target=initialize_mcp_tools,
                     name='mcp-init', daemon=True).start()


def wait_for_mcp_tools(timeout: float = 10.0) -> bool:
    """等待后台 MCP 初始化；仅当显式用异步启动过才可能阻塞。"""
    if _mcp_initialized:
        return True
    if not _mcp_async_started.is_set():
        return _mcp_initialized
    return _mcp_ready.wait(timeout)


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
        workspace=get_workdir(),
    )


def _run_heartbeat_jobs(jobs) -> None:
    """本地生成心跳摘要；失败只告警（摘要不能拖垮回合）。"""
    for job in jobs:
        try:
            heartbeat.run_due(job, workspace=get_workdir())
        except Exception as exc:
            print(f'  \033[31m[heartbeat] failed: '
                  f'{type(exc).__name__}: {exc}\033[0m')


def _consume_cron_for_running_turn():
    """交互回合执行中消费 cron：只取心跳与创建时显式允许副作用的任务。

    只读任务留在队列里，等专用 cron 回合按 read-only 策略跑 —— 当前回合的写
    权限属于正在操作的用户，不属于那份任务配置。心跳是本地只读摘要，随时可跑。
    当前回合本身就是只读定时回合时，也不再吞别的任务（避免可写任务被注入到
    只读上下文里执行）。
    """
    if hooks.scheduled_read_only():
        _run_heartbeat_jobs(
            consume_cron_queue(lambda job: job.id == heartbeat.JOB_ID))
        return []
    # 带投递目标的任务必须走自己的 cron 回合：答案要单独推回来源聊天，
    # 不能混进当前交互回合（比如用户在 TUI 打字时顺手跑掉飞书的任务）。
    fired = consume_cron_queue(
        lambda job: job.id == heartbeat.JOB_ID
        or (bool(getattr(job, 'allow_side_effects', False))
            and not getattr(job, 'deliver_to', '')))
    _run_heartbeat_jobs(job for job in fired if job.id == heartbeat.JOB_ID)
    return [job for job in fired if job.id != heartbeat.JOB_ID]


def _deliver_cron_outcome(jobs, answer=None, error=None) -> None:
    """把定时回合的最终答复/失败原因放进投递 outbox（同一聊天合并一条）。"""
    targets: dict[str, list[str]] = {}
    for job in jobs:
        chat_id = str(getattr(job, 'deliver_to', '') or '').strip()
        if chat_id:
            targets.setdefault(chat_id, []).append(job.id)
    if not targets:
        return
    occurrence = ','.join(sorted({str(getattr(job, 'last_fired', '') or '')
                                  for job in jobs}))
    for chat_id, job_ids in targets.items():
        ids = sorted(set(job_ids))
        text = (cron_delivery.failure_text(ids, error)
                if error is not None
                else cron_delivery.result_text(ids, answer or ''))
        try:
            cron_delivery.queue(chat_id, text, job_id=','.join(ids),
                                occurrence=occurrence)
        except Exception as exc:   # 投递失败不能反过来弄丢回合结果
            print(f'  \033[31m[cron] delivery queue failed: '
                  f'{type(exc).__name__}: {exc}\033[0m')


def run_agent_turn_locked():
    """持锁状态下执行一轮对话（供 queue_processor_loop 调用）：
    消费 cron 队列 → 构造 user 消息 → agent_loop。

    先拿全局回合槽再消费队列：槽被 TUI/API 占用时直接返回，任务留在队列里，
    不能先清掉队列再执行失败。
    """
    from turn_coordinator import TurnBusy, turn_slot

    try:
        slot = turn_slot(Path(SESSIONS_DIR).parent)
        with slot:
            fired = consume_cron_queue()
            if not fired:
                return
            # 默认心跳在本地生成只读摘要，不经过模型：拦截掉它的 prompt，避免
            # 一次巡检变成 AI 回合（占全局回合槽 + 不可预测的写入口）。
            heartbeats = [job for job in fired if job.id == heartbeat.JOB_ID]
            _run_heartbeat_jobs(heartbeats)
            fired = [job for job in fired if job.id != heartbeat.JOB_ID]
            if not fired:
                return
            prompts = [f'[Scheduled] {job.prompt}' for job in fired]
            # 一批任务按最严档执行：只要混入一个默认只读任务，整批都只读。
            # 同一分钟混跑只读与可写任务很少见，保守处理优于让只读 prompt
            # 借同批可写任务的授权执行。
            read_only = not all(getattr(job, 'allow_side_effects', False)
                                for job in fired)
            mode = 'read-only' if read_only else 'side-effects'
            print(f"  \033[35m[cron turn] {len(fired)} scheduled job(s) "
                  f"[{mode}]\033[0m")
            answer = None
            error = None
            interrupted = False
            try:
                with hooks.use_scheduled_turn(read_only=read_only):
                    answer = agent_loop(
                        [{'role': 'user', 'content': '\n'.join(prompts)}],
                        turn_source='cron')
            except KeyboardInterrupt:
                interrupted = True
                print('\n[会话已中断]')
            except Exception as exc:
                # 后台回合失败只影响这一批任务：worker 线程必须活下来，否则
                # 一次模型/网络错误之后所有定时任务都静默停摆。
                error = exc
                print(f'  \033[31m[cron] turn failed: '
                      f'{type(exc).__name__}: {exc}\033[0m')
            finally:
                if not interrupted:
                    _deliver_cron_outcome(fired, answer, error)
    except TurnBusy:
        return   # 别的入口在跑：本轮跳过，任务仍在 cron_queue






def run_repl(initial_messages:list, session_store=None):
    """最小交互循环：普通输入走 agent_loop，/ 开头的走 checkpoint 命令（见 handle_command）。

    agent_loop 每轮结束就返回，canonical history 由 session_store 维护，
    因此每轮结束后用 rebuild_openai_history() 刷新本地 messages —— 这样 /rewind 与多轮对话共用同一份历史。
    """
    METRICS.reset()
    ui_emit('session_start', prompt='')
    messages=[dict(m) for m in initial_messages]
    if session_store is None:
        session_store=SessionEventStore(SESSIONS_DIR)
        session_store.record_workspace(get_workdir())
        for message in messages:
            session_store.append_message(message)
    else:
        workspace = session_store.session_summary()['workspace']
        if workspace:
            set_workspace(workspace)
    ui_mode = os.getenv('HARNESS_UI', 'textual')
    if ui_mode in ('textual', 'textual-next') and sys.stdin.isatty():
        # textual 是默认入口；textual-next 保留为旧环境变量兼容别名。
        try:
            module = __import__('ui.harness_app', fromlist=['run'])
        except Exception as exc:   # 缺 textual/渲染失败：回退行式 REPL
            print(f'[ui] Textual 不可用，回退 ANSI：{exc}')
        else:
            try:
                module.run(messages,
                           session_id=session_store.session_id,
                           title=session_store.session_summary()['title'],
                           session_store=session_store)
                return
            except Exception as exc:   # 渲染驱动/终端不兼容：回退行式
                print(f'[ui] Textual 运行失败，回退行式 REPL：'
                      f'{type(exc).__name__}: {exc}')

    with agent_lock:
        run_line_repl(messages, session_store)



if __name__ == '__main__':
    # 保留 python harness.py 入口；真正的进程入口放在可测试、可独立复用的模块里。
    from entrypoint import main

    main()
