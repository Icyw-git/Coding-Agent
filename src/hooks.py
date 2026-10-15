"""Hook 回调：权限 / 日志 / 上下文注入 / 总结。注册表在 agent_core。
可变全局（DENY_LIST/DESTRUCTIVE/WORKDIR）由 harness 持有，经 _h() 读取。

权限审批中心（PermissionGate）：
    console — CLI 交互，主线程 input()；stdin 不可用（服务器/管道）自动拒绝
    remote  — 挂起 threading.Event，由 HTTP API（/v1/permissions）远程决议，
              飞书/TG 等消息网关轮询该端点并把决议送回
    超时未决议一律拒绝（fail-safe），默认 300s（HARNESS_APPROVAL_TIMEOUT 可调）。
"""
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

from agent_core import register_hook
from tools import tool_registry


def _h():
    import harness
    return harness


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

    # ---------- 登记 / 查询（供 API 与测试使用） ----------

    def request(self, tool: str, args: dict, reason: str) -> PermissionRequest:
        req = PermissionRequest(
            req_id=f'perm_{uuid.uuid4().hex[:10]}',
            tool=tool,
            reason=reason,
            created_at=time.time(),
            args=dict(args),
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
        """清空全部请求历史（测试隔离辅助）。"""
        with self._lock:
            self._requests.clear()

    def pending(self) -> list[PermissionRequest]:
        with self._lock:
            return [req for req in self._requests.values()
                    if req.status == 'pending']

    def history(self, limit: int = 20) -> list[PermissionRequest]:
        with self._lock:
            rows = sorted(self._requests.values(),
                          key=lambda r: r.created_at, reverse=True)
        return rows[:max(1, limit)]

    def resolve(self, req_id: str, allow: bool, by: str = 'remote') -> PermissionRequest:
        """远程决议一个 pending 请求并唤醒等待方。未知 id / 已决议 → 异常。"""
        with self._lock:
            req = self._requests.get(req_id)
            if req is None:
                raise KeyError(req_id)
            if req.status != 'pending':
                raise RuntimeError(f'request {req_id} already {req.status}')
            req.status = 'approved' if allow else 'denied'
            req.resolved_by = by
            req._event.set()
            return req

    # ---------- 决议入口（permission_hook 调用） ----------

    def decide(self, tool: str, args: dict, reason: str) -> tuple[bool, str]:
        """登记请求并等待决议，返回 (allowed, message)。阻塞调用方线程。"""
        req = self.request(tool, args, reason)
        if self.backend == 'remote':
            signalled = req._event.wait(self.timeout)
            if not signalled:
                with self._lock:
                    if req.status == 'pending':
                        req.status, req.resolved_by = 'timeout', 'gate'
        else:
            self._decide_console(req)
        allowed = req.status == 'approved'
        message = f'{req.status} [{req.req_id}] {tool}: {reason}'
        status = {'approved': '已允许', 'denied': '已拒绝', 'timeout': '审批超时'}[req.status]
        print(f'  \033[{"32" if allowed else "31"}m[permission] {status} · {tool}: {reason}\033[0m')
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
    if tool=='bash':
        command=args.get("command", "")
        normalized=command.casefold()
        for pattern in _h().DENY_LIST:
            if pattern.casefold() in normalized:
                print(f'\n\033[31m⛔ Blocked: {pattern}\033[0m')
                return f"Error: Dangerous command {pattern}, please do not execute"
        reasons=[]
        for pattern in _h().DESTRUCTIVE:
            if pattern.casefold() in normalized:
                reasons.append(f"potentially destructive command ({pattern})")
                break
        if os.getenv('HARNESS_APPROVAL_ALL') == '1':
            reasons.append('all bash commands require approval')
        if reasons:
            allowed, message = GATE.decide(tool, args, '; '.join(reasons))
            if not allowed:
                # 对模型的固定拒绝语（保持旧契约）；细节（req_id/原因）走日志打印
                return 'Permission denied by user'
    elif tool in ["read", "write", "edit"]:
        path=args.get('path','')
        try:
            workspace = _h().get_workdir()
            inside=(workspace / path).resolve().is_relative_to(workspace)
        except (OSError, ValueError):
            inside=False
        if not inside:
            allowed, message = GATE.decide(tool, args, 'access outside workspace')
            if not allowed:
                return 'Permission denied by user'

    return None


def log_hook(tool:str,args:dict,output:str):
    print(f"\033[90m[HOOK] {tool}({args}) -> {output[:100]}\033[0m")
    return None


def large_output_hook(tool:str,args:dict,output:str):
    if len(str(output))>100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {tool}: {len(str(output))} chars\033[0m")
    return None


def context_injection_hook(query:str):
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {_h().get_workdir()}\033[0m")
    return None


def summary_hook(messages:list):
    tool_count=sum(1 for m in messages if m.get('role')=='tool')
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


register_hook("UserPromptSubmit",context_injection_hook)

register_hook("Stop",summary_hook)

register_hook("PreToolUse",is_tool_hook)

register_hook("PreToolUse",permission_hook)

register_hook("PostToolUse",log_hook)

register_hook("PostToolUse",large_output_hook)
