"""Hook 回调：权限 / 日志 / 上下文注入 / 总结。注册表在 agent_core。
可变全局（DENY_LIST/DESTRUCTIVE/WORKDIR）由 harness 持有，经 _h() 读取。

执行策略的唯一入口是 enforce()：策略层判定（exec_policy）→ 会话白名单 → 审批 →
凭据。CLI 的 PreToolUse hook、工具边界（run_bash）、teammate、后台任务与 HTTP
任务都经过它，区别只在 interactive —— 即调用线程能不能问人。

权限审批中心（PermissionGate）：
    console — CLI 交互，主线程 input()；stdin 不可用（服务器/管道）自动拒绝
    remote  — 挂起 threading.Event，由 HTTP API（/v1/permissions）远程决议，
              飞书/TG 等消息网关轮询该端点并把决议送回
    工作线程（teammate / 后台 worker / 任务 runner）不能抢 stdin，走 decide_queued：
    只排队等远程决议，超时同样拒绝。
    超时未决议一律拒绝（fail-safe），默认 300s（HARNESS_APPROVAL_TIMEOUT 可调）。
"""
import contextlib
import contextvars
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agent_core import register_hook
from exec_policy import (READ_WRITE_TOOLS, approval_class, grant_key, load_policy,
                         session_allow_key)
from tools import tool_registry


def _h():
    import harness
    return harness


# ============================================================
# 会话上下文（审批白名单分区）
# ============================================================

def current_session_id() -> str:
    """当前会话 id，取不到就返回空串（白名单退回进程级键，行为与旧版一致）。

    借用 checkpoint manager 的会话绑定：它已经在 agent_loop 里按回合设好，
    再引入一个「当前会话」全局变量只会多一处需要同步的状态。
    测试里的假 harness 配置没有这个属性 → 无会话上下文。
    """
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


@dataclass
class PermissionRequest:
    """一次待审批操作：登记后可被控制台或远程网关决议。"""
    req_id: str
    tool: str
    reason: str
    created_at: float
    args: dict = field(default_factory=dict)
    status: str = 'pending'        # pending | approved | denied | timeout
    resolved_by: str = ''
    session_id: str = ''           # 登记时的会话（空串 = 无会话上下文，白名单退回进程级）
    _event: threading.Event = field(default_factory=threading.Event,
                                    repr=False, compare=False)

    def to_dict(self) -> dict:
        return {key: value for key, value in vars(self).items()
                if not key.startswith('_')}


class PermissionGate:
    """登记请求并等待决议；双后端 + 超时熔断 + 审批历史（有界）。"""

    MAX_HISTORY = 200   # 已决议请求的保留上限，防止长驻服务器无限积累

    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = float(
            timeout if timeout is not None
            else os.getenv('HARNESS_APPROVAL_TIMEOUT', '300'))
        self.backend = 'console'   # console | remote
        self._lock = threading.RLock()
        self._requests: dict[str, PermissionRequest] = {}
        # 会话级白名单（审批分级）：键 session_allow_key(tool, klass, session_id)，
        # 命中后同会话的同类操作免审
        self._session_allows: set[str] = set()

    # ---------- 登记 / 查询（供 API 与测试使用） ----------

    def request(self, tool: str, args: dict, reason: str,
                session_id: str | None = None) -> PermissionRequest:
        req = PermissionRequest(
            req_id=f'perm_{uuid.uuid4().hex[:10]}',
            tool=tool,
            reason=reason,
            created_at=time.time(),
            args=dict(args),
            session_id=current_session_id() if session_id is None else session_id,
        )
        with self._lock:
            self._requests[req.req_id] = req
            self._prune_history()
        return req

    def _prune_history(self) -> None:
        """超过上限时淘汰最老的已决议记录（pending 的永不淘汰）。"""
        excess = len(self._requests) - self.MAX_HISTORY
        if excess <= 0:
            return
        resolved = sorted(
            (req for req in self._requests.values() if req.status != 'pending'),
            key=lambda r: r.created_at)
        for req in resolved[:excess]:
            self._requests.pop(req.req_id, None)

    def clear(self) -> None:
        """清空请求历史、会话白名单与本上下文的审批凭据（测试隔离辅助）。"""
        with self._lock:
            self._requests.clear()
            self._session_allows.clear()
        _GRANTS.set(frozenset())

    def pending(self) -> list[PermissionRequest]:
        with self._lock:
            return [req for req in self._requests.values()
                    if req.status == 'pending']

    def history(self, limit: int = 20) -> list[PermissionRequest]:
        with self._lock:
            rows = sorted(self._requests.values(),
                          key=lambda r: r.created_at, reverse=True)
        return rows[:max(1, limit)]

    def resolve(self, req_id: str, allow: bool, by: str = 'remote',
                scope: str | None = None, klass: str | None = None) -> PermissionRequest:
        """远程决议一个 pending 请求并唤醒等待方。未知 id / 已决议 → 异常。

        scope='session' 且 klass 非空、allow=True 时，把「该会话的同类操作」加入
        白名单（键按登记时的 session_id 分区），之后同会话同类审批自动通过。
        旧实现是进程级集合：一个会话批过的 rm，另一个会话也免审。
        """
        with self._lock:
            req = self._requests.get(req_id)
            if req is None:
                raise KeyError(req_id)
            if req.status != 'pending':
                raise RuntimeError(f'request {req_id} already {req.status}')
            req.status = 'approved' if allow else 'denied'
            req.resolved_by = by
            if allow and scope == 'session' and klass:
                self._session_allows.add(session_allow_key(req.tool, klass, req.session_id))
            req._event.set()
            return req

    # ---------- 会话级白名单（审批分级） ----------

    def session_allows(self, tool: str, args: dict,
                       session_id: str | None = None) -> bool:
        """同类操作是否已被本会话批准。session_id=None 时取当前会话上下文。"""
        key = session_allow_key(tool, approval_class(tool, args),
                                current_session_id() if session_id is None else session_id)
        with self._lock:
            return key in self._session_allows

    # ---------- 决议入口（enforce 调用） ----------

    def decide(self, tool: str, args: dict, reason: str) -> tuple[bool, str]:
        """登记请求并等待决议，返回 (allowed, message)。阻塞调用方线程。"""
        req = self.request(tool, args, reason)
        if self.backend == 'remote':
            self._await_remote(req)
        else:
            self._decide_console(req)
        return self._conclude(req)

    def decide_queued(self, tool: str, args: dict, reason: str) -> tuple[bool, str]:
        """无头路径（teammate / 后台 worker / 任务 runner / HTTP）的审批。

        这些线程不能读 stdin，否则会和主循环抢控制台、或在工作线程里静默吃掉
        一次询问。所以只登记进队列等远程决议（/approvals、审批卡片、HTTP），
        超时同样拒绝。阻塞的是这个工作线程，主循环不受影响。"""
        req = self.request(tool, args, reason)
        print(f'  \033[33m[permission] {tool} 等待审批：{reason}'
              f'（/approvals 或审批卡片处理，{int(self.timeout)}s 未决议则拒绝）\033[0m')
        self._await_remote(req)
        return self._conclude(req)

    def _await_remote(self, req: PermissionRequest) -> None:
        signalled = req._event.wait(self.timeout)
        if not signalled:
            with self._lock:
                if req.status == 'pending':
                    req.status, req.resolved_by = 'timeout', 'gate'

    def _conclude(self, req: PermissionRequest) -> tuple[bool, str]:
        allowed = req.status == 'approved'
        message = f'{req.status} [{req.req_id}] {req.tool}: {req.reason}'
        status = {'approved': '已允许', 'denied': '已拒绝', 'timeout': '审批超时'}[req.status]
        print(f'  \033[{"32" if allowed else "31"}m[permission] {status} · '
              f'{req.tool}: {req.reason}\033[0m')
        return allowed, message

    def _decide_console(self, req: PermissionRequest) -> bool:
        print(f"\n\033[33m⚠  {req.reason}\033[0m")
        print(f"   操作: {req.tool}({req.args})")
        try:
            choice = input('Allow? (y/n) ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            print('  \033[31m[permission] stdin unavailable → denied\033[0m')
            req.status, req.resolved_by = 'denied', 'console(EOF)'
            return False
        req.status = 'approved' if choice in ('y', 'yes') else 'denied'
        req.resolved_by = 'console'
        return req.status == 'approved'


GATE = PermissionGate()


def is_tool_hook(tool:str,args:dict):
    if tool not in tool_registry.keys():
        return f'Error: unknown tool {tool}'
    return None


def permission_hook(tool:str,args:dict):
    """PreToolUse：交互式（CLI 主循环 / API 会话主线程）策略闸门。
    返回 None 放行，返回字符串即拒绝（写回 tool 消息给模型）。"""
    return enforce(tool, args, interactive=True)


def evaluate_call(tool: str, args: dict):
    """纯策略判定（黑名单 / 档位 / 越界），不问人。测试与 /sandbox 预览用它。"""
    inside = True
    if tool == 'read' or tool in READ_WRITE_TOOLS:
        inside = _inside_workspace(str(args.get('path', '')), args.get('cwd'))
    return load_policy().evaluate(tool, args,
                                  deny_list=_h().DENY_LIST,
                                  destructive=_h().DESTRUCTIVE,
                                  inside_workspace=inside)


def enforce(tool: str, args: dict, *, interactive: bool) -> str | None:
    """执行路径的唯一策略入口：策略层 → 会话白名单 → 审批 → 凭据。

    返回 None 放行，返回字符串是拒绝语（四条路径共用，模型看到的文案一致）。
    interactive=True：调用线程可以问人（PreToolUse hook 跑在主循环里）；
    False：工作线程（teammate / 后台 worker / 任务 runner），询问进远程审批队列，
    超时拒绝。
    """
    decision = evaluate_call(tool, args)
    if decision.allowed:
        return None
    if decision.denied:
        print(f'\n\033[31m⛔ Blocked: {decision.pattern or decision.reason}\033[0m')
        return deny_message(decision)
    if GATE.session_allows(tool, args):
        print(f'  \033[32m[permission] 会话白名单命中，自动允许 · {tool}\033[0m')
        issue_grant(tool, args)   # 白名单命中同样是一次「本调用已批准」
        return None
    ask = GATE.decide if interactive else GATE.decide_queued
    allowed, _message = ask(tool, args, decision.reason)
    if not allowed:
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


def _inside_workspace(path: str, cwd: str | None = None) -> bool:
    """与 tools.safe_path 同一套解析 —— 两处判定不一致时，批准了也会被路径检查拒绝。"""
    try:
        workspace = _h().get_workdir()
        base = Path(cwd).resolve() if cwd else workspace
        return (base / path).resolve().is_relative_to(workspace)
    except (OSError, ValueError):
        return False


def log_hook(tool:str,args:dict,output:str):
    print(f"\033[90m[HOOK] {tool}({args}) -> {output[:100]}\033[0m")
    return None


def large_output_hook(tool:str,args:dict,output:str):
    if len(str(output))>100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {tool}: {len(str(output))} chars\033[0m")
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
