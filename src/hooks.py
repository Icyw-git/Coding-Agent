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

import checkpoints
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
    DENY,
    PATH_INVALID,
    PATH_OUTSIDE,
    PATH_SECRET,
    PATH_SYSTEM,
    PATH_WORKSPACE,
    READ_ONLY_FILE_TOOLS,
    READ_WRITE_TOOLS,
    Decision,
    approval_class,
    classify_path,
    grant_key,
    load_policy,
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
    'use_session',
]


def _h():
    import harness
    return harness


# ============================================================
# 会话上下文（审批白名单分区）
# ============================================================

_SESSION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    'harness_session_id', default='')


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
    返回 None 放行，返回字符串即拒绝（写回 tool 消息给模型）。"""
    return enforce(tool, args, interactive=True)


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
    return (policy or load_policy()).evaluate(
        tool, eval_args,
        deny_list=_h().DENY_LIST,
        destructive=_h().DESTRUCTIVE,
        secrets=checkpoints.DEFAULT_SECRETS,
        path_zone=zone,
        external_tools=EXTERNAL_TOOLS,
        workspace=str(_h().get_workdir()))


def enforce(tool: str, args: dict, *, interactive: bool) -> str | None:
    """执行路径的唯一策略入口：策略层 → 会话白名单 → 审批 → 凭据。

    返回 None 放行，返回字符串是拒绝语（四条路径共用，模型看到的文案一致）。
    interactive=True：调用线程可以问人（PreToolUse hook 跑在主循环里）；
    False：工作线程（teammate / 后台 worker / 任务 runner），询问进远程审批队列，
    超时拒绝。
    """
    policy = load_policy()
    decision = evaluate_call(tool, args, policy=policy)
    if decision.allowed:
        return None
    if decision.denied:
        print(f'\n\033[31m[blocked] {decision.pattern or decision.reason}\033[0m')
        return deny_message(decision)
    # untrusted 档的语义是「每条都问」，而会话白名单是一种预授权：命中它就等于
    # 用一次「本次会话允许同类」把档位悄悄改回 on-request。所以这一档不走白名单。
    if policy.approval_policy != 'untrusted' and GATE.session_allows(tool, args):
        print(f'  \033[32m[permission] 会话白名单命中，自动允许 · {tool}\033[0m')
        issue_grant(tool, args)   # 白名单命中同样是一次「本调用已批准」
        return None
    # LLM 审查官：两个噪音源（解释器命令、破坏性子串误伤）先复核，
    # 判定为常规开发操作则自动放行；升级或审查不可用则照常走人工审批。
    # 仅限 interactive 路径：无头线程（teammate/后台/任务 runner）的审批
    # 语义是「排队等远程决议」，不能被 LLM 旁路——它们的 ASK 照旧进队列。
    if (interactive and llm_approval_enabled()
            and policy.approval_policy == 'on-request'
            and decision.code in JUDGEABLE_CODES
            and _judge_auto_approve(tool, args, decision)):
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


def deny_message(decision) -> str:
    """拒绝语文案是对外契约：提示词规则、审批测试、旧日志都在断言它。"""
    if decision.pattern:
        return f'Error: Dangerous command {decision.pattern}, please do not execute'
    return f'Error: {decision.reason}'


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
