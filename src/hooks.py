"""Hook 回调：权限 / 日志 / 上下文注入 / 总结。注册表在 agent_core。
可变全局（DENY_LIST/DESTRUCTIVE/WORKDIR）由 harness 持有，经 _h() 读取。

执行策略的唯一入口是 enforce()：策略层判定（exec_policy）→ 会话白名单 → 审批 →
凭据。CLI 的 PreToolUse hook、工具边界（run_bash）、teammate、后台任务与 HTTP
任务都经过它，区别只在 interactive —— 即调用线程能不能问人。

审批门在 approval.py，LLM 审查官在 approval_judge.py；本模块持有会话上下文与
审批凭据，构造 GATE 时把它们注入，并负责策略判定与 hook 注册。工作线程不能抢
stdin，走 decide_queued：只排队等远程决议，超时同样拒绝。会话与凭据都要在派发
时显式带走（use_session / use_grants）—— ContextVar 不随线程继承，漏带的那次
审批会落回进程级白名单键，跨会话免审。
"""

import contextlib
import contextvars
import os

import approval_view
import checkpoints
import grants
import net_grants
import policy_bash
import rules as user_rules
import taint
from agent_core import register_hook
from approval import (
    DEFAULT_APPROVAL_TIMEOUT,
    TIMEOUT_ENV,
    PermissionGate,
    PermissionRequest,
    approval_timeout,
)
from approval_judge import (
    DEFAULT_JUDGE_TIMEOUT,
    JUDGE_BATCH_PROMPT,
    JUDGE_DATA_MARKER,
    JUDGE_DETAIL_BUDGET,
    JUDGE_MODEL_ENV,
    JUDGE_RULES,
    JUDGE_SYSTEM_PROMPT,
    JUDGE_TIMEOUT_ENV,
    JUDGEABLE_CODES,
    LLM_APPROVAL_ENV,
    _judge_auto_approve,
    judge_timeout,
    llm_approval_enabled,
    preflight_judge,
)
from exec_policy import (
    CLIPBOARD_TOOLS,
    DENY,
    DESKTOP_TOOLS,
    PATH_INVALID,
    PATH_OUTSIDE,
    PATH_SECRET,
    PATH_SYSTEM,
    PATH_WORKSPACE,
    READ_ONLY_FILE_TOOLS,
    READ_WRITE_TOOLS,
    SCREEN_TOOLS,
    Decision,
    Policy,
    approval_class,
    classify_path,
    grant_key,
    load_policy,
    secret_references,
    session_allow_key,
)
from tools import PatchError, patch_paths, tool_registry

# 拆分后保持既有导入点不变：审批门与审查官的公开名字继续从本模块可见。
__all__ = [
    'DEFAULT_APPROVAL_TIMEOUT',
    'DEFAULT_JUDGE_TIMEOUT',
    'EXTERNAL_TOOLS',
    'GATE',
    'JUDGEABLE_CODES',
    'JUDGE_BATCH_PROMPT',
    'JUDGE_DATA_MARKER',
    'JUDGE_DETAIL_BUDGET',
    'JUDGE_MODEL_ENV',
    'JUDGE_RULES',
    'JUDGE_SYSTEM_PROMPT',
    'JUDGE_TIMEOUT_ENV',
    'LLM_APPROVAL_ENV',
    'PermissionGate',
    'PermissionRequest',
    'TIMEOUT_ENV',
    'approval_class',
    'approval_timeout',
    'context_injection_hook',
    'current_grants',
    'current_origin_chat',
    'current_session_id',
    'deny_message',
    'enforce',
    'enforce_headless',
    'evaluate_call',
    'is_tool_hook',
    'issue_grant',
    'judge_timeout',
    'large_output_hook',
    'llm_approval_enabled',
    'log_hook',
    'permission_hook',
    'preflight_judge',
    'register_external_tools',
    'session_allow_key',
    'summary_hook',
    'take_grant',
    'take_grants',
    'use_grants',
    'use_origin_chat',
    'use_scheduled_turn',
    'use_session',
    'scheduled_read_only',
    'scheduled_turn',
]


def _h():
    import harness
    return harness


# ============================================================
# 会话上下文（审批白名单分区）
# ============================================================

_SESSION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    'harness_session_id', default='')


# 定时回合上下文：None = 普通交互/API 回合；True = 只读定时回合；
# False = 创建时显式允许副作用的定时回合。只读定时回合不能借当前会话的
# 交互权限执行写操作；两种定时回合都不读 stdin（审批进远程/本地队列）。
_SCHEDULED_READ_ONLY: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    'harness_scheduled_read_only', default=None)


@contextlib.contextmanager
def use_scheduled_turn(*, read_only: bool = True):
    """标记当前线程正在执行定时任务回合（run_agent_turn_locked 使用）。"""
    token = _SCHEDULED_READ_ONLY.set(bool(read_only))
    try:
        yield
    finally:
        _SCHEDULED_READ_ONLY.reset(token)


def scheduled_turn() -> bool:
    return _SCHEDULED_READ_ONLY.get() is not None


def scheduled_read_only() -> bool:
    return _SCHEDULED_READ_ONLY.get() is True


# 来源聊天：模型在一个飞书回合里调用 schedule_cron 时，任务要记住「从哪个
# 聊天来的」，回合结束后才能把结果推回同一处。API/CLI 回合没有这个上下文。
_ORIGIN_CHAT: contextvars.ContextVar[str] = contextvars.ContextVar(
    'harness_origin_chat', default='')


@contextlib.contextmanager
def use_origin_chat(chat_id: str | None):
    token = _ORIGIN_CHAT.set(str(chat_id or ''))
    try:
        yield
    finally:
        _ORIGIN_CHAT.reset(token)


def current_origin_chat() -> str:
    return _ORIGIN_CHAT.get()


@contextlib.contextmanager
def use_session(session_id: str):
    """把会话 id 带进另一个线程（与 use_workspace / use_grants 同构）。

    ContextVar 不随 threading.Thread 继承，teammate 和后台 worker 的新线程因此
    会丢掉会话。丢掉的后果不只是「多问一遍」：白名单键退回进程级的 `bash:rm`
    （session_allow_key 的空会话分支），一个 teammate 里批准的 rm，别的会话的
    teammate 也免审。派发时把会话显式带走，决议就还按会话分区。
    """
    token = _SESSION_ID.set(session_id or '')
    try:
        yield
    finally:
        _SESSION_ID.reset(token)


def current_session_id() -> str:
    """当前会话 id，取不到就返回空串（白名单退回进程级键）。

    优先取 use_session 显式带过来的值；主循环/会话线程不需要额外接线，那里已经
    由 agent_loop 把 checkpoint manager 按回合设进 ContextVar，会话 id 就在它
    身上。再引入一个「当前会话」全局变量只会多一处需要同步的状态。
    测试里的假 harness 配置没有这个属性 → 无会话上下文。
    """
    session_id = _SESSION_ID.get()
    if session_id:
        return session_id
    try:
        manager = _h().active_checkpoint_manager()
    except AttributeError:
        return ''
    return manager.session_id if manager is not None else ''


# ============================================================
# 审批凭据（hook 决议 → 工具边界）
# ============================================================
# 批准后工具函数还会再判一次（run_bash / TaskManager._run 是绕过 hook 的入口），
# 凭据让「同一次调用」不被二次询问：按调用指纹一次性消费，只活在当前
# ContextVar 上下文里 —— 后台线程要显式带走（见 background_task、task_manager）。

_GRANTS: contextvars.ContextVar[frozenset] = contextvars.ContextVar(
    'harness_grants', default=frozenset())


def issue_grant(tool: str, args: dict) -> None:
    _GRANTS.set(_GRANTS.get() | {grant_key(tool, args)})


def take_grant(tool: str, args: dict) -> bool:
    """消费一次凭据。命中即失效，同样的命令再调一次还要重新审批。"""
    key = grant_key(tool, args)
    grants = _GRANTS.get()
    if key not in grants:
        return False
    _GRANTS.set(grants - {key})
    return True


def current_grants() -> frozenset:
    return _GRANTS.get()


def take_grants() -> frozenset:
    """移出当前上下文的全部凭据，供一个后台调用独占消费。"""
    grants = _GRANTS.get()
    _GRANTS.set(frozenset())
    return grants


@contextlib.contextmanager
def use_grants(grants):
    """把凭据带进另一个线程/上下文（与 use_workspace 同构的跨线程传递）。"""
    token = _GRANTS.set(frozenset(grants))
    try:
        yield
    finally:
        _GRANTS.reset(token)


def _reset_grants() -> None:
    """GATE.clear() 回调：审批门不直接依赖本模块的 ContextVar。"""
    _GRANTS.set(frozenset())


# ============================================================
# 审批门（approval.PermissionGate）
# ============================================================

def _record_approval_timeout(req: PermissionRequest) -> None:
    """审批无人应答 → 写入会话事件流（观察性，不改变流程）。

    远程端/网关看到的是「审批超时」而不是「用户拒绝」：前者说明审批通道失联或
    没人处理，后者是明确决定。事件只进事件流（SSE / watcher 可选推送），不进
    消息历史 —— 模型已经从 enforce 的拒绝语拿到原因了。

    没有活动会话（console 路径不会超时、单测没有会话文件）时静默跳过；写事件
    失败也只打印：观察性记录不能反过来改变一次已经被拒绝的调用。
    """
    session_id = req.session_id or current_session_id()
    if not session_id:
        return
    try:
        h = _h()
        # 测试用的假 harness 没有会话存储：跳过而不是把它当成写入失败。
        sessions_dir = getattr(h, 'SESSIONS_DIR', None)
        store_cls = getattr(h, 'SessionEventStore', None)
        if sessions_dir is None or store_cls is None:
            return
        path = sessions_dir / f'session_{session_id}.jsonl'
        if not path.is_file():
            return
        source = ''
        try:
            import turn_control
            token = turn_control.first_token(session_id)
            source = token.source if token is not None else ''
        except Exception:
            source = ''
        store_cls.open(path).append(
            'approval_timeout',
            request_id=req.req_id,
            tool=req.tool,
            reason=str(req.reason)[:200],
            session_id=session_id,
            source=source,
            timeout_s=round(float(GATE.timeout), 1),
        )
    except Exception as exc:
        print(f'  \033[33m[permission] failed to record approval timeout: '
              f'{type(exc).__name__}: {exc}\033[0m')


def _append_audit(event: str, payload: dict) -> bool:
    """把一条审计事件写进当前会话事件流；没有活动会话返回 False。

    审计是观察层：任何失败都只打印，绝不反过来改变一次已经做出的判定。
    """
    session_id = current_session_id()
    if not session_id:
        return False
    try:
        h = _h()
        sessions_dir = getattr(h, 'SESSIONS_DIR', None)
        store_cls = getattr(h, 'SessionEventStore', None)
        if sessions_dir is None or store_cls is None:
            return False
        path = sessions_dir / f'session_{session_id}.jsonl'
        if not path.is_file():
            return False
        store_cls.open(path).append(event, session_id=session_id, **payload)
        return True
    except Exception as exc:
        print(f'  \033[33m[permission] failed to record {event}: '
              f'{type(exc).__name__}: {exc}\033[0m')
        return False


def _record_policy_denied(tool: str, args: dict, decision, *,
                          reason: str = '', blocked_by: str = 'policy') -> None:
    """被拒绝的调用写审计事件。

    `policy_denied` 在审计白名单里登记了很久，但一直没有写入方——于是
    `/audit` 能回答「放行了什么」，却答不了「什么被挡住了」。而后者才是
    安全审计的主要用途。

    ``blocked_by`` 区分拒绝来源：
      - ``policy``     策略层硬拒（黑名单、秘密文件、系统路径、域名 deny…）
      - ``background`` 需要交互但当前是后台/定时回合
    """
    args = args if isinstance(args, dict) else {}
    summary = approval_view.summarize(tool, args)
    command = summary.get('command')
    _append_audit('policy_denied', {
        'tool': tool,
        'code': getattr(decision, 'code', '') or '',
        'zone': getattr(decision, 'pattern', '') or '',
        'reason': str(reason or getattr(decision, 'reason', '') or '')[:200],
        'blocked_by': blocked_by,
        'command': str(command)[:400] if command else '',
        'args_keys': sorted(str(key) for key in args)[:20],
    })


def _record_auto_approval(tool: str, args: dict, decision, *,
                          by: str, reason: str) -> None:
    """自动放行写审计事件（观察性，不改变流程）。

    审查官放行是本机唯一「不经人点头就执行」的路径，必须留下痕迹：谁批的
    （llm-judge / rule）、什么工具、命中的策略码、以及模型给出的理由。
    参数只记摘要（`approval_view.summarize` 的口径），命令留头尾——审计要能
    回答「当时放行了什么」，但不该变成第二份完整历史。

    没有活动会话、或会话文件不存在时静默跳过；写失败只打印：审计不能反过来
    让一次已经放行的调用失败。
    """
    args = args if isinstance(args, dict) else {}
    summary = approval_view.summarize(tool, args)
    command = summary.get('command')
    _append_audit('approval_auto', {
        'tool': tool,
        'by': by,
        'code': getattr(decision, 'code', '') or '',
        'reason': str(reason or '')[:200],
        'command': str(command)[:400] if command else '',
        'args_keys': sorted(str(key) for key in args)[:20],
    })

GATE = PermissionGate(
    session_id_provider=current_session_id,
    reset_grants=_reset_grants,
    on_timeout=_record_approval_timeout,
)


def is_tool_hook(tool:str,args:dict):
    if tool not in tool_registry.keys():
        return f'Error: unknown tool {tool}'
    return None


def permission_hook(tool:str,args:dict):
    """PreToolUse：交互式（CLI 主循环 / API 会话主线程）策略闸门。
    返回 None 放行，返回字符串即拒绝（写回 tool 消息给模型）。

    定时任务回合跑在后台线程：即使创建时授权了副作用，审批也进队列
    （decide_queued），不能从 cron 线程抢 stdin。"""
    return enforce(tool, args, interactive=not scheduled_turn())


# ============================================================
# 第三方工具名单（MCP）
# ============================================================
# 策略层只认识本项目实现的工具：bash 看命令文本，文件工具看路径，worktree 看
# 名字。MCP 工具在 initialize_mcp_tools 时才注册进来，名字和副作用都不在这个
# 已知集合里 —— 它可能写盘、可能发请求，静态判不了，所以登记进这份名单，由策略
# 层一律询问（exec_policy.Policy.evaluate 的 external_tools 参数）。
# 免审的出路是既有的「本次会话允许同类」：第三方工具的同类键就是工具名本身，
# 批准一次后同会话内同名工具不再重复问。

EXTERNAL_TOOLS: set[str] = set()
# 定时只读回合额外拦掉的工具：它们要么会改写状态但不经过文件/命令策略
# （任务板、todo、cron 表、团队协作），要么会把只读标记带丢后重新获得写权限
# （子代理、worktree）。返回值是给模型看的拒绝语，不是安全边界。
SCHEDULED_FORBIDDEN_TOOLS = frozenset({
    'task', 'spawn_teammate', 'send_message', 'request_shutdown',
    'request_plan', 'review_plan', 'schedule_cron', 'cancel_cron',
    'create_task', 'claim_task', 'complete_task', 'todo_write',
    'create_worktree', 'remove_worktree', 'keep_worktree',
    'kill_background', 'rename_session', 'revoke_permissions',
    'screen_capture', 'screen_ocr',
    'clipboard_read', 'clipboard_write',
    'desktop_snapshot', 'desktop_windows', 'desktop_action',
    'delete_paths',
})
# 多文件补丁取最严分区：一个补丁里只要有一个秘密/系统/越界路径，
# 整批就按该分区判定，避免被同批的工作区内文件稀释。
_PATH_ZONE_RANK = {
    PATH_WORKSPACE: 0,
    PATH_OUTSIDE: 1,
    PATH_SYSTEM: 2,
    PATH_INVALID: 3,
    PATH_SECRET: 4,
}


def register_external_tools(names) -> None:
    """登记第三方工具名；在 handler 注入 tool_registry 之后调用。"""
    EXTERNAL_TOOLS.update(name for name in names if name)


def evaluate_call(tool: str, args: dict, policy=None):
    """纯策略判定（黑名单 / 档位 / 越界），不问人。测试与 /sandbox 预览用它。

    policy 可以传入已解析的档位快照，免得调用方（enforce）读一次环境、判定再读
    一次；省略时按当前环境即时解析。
    """
    zone = PATH_WORKSPACE
    eval_args = args
    paths = None
    if tool == 'apply_patch':
        try:
            paths = patch_paths(str(args.get('patch') or ''))
        except PatchError as exc:
            # 解析不出版本/路径的补丁不能按「未知工具」放行：策略层对它
            # 没有路径可判，硬拒比弹一次看不懂的审批更安全。
            return Decision(DENY, 'invalid_patch', f'invalid patch ({exc})')
        zones = [_path_zone(path, args.get('cwd')) for path in paths]
        zone = max(zones, key=lambda item: _PATH_ZONE_RANK.get(item, 0))
        eval_args = {**args, 'path': paths[0]}
    elif tool in READ_ONLY_FILE_TOOLS or tool in READ_WRITE_TOOLS:
        # grep / ls / glob 的 path 缺省是工作区；用它参与分区判定，显式指向
        # 秘密文件时同样在策略层硬拒。
        zone = _path_zone(str(args.get('path') or '.'), args.get('cwd'))
    policy = policy or load_policy()
    # 持久授权只在默认档参与判定：read-only 仍要审批，danger 本来全放行。
    # 账本损坏时 active_write_paths 返回空元组（fail-closed），不会误放行。
    write_grants = (grants.active_write_paths()
                    if policy.sandbox_mode == 'workspace-write' else ())
    # 域名名单与执行档位无关：deny 是硬拒，连 danger-full-access 也绕不过。
    # 账本损坏时返回空元组，等价于「未知域名逐个审批」。
    net_rules = net_grants.active_rules()
    # 用户规则文件：只能收窄或提前决定「问不问」。硬拒（秘密文件、黑名单、
    # 系统路径、域名 deny）在 apply() 里被显式跳过，规则改不动它们。
    decision = policy.evaluate(
        tool, eval_args,
        deny_list=_h().DENY_LIST,
        destructive=_h().DESTRUCTIVE,
        secrets=checkpoints.DEFAULT_SECRETS,
        path_zone=zone,
        external_tools=EXTERNAL_TOOLS,
        workspace=str(_h().get_workdir()),
        write_grants=write_grants,
        net_rules=net_rules,
        paths=paths)
    # read-only / untrusted 是用户的显式选择，规则文件不能替他改回去。
    allow_permitted = policy.sandbox_mode != 'read-only'         and policy.approval_policy != 'untrusted'
    return user_rules.apply(decision, user_rules.load(), tool, eval_args,
                            allow_permitted=allow_permitted)


def _is_egress(tool: str, args: dict) -> bool:
    """本次调用是不是「把数据送出去」。

    ``web_fetch`` 与 ``http_request`` 这类工具恒为出网；``bash`` 要看命令动词，
    复用 ``policy_bash`` 已有的入口名单（curl/wget/nc/Invoke-WebRequest…），
    不另写一份黑名单。
    """
    if tool in ('web_fetch',):
        return True
    if tool != 'bash':
        return False
    command = str((args or {}).get('command') or '')
    if not command:
        return False
    try:
        verbs = policy_bash._command_verbs(command)
    except Exception:
        return False
    return bool(verbs & policy_bash.EGRESS_COMMANDS)


def _taint_upgrade_reason(tool: str, args: dict) -> str:
    """本回合读过敏感内容、现在要出网时返回一句话理由，否则空串。"""
    if not taint.tainted() or not _is_egress(tool, args):
        return ''
    return ('data egress after reading sensitive content in this turn; ' + taint.summary())


def _mark_read_taint(tool: str, args: dict) -> None:
    """把「刚读了敏感东西」记到本回合的污点表上。

    为什么挂在放行点上而不是读取实现里：读路径分散在 ``read``/``bash``/``grep``
    /``read_document`` 与无头 worker 里，逐个改容易漏（包括以后的子代理）。
    这里只要知道「本次调用的目标是什么」，不依赖读完之后的返回值。

    判定只靠文件名/路径，不靠内容：内容级检测（看起来像密钥）误报多且会诱使
    后续代码去做「正则猜测」，反而不可靠。
    """
    if tool == 'bash':
        command = str((args or {}).get('command') or '')
        if not command:
            return
        try:
            refs = secret_references(
                command, checkpoints.DEFAULT_SECRETS)
        except Exception:
            return
        if refs:
            taint.mark(f'protected file via bash ({refs[0]})')
        return
    if tool not in ('read', 'grep', 'read_document', 'load_skill'):
        return
    path = str((args or {}).get('path') or (args or {}).get('pattern') or '')
    if not path:
        return
    lowered = path.replace('\\', '/').casefold()
    # 就算文件工具已经硬拦了秘密路径，这里仍然记一笔：拦截失败的路径（新格式
    # 的秘密文件名、用户自定义 secrets）不会走到这里，但记忆/会话目录会。
    if 'memor' in lowered or '/memory' in lowered:
        taint.mark(f'memory files ({path})')
    elif 'session' in lowered and ('.jsonl' in lowered or 'history' in lowered):
        taint.mark(f'session history ({path})')


def enforce(tool: str, args: dict, *, interactive: bool) -> str | None:
    """执行路径的唯一策略入口：策略层 → 会话白名单 → 审批 → 凭据。

    返回 None 放行，返回字符串是拒绝语（四条路径共用，模型看到的文案一致）。
    interactive=True：调用线程可以问人（PreToolUse hook 跑在主循环里）；
    False：工作线程（teammate / 后台 worker / 任务 runner），询问进远程审批队列，
    超时拒绝。

    定时任务默认按 read-only 档重判：写文件、联网、第三方工具、子代理直接拒绝，
    不进入审批 —— 没人授权过这个任务的副作用，不能让它借当前会话的档位执行。
    """
    policy = load_policy()
    decision = evaluate_call(tool, args, policy=policy)
    # 先记污点再判决策：无论这次调用最后是放行、被拒还是用户拒，只要它"读过"
    # 敏感目标，本回合就已经有污点了。挂在入口而不是各 return 点，避免以后
    # 新增分支时漏掉（漏一处就等于整条链路失效）。
    _mark_read_taint(tool, args)
    if scheduled_read_only():
        if tool in SCHEDULED_FORBIDDEN_TOOLS:
            _record_policy_denied(tool, args, decision,
                                  reason=_scheduled_readonly_error(tool),
                                  blocked_by='background')
            return _scheduled_readonly_error(tool)
        if decision.denied:
            _record_policy_denied(tool, args, decision)
            return deny_message(decision)
        # 用 read-only + never 复核：工作区写改在默认档是 ALLOW，只读档才会
        # 变成 ASK，never 再把它折成 DENY。硬拒（秘密路径/黑名单）保持原文案。
        readonly = evaluate_call(
            tool, args, policy=Policy(sandbox_mode='read-only',
                                      approval_policy='never'))
        if readonly.denied:
            _record_policy_denied(tool, args, readonly,
                                  reason=_scheduled_readonly_error(tool),
                                  blocked_by='background')
            return _scheduled_readonly_error(tool)
        return None
    if decision.allowed:
        # 污点出网：策略层放行不代表应该默默放行 —— 本回合读过秘密/记忆/历史后
        # 再联网，审批面上原本看不到这层因果（各步同类键互相独立）。这里把它
        # 升成 ASK，让审批人看到“刚读过什么，现在要发出去”。
        taint_reason = _taint_upgrade_reason(tool, args)
        if taint_reason:
            print(f'\n\033[33m[taint] {taint_reason}\033[0m')
            ask = GATE.decide if interactive else GATE.decide_queued
            allowed, message = ask(tool, args, taint_reason)
            if not allowed:
                if message.split(' ', 1)[0] == 'timeout':
                    return (f'Permission denied: approval timed out after '
                            f'{GATE.timeout:g}s with no response')
                return 'Permission denied by user'
            issue_grant(tool, args)
            return None
        # 规则放行同样要留痕：它和审查官放行一样是「没人点头就执行」。
        if decision.code == 'rule_allowed':
            _record_auto_approval(tool, args, decision, by='rule',
                                  reason=decision.reason)
        return None
    if decision.denied:
        print(f'\n\033[31m[blocked] {decision.pattern or decision.reason}\033[0m')
        _record_policy_denied(tool, args, decision)
        return deny_message(decision)
    # 屏幕内容可能包含锁屏后的私密信息，且没人能确认「现在屏幕上是用户想让我看
    # 的那一屏」：后台/定时回合不进入审批队列，直接拒绝。
    if tool in SCREEN_TOOLS and not interactive:
        _record_policy_denied(tool, args, decision,
                              reason=_screen_background_error(tool),
                              blocked_by='background')
        return _screen_background_error(tool)
    if tool in CLIPBOARD_TOOLS and not interactive:
        _record_policy_denied(tool, args, decision,
                              reason=_clipboard_background_error(tool),
                              blocked_by='background')
        return _clipboard_background_error(tool)
    if tool in DESKTOP_TOOLS and not interactive:
        _record_policy_denied(tool, args, decision,
                              reason=_desktop_background_error(tool),
                              blocked_by='background')
        return _desktop_background_error(tool)
    # untrusted 档的语义是「每条都问」，而会话白名单是一种预授权：命中它就等于
    # 用一次「本次会话允许同类」把档位悄悄改回 on-request。所以这一档不走白名单，
    # 屏幕/剪贴板工具同理：每次都应按当下的屏幕/剪贴板重新审批。
    if (policy.approval_policy != 'untrusted'
            and tool not in SCREEN_TOOLS and tool not in CLIPBOARD_TOOLS
            and tool not in DESKTOP_TOOLS
            and GATE.session_allows(tool, args)):
        # 污点优先于白名单：一次「这个域名我认了」的预授权不是在读过密钥之后的
        # 放行依据。否则污点机制会被早先批准的域名整个绕开。
        taint_reason = _taint_upgrade_reason(tool, args)
        if taint_reason:
            print(f'\n\033[33m[taint] {taint_reason}\033[0m')
            ask = GATE.decide if interactive else GATE.decide_queued
            allowed, message = ask(tool, args, taint_reason)
            if not allowed:
                if message.split(' ', 1)[0] == 'timeout':
                    return (f'Permission denied: approval timed out after '
                            f'{GATE.timeout:g}s with no response')
                return 'Permission denied by user'
            issue_grant(tool, args)
            return None
        print(f'  \033[32m[permission] 会话白名单命中，自动允许 · {tool}\033[0m')
        issue_grant(tool, args)   # 白名单命中同样是一次「本调用已批准」
        return None
    # 污点出网优先于 LLM 审查官：审查官看的是「这条命令像不像常规开发操作」，
    # 它看不到本回合前面读过什么，也就无权在读过密钥之后放行外发。
    taint_reason = _taint_upgrade_reason(tool, args)
    if taint_reason:
        print(f'\n\033[33m[taint] {taint_reason}\033[0m')
        ask = GATE.decide if interactive else GATE.decide_queued
        allowed, message = ask(tool, args, taint_reason)
        if not allowed:
            if message.split(' ', 1)[0] == 'timeout':
                return (f'Permission denied: approval timed out after '
                        f'{GATE.timeout:g}s with no response')
            return 'Permission denied by user'
        issue_grant(tool, args)
        return None
    # LLM 审查官：两个噪音源（解释器命令、破坏性子串误伤）先复核，
    # 判定为常规开发操作则自动放行；升级或审查不可用则照常走人工审批。
    # 仅限 interactive 路径：无头线程（teammate/后台/任务 runner）的审批
    # 语义是「排队等远程决议」，不能被 LLM 旁路——它们的 ASK 照旧进队列。
    if (interactive and llm_approval_enabled()
            and policy.approval_policy == 'on-request'
            and decision.code in JUDGEABLE_CODES
            and _judge_auto_approve(tool, args, decision)):
        _record_auto_approval(tool, args, decision, by='llm-judge',
                              reason=decision.reason)
        issue_grant(tool, args)
        return None
    ask = GATE.decide if interactive else GATE.decide_queued
    allowed, message = ask(tool, args, decision.reason)
    if not allowed:
        # message 首段是终态（approval.PermissionGate._conclude）。超时要和「用户
        # 拒绝」区分开：模型据此决定是换个方式还是稍后重试，远程端也能看到审批
        # 通道失联（会话事件由 _record_approval_timeout 落盘）。
        if message.split(' ', 1)[0] == 'timeout':
            return (f'Permission denied: approval timed out after '
                    f'{GATE.timeout:g}s with no response')
        # 对模型的固定拒绝语（保持旧契约）；细节（req_id / 原因）在审批历史里
        return 'Permission denied by user'
    issue_grant(tool, args)
    return None


def enforce_headless(tool: str, args: dict) -> str | None:
    """工具边界 / 工作线程的判定入口（run_bash、TaskManager._run 用它）。

    绕过 PreToolUse hook 的调用没有第二次拦截机会，所以边界上必须自己判一次；
    hook 刚批准的那次调用带凭据，取走即可，不必再问一遍。
    """
    if take_grant(tool, args):
        return None
    error = enforce(tool, args, interactive=False)
    if error is None:
        take_grant(tool, args)   # 消费掉刚登记的凭据，不留给下一次同名调用
    return error


def _scheduled_readonly_error(tool: str) -> str:
    """只读定时任务触碰副作用时的拒绝语。

    文案要点名「这是任务配置，不是一次审批」：模型不该反复换写法重试，用户
    需要到 /cron 重新创建任务并显式允许副作用。"""
    return (f'Error: scheduled task is read-only; {tool} is not allowed. '
            'Re-create the job with allow_side_effects enabled to permit '
            'side effects (approval still applies).')


def _screen_background_error(tool: str) -> str:
    return (f'Error: {tool} requires an interactive turn; background and '
            'scheduled tasks cannot read the local screen.')


def _clipboard_background_error(tool: str) -> str:
    return (f'Error: {tool} requires an interactive turn; background and '
            'scheduled tasks cannot access the local clipboard.')


def _desktop_background_error(tool: str) -> str:
    return (f'Error: {tool} requires an interactive turn; background and '
            'scheduled tasks cannot control the local desktop.')


# 每类拒绝给一句「接下来可以怎么做」。模型收到一句干巴巴的拒绝，最常见的
# 反应是换个写法再试一次；给出替代路径能让它停止试探、直接告诉用户。
_DENY_HINTS = {
    'deny_pattern': 'This command is on the deny list; use another tool or ask '
                    'the user to run it.',
    'deny_root_delete': 'Refusing to delete a filesystem root.',
    'secret_path': 'Protected path (secret files and version-control '
                   'internals are off limits); ask the user to handle it.',
    'secret_egress': 'This would send a protected file outbound; confirm with '
                     'the user first.',
    'system_path': 'Writes to system directories are blocked; choose a path '
                   'inside the workspace.',
    'domain_denied': 'This domain is denied by policy; ask the user to remove '
                     'the rule if it is needed.',
    'outside_workspace_write': 'Write inside the workspace, or ask the user to '
                               'grant this directory.',
    'sandbox_read_only': 'The sandbox is read-only; ask the user to switch tiers.',
    'invalid_url': 'Use a plain http(s) URL with a public host.',
    'invalid_path': 'The path could not be resolved; check it and retry.',
}


def deny_message(decision) -> str:
    """拒绝语文案是对外契约：提示词规则、审批测试、旧日志都在断言它。

    在原文后追加一行可执行提示（``hint``），但不改变首句——首句是契约的一部分。
    """
    if decision.pattern:
        head = f'Error: Dangerous command {decision.pattern}, please do not execute'
    else:
        head = f'Error: {decision.reason}'
    hint = _DENY_HINTS.get(getattr(decision, 'code', '') or '')
    return head + chr(10) + hint if hint else head


def _path_zone(path: str, cwd: str | None = None) -> str:
    """与 tools.safe_path 同一套解析 —— 两处不一致时，批准后工具会写到另一个地方。"""
    return classify_path(path, cwd=cwd, workspace=_h().get_workdir(),
                         secrets=checkpoints.DEFAULT_SECRETS)


def log_hook(tool:str,args:dict,output:str):
    print(f"\033[90m[HOOK] {tool}({args}) -> {output[:100]}\033[0m")
    return None


def large_output_hook(tool:str,args:dict,output:str):
    if len(str(output))>100000:
        print(f"\033[33m[HOOK] Large output from {tool}: {len(str(output))} chars\033[0m")
    return None


def context_injection_hook(query:str):
    if os.getenv('HARNESS_DEBUG') == '1':
        print(f"\033[90m[HOOK] UserPromptSubmit: working in {_h().get_workdir()}\033[0m")
    return None


def summary_hook(messages:list):
    if os.getenv('HARNESS_DEBUG') == '1':
        tool_count=sum(1 for m in messages if m.get('role')=='tool')
        print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


register_hook("UserPromptSubmit",context_injection_hook)

register_hook("Stop",summary_hook)

register_hook("PreToolUse",is_tool_hook)

register_hook("PreToolUse",permission_hook)

register_hook("PostToolUse",log_hook)

register_hook("PostToolUse",large_output_hook)
