"""审批门：登记请求、等待决议、维护会话白名单与审查官缓存。

从 hooks.py 拆出。本模块不 import harness / hooks：会话 id 与凭据清理由调用方
以回调注入（hooks 里的 GATE 构造处），UI 弹窗通过 harness.permission_gate
懒加载。这样 gate 可以脱离运行时单测，也不会把审批状态复制到两个模块。

超时未决议一律拒绝（fail-safe），默认 300s（HARNESS_APPROVAL_TIMEOUT 可调）。
超时不是一次普通的「用户拒绝」：调用方可通过 ``on_timeout`` 注入观察者，把
「无人应答」写成显式会话事件，远程端因此能区分主动拒绝与审批通道失联。
"""

import os
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import approval_scope
import approval_view
from exec_policy import approval_class, session_allow_key


def _h():
    """渲染层所在的 harness 模块；只在 UI 分流时懒加载。"""
    import harness
    return harness


# 审批面的参数摘要与终端可读性都在 approval_view：那里说明「命令留头尾、其余
# 限长」的分工，以及为什么展示前要转义控制字符。

# 审批超时：未决议一律拒绝（fail-safe）。环境只在构造时读一次，之后 timeout 就是
# 普通属性，改它即刻生效。
TIMEOUT_ENV = 'HARNESS_APPROVAL_TIMEOUT'
DEFAULT_APPROVAL_TIMEOUT = 300.0


def approval_timeout() -> float:
    """解析 HARNESS_APPROVAL_TIMEOUT；非法值提示一声后按默认档。

    PermissionGate 在 import 时构造，一个笔误（'5m'）抛出的 ValueError 会连累整个
    CLI；但静默回落更糟 —— 用户以为等 5 秒，其实还是 300 秒。0 和负数同样回落：
    那等于「任何审批都立刻超时拒绝」，不是人能配出来的意图。
    """
    raw = (os.getenv(TIMEOUT_ENV) or '').strip()
    if not raw:
        return DEFAULT_APPROVAL_TIMEOUT
    try:
        seconds = float(raw)
    except ValueError:
        return _timeout_fallback(raw, 'not a number of seconds')
    if seconds <= 0:
        return _timeout_fallback(raw, 'must be a positive number of seconds')
    return seconds


def _timeout_fallback(raw: str, problem: str) -> float:
    print(f'\033[33m[permission] ignoring {TIMEOUT_ENV}={raw!r} ({problem}), '
          f'using {DEFAULT_APPROVAL_TIMEOUT:g}s\033[0m', file=sys.stderr)
    return DEFAULT_APPROVAL_TIMEOUT


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


def _ui_gate():
    """Textual 模式的审批门（harness.permission_gate）；不可用时返回 None。"""
    return getattr(_h(), 'permission_gate', None)


def _ui_mode() -> bool:
    """渲染层接管终端时，审批走弹窗、控制台提示一律不打印（否则会刷坏 TUI）。"""
    return bool(getattr(_ui_gate(), 'use_ui', False))


def _ask_ui(tool: str, args: dict, reason: str) -> tuple[bool, str | None]:
    """UI 审批的 (allowed, scope)；旧适配器只有 bool 返回时退回单次允许。

    allow_session 只在有「同类」键时打开：解释器、ssh/docker、Git 写操作
    没有可记的类别，弹窗不该摆一个不起作用的 a。
    """
    gate = _ui_gate()
    allow_session = bool(approval_class(tool, args))
    scoped = getattr(gate, 'request_scoped', None)
    if scoped is None:
        return bool(gate.request(tool, args, title=reason)), None
    return scoped(tool, args, title=reason, allow_session=allow_session)


class PermissionGate:
    """登记请求并等待决议；双后端 + 超时熔断 + 审批历史（有界）。

    timeout 是普通属性：构造后直接改 GATE.timeout 即刻生效，长驻服务调参不必重启。
    """

    MAX_HISTORY = 200   # 已决议请求的保留上限，防止长驻服务器无限积累

    def __init__(self, timeout: float | None = None, *,
                 session_id_provider: Callable[[], str] | None = None,
                 reset_grants: Callable[[], None] | None = None,
                 on_timeout: Callable[[PermissionRequest], None] | None = None) -> None:
        """session_id_provider / reset_grants / on_timeout 由 hooks 注入（见模块说明）。

        ``on_timeout`` 在请求因超时被判为 ``timeout`` 后调用一次（已离开 Gate 内部锁）；
        观察者抛异常只打印，不影响拒绝结果 —— 记录观察性事件不能反过来放行操作。
        """
        self.timeout = (float(timeout) if timeout is not None
                        else approval_timeout())
        self._session_id_provider = session_id_provider or (lambda: '')
        self._reset_grants = reset_grants or (lambda: None)
        self._on_timeout = on_timeout
        self.backend = 'console'   # console | remote
        self._lock = threading.RLock()
        self._requests: dict[str, PermissionRequest] = {}
        # 会话级白名单（审批分级）：键 session_allow_key(tool, klass, session_id)，
        # 值是 approval_scope 记录（不再是无期限的 set）：批准一次默认只算
        # 「这一次」（once），要同类免审得显式选作用域。
        self._session_allows: dict[str, dict] = {}
        # LLM 审查官放行缓存：键 = 会话|grant_key（审查批过的精确命令）。
        # 归 Gate 持有：clear()/revoke_session() 连带清空（撤销必须能收回
        # 审查官批出去的免审），且与 _session_allows 同锁防竞态。
        self._judge_approved: dict[str, None] = {}
        self._judge_approved_max = 200
        # 审查官运行统计（/status 展示）与熔断：连续 3 次失败后本会话停用——
        # 供应商挂掉时每批白等 30s 超时是最差体验。成功调用重置连败计数。
        self.judge_stats = {'approved': 0, 'escalated': 0, 'failed': 0}
        self.judge_consecutive_failures = 0
        self.judge_broken = False

    # ---------- 登记 / 查询（供 API 与测试使用） ----------

    def request(self, tool: str, args: dict, reason: str,
                session_id: str | None = None) -> PermissionRequest:
        req = PermissionRequest(
            req_id=f'perm_{uuid.uuid4().hex[:10]}',
            tool=tool,
            reason=reason,
            created_at=time.time(),
            args=approval_view.summarize(tool, dict(args)),
            session_id=self._session_id_provider() if session_id is None else session_id,
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
        """清空请求历史、会话白名单、审查官缓存与本上下文的审批凭据（测试隔离辅助）。"""
        with self._lock:
            self._requests.clear()
            self._session_allows.clear()
            self._judge_approved.clear()
            self.judge_broken = False
            self.judge_consecutive_failures = 0
        self._reset_grants()

    def revoke_session(self, session_id: str | None = None) -> int:
        """撤销指定会话的预授权；没有会话上下文时撤销所有预授权。

        连带清掉 LLM 审查官的放行缓存（键同前缀格式）——撤销的意义是「本会话
        此前给出去的免审全部作废」，审查官批过的命令也是预授权的一种。"""
        prefix = f'{session_id}|' if session_id else None
        with self._lock:
            before = len(self._session_allows) + len(self._judge_approved)
            if prefix is None:
                self._session_allows.clear()
                self._judge_approved.clear()
            else:
                self._session_allows = {
                    key: record for key, record in self._session_allows.items()
                    if not key.startswith(prefix)
                }
                self._judge_approved = {
                    key: None for key in self._judge_approved
                    if not key.startswith(prefix)
                }
            # 撤销意味着安全状态变更：审查官熔断一并复位（重新开始）
            self.judge_broken = False
            self.judge_consecutive_failures = 0
            return before - (len(self._session_allows) + len(self._judge_approved))

    # ---------- LLM 审查官缓存（judge 批过的精确命令） ----------

    def judge_cache_hit(self, key: str) -> bool:
        with self._lock:
            return key in self._judge_approved

    def judge_cache_add(self, key: str) -> None:
        with self._lock:
            while len(self._judge_approved) >= self._judge_approved_max:
                self._judge_approved.pop(next(iter(self._judge_approved)))
            self._judge_approved[key] = None

    def judge_record(self, outcome: str) -> None:
        """记录一次审查结果并维护熔断：连续 3 次失败 → 本会话停用审查官。

        成功/升级重置连败计数；/revoke 与档位切换（revoke_session/clear）
        一并复位熔断——用户主动改变安全状态意味着重新开始。"""
        with self._lock:
            if outcome in self.judge_stats:
                self.judge_stats[outcome] += 1
            if outcome == 'failed':
                self.judge_consecutive_failures += 1
                if self.judge_consecutive_failures >= 3:
                    self.judge_broken = True
            else:
                self.judge_consecutive_failures = 0

    def judge_summary(self) -> str:
        """/status 的一行摘要。"""
        with self._lock:
            stats = self.judge_stats
            tail = ' · 已熔断停用' if self.judge_broken else ''
            # 带上前置分隔符：调用方把它直接拼在别的字段后面，
            # 不带就会读成 "OS 沙箱：offLLM 审查官：…"。
            return (f' · LLM 审查官：放行 {stats["approved"]}'
                    f' · 升级 {stats["escalated"]}'
                    f' · 失败 {stats["failed"]}{tail}')

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
                scope: str | None = None,
                times: int = 0, until: float = 0) -> PermissionRequest:
        """远程决议一个 pending 请求并唤醒等待方。未知 id / 已决议 → 异常。

        scope='session' 且 allow=True 时，把「该会话的同类操作」加入白名单
        （键按登记时的 session_id 分区），之后同会话同类审批自动通过。

        「同类」一律由服务端从登记时的参数重算（approval_class），不接受调用方
        传入：审批请求的 tool/args 才是这次要批准的内容，客户端再带一个 klass
        等于允许「批一个无害请求、顺手预授权 rm」这种提权写法。登记时的摘要保留
        命令首尾（approval_view.summarize），重算结果与发卡时一致；命令被截断时
        省略标记会打断分段归类，这一类拿不到免审键 —— 没看全的命令不预授权。
        """
        with self._lock:
            req = self._requests.get(req_id)
            if req is None:
                raise KeyError(req_id)
            if req.status != 'pending':
                raise RuntimeError(f'request {req_id} already {req.status}')
            req.status = 'approved' if allow else 'denied'
            req.resolved_by = by
            if allow and scope == 'session':
                klass = approval_class(req.tool, req.args)
                if klass:
                    key = session_allow_key(req.tool, klass, req.session_id)
                    with self._lock:
                        self._session_allows[key] = approval_scope.make(
                            scope, times=times, until=until)
            elif allow:
                # 非持久作用域（once / times / until）也要留一条记录：
                # once 用完即失，times 递减，until 到期失效——都在 consume() 里。
                klass = approval_class(req.tool, req.args)
                if klass:
                    key = session_allow_key(req.tool, klass, req.session_id)
                    record = approval_scope.make(scope, times=times, until=until)
                    # once 不存：它只对「刚刚批准的这一次」有效，存了反而会
                    # 让下一次命中。单次授权靠 take_grant 凭据传递。
                    if record['scope'] != approval_scope.ONCE:
                        with self._lock:
                            self._session_allows[key] = record
            req._event.set()
            return req

    # ---------- 会话级白名单（审批分级） ----------

    def session_allows(self, tool: str, args: dict,
                       session_id: str | None = None) -> bool:
        """同类操作是否已被本会话批准。session_id=None 时取当前会话上下文。"""
        key = session_allow_key(
            tool, approval_class(tool, args),
            self._session_id_provider() if session_id is None else session_id)
        with self._lock:
            record = self._session_allows.get(key)
            if record is None:
                return False
            if not approval_scope.is_active(record):
                # 过期/耗尽即删除：不能让一条死记录一直占着键位。
                self._session_allows.pop(key, None)
                return False
            # 在这里消费：命中一次就是「用掉一次」——次数型授权才能递减。
            updated = approval_scope.consume(record)
            if updated is None:
                self._session_allows.pop(key, None)
            else:
                self._session_allows[key] = updated
            return True

    # ---------- 决议入口（enforce 调用） ----------

    def decide(self, tool: str, args: dict, reason: str) -> tuple[bool, str]:
        """登记请求并等待决议，返回 (allowed, message)。阻塞调用方线程。"""
        req = self.request(tool, args, reason)
        if _ui_mode():
            # Textual 正占用终端：input() 读不到，打印会刷坏屏幕；
            # 交给渲染层弹窗应答（弹窗自带超时与取消）。a 的同类范围在
            # resolve 里按登记的参数重算，弹窗只能选择「要不要同类」。
            allowed, scope = _ask_ui(tool, args, reason)
            self.resolve(req.req_id, allowed, by='ui-modal', scope=scope)
            return self._conclude(req, quiet=True)
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
        if _ui_mode():
            allowed, scope = _ask_ui(tool, args, reason)
            self.resolve(req.req_id, allowed, by='ui-modal', scope=scope)
            return self._conclude(req, quiet=True)
        print(f'  \033[33m[permission] '
              f'{approval_view.safe_text(approval_view.tool_label(tool))} 等待审批：'
              f'{approval_view.safe_text(reason)}'
              f'（/approvals 或审批卡片处理，{int(self.timeout)}s 未决议则拒绝）\033[0m')
        self._await_remote(req)
        return self._conclude(req)

    def _await_remote(self, req: PermissionRequest) -> None:
        signalled = req._event.wait(self.timeout)
        if not signalled:
            timed_out = False
            with self._lock:
                if req.status == 'pending':
                    req.status, req.resolved_by = 'timeout', 'gate'
                    timed_out = True
            # 观察者在锁外调用：写会话事件要走文件 I/O，不能卡住别的登记/决议。
            if timed_out:
                self._report_timeout(req)

    def _report_timeout(self, req: PermissionRequest) -> None:
        """通知超时观察者；观察本身失败不能让审批链路改变结果。"""
        callback = self._on_timeout
        if callback is None:
            return
        try:
            callback(req)
        except Exception as exc:
            print(f'  \033[33m[permission] timeout observer failed: '
                  f'{type(exc).__name__}: {exc}\033[0m', file=sys.stderr)

    def _conclude(self, req: PermissionRequest, *,
                  quiet: bool = False) -> tuple[bool, str]:
        """返回 ``(allowed, '<status> [req_id] tool: reason')``。

        首段是终态（approved / denied / timeout），调用方据此区分「用户拒绝」与
        「无人应答」；只把两者都折成一句固定拒绝语，模型和远程端都看不到审批
        通道是否失联。"""
        allowed = req.status == 'approved'
        message = f'{req.status} [{req.req_id}] {req.tool}: {req.reason}'
        status = {'approved': '已允许', 'denied': '已拒绝', 'timeout': '审批超时'}[req.status]
        if not quiet:   # Textual 由弹窗展示结果
            shown = approval_view.safe_text(f'{req.tool}: {req.reason}')
            print(f'  \033[{"32" if allowed else "31"}m[permission] {status} · '
                  f'{shown}\033[0m')
        return allowed, message

    def _decide_console(self, req: PermissionRequest) -> bool:
        """CLI 决议。与审批卡片的三个按钮对齐：y / n / a（同类）。

        a 走的是和远程决议完全相同的一条路（self.resolve），同类键仍由服务端
        按登记的 tool/args 重算。第三方工具默认每次询问，没有卡片的 CLI 必须
        有一条免审出路，否则一个 fetch 类工具能把人问烦。

        approval_class 为空的那一类（解释器、ssh/docker/kubectl、Git 写操作）
        没有「同类」可以记：卡片隐藏这个按钮，CLI 也不能摆一个不起作用的选项，
        否则用户以为自己买了整会话免审，其实只放了这一次。
        """
        print(f"\n\033[33m[approval] {approval_view.safe_text(req.reason)}\033[0m")
        print(f'   操作: {req.tool}')
        for line in approval_view.detail(req.tool, req.args).splitlines():
            print(f'     {line}')
        klass = approval_class(req.tool, req.args)
        try:
            choice = input('Allow? (y=仅这一次 / n=拒绝 / a=本会话允许同类 / '
                           't=允许特 N 次) ' if klass
                           else 'Allow? (y=允许 / n=拒绝) ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            print('  \033[31m[permission] stdin unavailable → denied\033[0m')
            self.resolve(req.req_id, False, by='console(EOF)')
            return False
        if choice == 't' and klass:
            try:
                raw = input('  还能用几次？').strip()
                times = max(1, int(raw))
            except (EOFError, KeyboardInterrupt, ValueError):
                # 输入坏了就当单次：宁可多问一次，不可超发授权。
                print('  \033[33m[permission] 次数无效，按「仅这一次」处理\033[0m')
                choice = 'y'
                times = 0
            else:
                self.resolve(req.req_id, True, by='console',
                             scope='times', times=times)
                print(f'  \033[32m[permission] 同类还能用 {times} 次\033[0m')
                return True
        allow = choice in ('y', 'yes', 'a')
        if choice == 'a' and not klass:
            print('  \033[33m[permission] 这类命令不记「同类」，本次只放行这一次\033[0m')
        # y（默认）不传 scope：走 approval_scope.default_scope()，默认 once。
        self.resolve(req.req_id, allow, by='console',
                     scope='session' if choice == 'a' else None)
        return allow
