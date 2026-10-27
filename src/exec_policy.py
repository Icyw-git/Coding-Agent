"""执行策略层：一次纯函数决策，替代散落在 hooks / tools / agent_teams 的三套安全判定。

⚠ 边界：本模块只回答「该不该做」，不做 OS 级强制。read-only 档同样是策略级约束
（模型可以被提示词绕过，只是所有入口的判定终于一致）。内核级文件/网络隔离属于
后续阶段（AppContainer / Job Object），不要把它当安全边界对外承诺。

两个档位，命名与 Codex 对齐，默认值保持本项目的历史实际行为：

    sandbox_mode     read-only | workspace-write(默认) | danger-full-access
    approval_policy  untrusted | on-request(默认) | never

    workspace-write + on-request = 黑名单硬拒、破坏性命令询问、工作区内文件工具读写
                                   放行、工作区外写改询问、秘密文件读写硬拒、
                                   外发网络请求询问。
                                   shell 是文本启发式：命令里明写的越界路径会询问，
                                   藏在解释器/命令替换里的看不到（那是 OS 沙箱的事）。

调用方负责注入黑名单（hooks / tools 从 harness 读），本模块不 import harness，
因此可脱离运行时单测，也不会因新增全局属性而打爆既有的 monkeypatch 点。
"""

import os
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from policy_bash import (
    _STRIP_QUOTES,
    EGRESS_COMMANDS,
    EGRESS_SEND_FLAGS,
    EXTERNAL_OPERATIONS,
    FILE_WRITE_COMMANDS,
    GIT_READONLY_SUBCOMMANDS,
    GIT_WRITE_SUBCOMMANDS,
    QUERY_FLAGS,
    READ_ONLY_COMMANDS,
    WORKTREE_TOOLS,
    _all_write_targets,
    _command_verbs,
    _deletes_filesystem_root,
    _is_command_word_pattern,
    _looks_read_only,
    _opaque_payload,
    _strip_wrappers_in_segments,
    approval_class,
    egress_hosts,
    grant_key,
    is_readonly_command,
    session_allow_key,
)

# 拆分后保持既有导入点不变：bash 文本分析的公开名字继续从本模块可见。
__all__ = [
    'ALLOW',
    'APPROVAL_ENV',
    'APPROVAL_POLICIES',
    'ASK',
    'CLIPBOARD_ENV',
    'CLIPBOARD_TOOLS',
    'DESKTOP_ENV',
    'DESKTOP_TOOLS',
    'DEFAULT_APPROVAL_POLICY',
    'DEFAULT_SANDBOX_MODE',
    'DENY',
    'Decision',
    'EGRESS_COMMANDS',
    'EGRESS_SEND_FLAGS',
    'EXTERNAL_OPERATIONS',
    'FILE_WRITE_COMMANDS',
    'GIT_READONLY_SUBCOMMANDS',
    'GIT_WRITE_SUBCOMMANDS',
    'LEGACY_APPROVAL_ALL_ENV',
    'PATH_INVALID',
    'PATH_OUTSIDE',
    'PATH_SECRET',
    'PATH_SYSTEM',
    'PATH_WORKSPACE',
    'PUBLIC_SECRET_NAMES',
    'Policy',
    'QUERY_FLAGS',
    'READ_ONLY_COMMANDS',
    'READ_ONLY_FILE_TOOLS',
    'READ_WRITE_TOOLS',
    'SANDBOX_ENV',
    'SANDBOX_MODES',
    'SCREEN_ENV',
    'SCREEN_TOOLS',
    'WORKTREE_TOOLS',
    'approval_class',
    'classify_path',
    'clipboard_tools_enabled',
    'egress_hosts',
    'grant_key',
    'is_readonly_command',
    'is_secret_path',
    'load_policy',
    'resolve_tool_path',
    'screen_tools_enabled',
    'secret_references',
    'session_allow_key',
    'set_tier',
    'system_roots',
]

ALLOW = 'allow'
ASK = 'ask'
DENY = 'deny'

# 文件工具的路径分区：
#   workspace — 当前工作区内，写改免审（read-only 档仍要审批）
#   outside   — 工作区外，可读；写改需审批
#   secret    — 秘密文件，读写都硬拒（与工作区位置无关）
#   system    — 系统目录，默认档只读；写改硬拒（danger-full-access 才放开）
#   invalid   — 路径本身解析不了（空字节、符号链接环），拒绝而不是让 hook 抛异常
PATH_WORKSPACE = 'workspace'
PATH_OUTSIDE = 'outside'
PATH_SECRET = 'secret'
PATH_SYSTEM = 'system'
PATH_INVALID = 'invalid'

SANDBOX_MODES = ('read-only', 'workspace-write', 'danger-full-access')
APPROVAL_POLICIES = ('untrusted', 'on-request', 'never')

# 公开的秘密文件模板：命中 DEFAULT_SECRETS 的通配但不算凭证（见 is_secret_path）。
PUBLIC_SECRET_NAMES = frozenset({
    '.env.example', '.env.sample', '.env.template', '.env.dist', '.env.defaults',
})

SANDBOX_ENV = 'HARNESS_SANDBOX_MODE'
APPROVAL_ENV = 'HARNESS_APPROVAL_POLICY'
LEGACY_APPROVAL_ALL_ENV = 'HARNESS_APPROVAL_ALL'   # =1 等价 approval_policy=untrusted
# 默认档 = 重构前的实际行为：黑名单硬拒 + 破坏性命令询问 + 工作区内写改放行
DEFAULT_SANDBOX_MODE = 'workspace-write'
DEFAULT_APPROVAL_POLICY = 'on-request'
_WARNED: frozenset = frozenset()

READ_WRITE_TOOLS = ('write', 'edit', 'apply_patch')
# 只读文件工具：读哪里都放行，但秘密路径与解析失败的路径硬拒。
READ_ONLY_FILE_TOOLS = ('read', 'view_image', 'read_document',
                        'render_document', 'glob', 'grep', 'ls')
# 屏幕读取：内容可能含密码/私信，每次都问；定时/后台回合由 hooks 直接拒绝。
SCREEN_TOOLS = ('screen_capture', 'screen_ocr')
SCREEN_ENV = 'HARNESS_SCREEN_TOOLS'
# 剪贴板：读可能含密码/私信，写会覆盖用户刚复制的内容；两者都逐次询问。
CLIPBOARD_TOOLS = ('clipboard_read', 'clipboard_write')
CLIPBOARD_ENV = 'HARNESS_CLIPBOARD_TOOLS'
# Desktop input is opt-in and each action requires a human decision.
DESKTOP_TOOLS = ('desktop_snapshot', 'desktop_windows', 'desktop_action')
DESKTOP_ENV = 'HARNESS_DESKTOP_CONTROL'


def desktop_control_enabled(environ=None) -> bool:
    """Disabled unless explicitly enabled with HARNESS_DESKTOP_CONTROL=1."""
    raw = (environ if environ is not None else os.environ).get(DESKTOP_ENV, '')
    return str(raw).strip().casefold() in ('1', 'true', 'yes', 'on', 'enabled')


def screen_tools_enabled(environ=None) -> bool:
    """默认开；显式 `HARNESS_SCREEN_TOOLS=0/off/false` 关闭。"""
    raw = (environ if environ is not None else os.environ).get(SCREEN_ENV)
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().casefold() not in ('0', 'off', 'false', 'no', 'disabled')


def clipboard_tools_enabled(environ=None) -> bool:
    """默认开；显式 `HARNESS_CLIPBOARD_TOOLS=0/off/false` 关闭。"""
    raw = (environ if environ is not None else os.environ).get(CLIPBOARD_ENV)
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().casefold() not in ('0', 'off', 'false', 'no', 'disabled')


@dataclass(frozen=True)
class Decision:
    """一次工具调用的策略结论。code 是给日志/测试用的稳定标识，reason 给人看。"""
    action: str                     # ALLOW | ASK | DENY
    code: str = ''                  # deny_pattern | destructive | outside_workspace_write | ...
    reason: str = ''
    pattern: str = ''               # 命中的黑名单条目（用于还原既有拒绝文案）

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    @property
    def denied(self) -> bool:
        return self.action == DENY


@dataclass(frozen=True)
class Policy:
    """从环境解析出的不可变档位快照。"""
    sandbox_mode: str = DEFAULT_SANDBOX_MODE
    approval_policy: str = DEFAULT_APPROVAL_POLICY

    def evaluate(self, tool: str, args: dict, *,
                 deny_list=(), destructive=(), secrets=(),
                 path_zone: str = PATH_OUTSIDE,
                 external_tools=(), workspace: str | None = None,
                 write_grants=(), paths=None, net_rules=()) -> Decision:
        """判定一次工具调用。

        deny_list / destructive / secrets 由调用方注入（生产上分别来自 harness
        模块全局与 checkpoints.DEFAULT_SECRETS，测试里是自带的假列表）——
        本模块不反向依赖那些层。secrets 省略时 bash 不做秘密文件名检查，
        生产入口 hooks.evaluate_call 必须传；path_zone 由调用方用 classify_path
        解析后给出，本模块不重复实现路径逻辑。省略 path_zone 时按 outside 处理：
        漏判分类的文件写入走审批，而不是因为默认「在工作区内」被静默放行。
        workspace 是 bash 越界写检查的根：生产入口传 get_workdir()，省略时
        退回进程 cwd（不 fail-open：相对路径按 cwd 解析，越界的绝对路径照样问）。

        ASK 之后是问人还是直接拒，由调用方按自己是否处在可交互上下文里决定，
        本模块不关心。

        external_tools 是调用方登记的第三方工具名（MCP 注册进来的那批）。本模块
        只认识内置工具的副作用，第三方工具能写盘、能发请求，内容静态不可知 ——
        一律询问，而不是猜一个 ALLOW。名单为空时行为与既往完全一致。

        write_grants 是人显式给出的持久目录授权（已解析的绝对路径）。它只放宽
        PATH_OUTSIDE 的写入审批：秘密/系统目录仍在授权之前硬拒，read-only 档
        也仍按只读档询问。paths 用于 apply_patch 这类一次涉及多个路径的调用；
        缺省时只看 args['path']。
        """
        args = args if isinstance(args, dict) else {}
        if tool == 'bash':
            decision = self._evaluate_bash(str(args.get('command', '') or ''),
                                           deny_list, destructive, secrets,
                                           cwd=args.get('cwd'),
                                           workspace=workspace,
                                           write_grants=write_grants)
        elif tool in READ_WRITE_TOOLS or tool in READ_ONLY_FILE_TOOLS:
            decision = self._evaluate_file(tool, args, path_zone,
                                           workspace=workspace,
                                           write_grants=write_grants,
                                           paths=paths)
        elif tool in WORKTREE_TOOLS:
            decision = self._evaluate_worktree(tool, args)
        elif tool == 'web_fetch':
            decision = self._evaluate_web(args, net_rules)
        elif tool in SCREEN_TOOLS:
            if screen_tools_enabled():
                decision = Decision(
                    ASK, 'screen_access',
                    f'{tool} reads the local screen, which may contain '
                    'passwords or private messages')
            else:
                decision = Decision(
                    DENY, 'screen_tools_disabled',
                    f'{tool} is disabled ({SCREEN_ENV}=0)')
        elif tool in CLIPBOARD_TOOLS:
            if clipboard_tools_enabled():
                if tool == 'clipboard_write':
                    decision = Decision(
                        ASK, 'clipboard_write',
                        f'{tool} replaces the local clipboard, overwriting '
                        'what the user copied')
                else:
                    decision = Decision(
                        ASK, 'clipboard_access',
                        f'{tool} reads the local clipboard, which may contain '
                        'passwords or private text')
            else:
                decision = Decision(
                    DENY, 'clipboard_tools_disabled',
                    f'{tool} is disabled ({CLIPBOARD_ENV}=0)')
        elif tool in DESKTOP_TOOLS:
            if not desktop_control_enabled():
                decision = Decision(
                    DENY, 'desktop_control_disabled',
                    f'{tool} is disabled ({DESKTOP_ENV}=1 is required)')
            elif self.approval_policy == 'never':
                decision = Decision(
                    DENY, 'desktop_control_approval_required',
                    'desktop control always requires explicit per-action approval')
            else:
                decision = Decision(
                    ASK, 'desktop_control',
                    f'{tool} inspects or controls the local foreground desktop; '
                    'each operation requires explicit approval')
        elif tool in external_tools:
            decision = Decision(
                ASK, 'external_tool',
                f'{tool} is a third-party tool; its side effects are not '
                'known to the policy layer')
        else:
            decision = Decision(ALLOW)
        return self._apply_approval_policy(decision)

    # ---------- bash ----------

    def _evaluate_bash(self, command: str, deny_list, destructive,
                       secrets=(), *, cwd: str | None = None,
                       workspace: str | None = None,
                       write_grants=()) -> Decision:
        if not command.strip():
            return Decision(ALLOW)
        # 0) 前缀包装器：真正执行的是内层命令。按外层名字判定，等于
        #    `nohup <任意命令>` 整类放行。每个命令段都要剥（复合命令的
        #    后半段同样能藏一个包装器），剥完是空就按不可判定处理，不猜。
        unwrapped = _strip_wrappers_in_segments(command)
        if unwrapped is None:
            return Decision(ASK, 'opaque_command',
                            'wrapped command without a payload '
                            '(cannot judge what will run)')
        command = unwrapped
        normalized = command.casefold()
        # bash 会把 su""do 解析成 sudo：去引号后再取一次命令名，两种写法都覆盖
        verbs = _command_verbs(normalized) | _command_verbs(_STRIP_QUOTES.sub('', normalized))

        # 1) 硬拒：短词按命令名精确匹配，含空格/路径/重定向的按子串匹配。
        #    旧实现一律子串匹配，于是绝对路径调用（C:\Windows\System32\shutdown.exe）
        #    与引号拆分都能漏网；现在命令名做 token 归一。仍是尽力而为的文本分析，
        #    不是 shell 解析器 —— 真正的兜底属于 OS 级沙箱，不属于这一层。
        for pattern in deny_list:
            text = str(pattern).casefold().strip()
            if not text:
                continue
            if _is_command_word_pattern(text):
                if text in verbs:
                    return Decision(DENY, 'deny_pattern',
                                    f'denied command {pattern}', pattern)
            elif text in normalized:
                return Decision(DENY, 'deny_pattern',
                                f'denied command {pattern}', pattern)
        if '--no-preserve-root' in normalized:
            return Decision(DENY, 'deny_root_delete',
                            'denied filesystem-root deletion',
                            '--no-preserve-root')
        if _deletes_filesystem_root(normalized):
            return Decision(DENY, 'deny_root_delete',
                            'denied filesystem-root deletion', 'rm -rf /')

        # 1.5) 秘密文件外发：文件工具的路径分区管不到 bash 的 argv。命令里点名
        #      .env / 私钥，又同时出现出口或外部操作命令，就升级人工——不做路径
        #      解析，也不硬拒：bash 是文本分析，`grep .env docs` 这类正常用法
        #      误伤面比文件工具大，要不要拦由审批人看完整条命令决定。
        refs = secret_references(command, secrets)
        if refs and verbs & (EGRESS_COMMANDS | EXTERNAL_OPERATIONS):
            return Decision(ASK, 'secret_egress',
                            f'outbound command references protected file ({refs[0]})')

        # 2) 全量审批档（旧 HARNESS_APPROVAL_ALL=1 的等价语义）。排在档位短路之前，
        #    这样 danger-full-access + untrusted = 「不做启发式拦截，但每条都问我」。
        #    「每条都问」还要求 hooks.enforce 不在会话白名单上短路：白名单本身就是一次
        #    预授权，命中它等于用一个按钮把档位悄悄改回 on-request。
        if self.approval_policy == 'untrusted':
            return Decision(ASK, 'untrusted', 'all bash commands require approval')

        # 3) danger-full-access：只保留上面的硬黑名单，提示级限制全部取消。
        #    三个档位必须有真实区别，否则配置项只是装饰。
        if self.sandbox_mode == 'danger-full-access':
            return Decision(ALLOW)

        # 4) 询问：破坏性命令。单词条目（rm / del / rd…）按「去引号后的 token 或
        #    命令动词」匹配，不再子串匹配 —— 'confirm ' / 'delegate ' / 'delete '
        #    含 'rm ' / 'del ' 曾把无害命令卷进审批。含空格/路径的条目
        #    （chmod 777、> /etc/）保留子串语义，覆盖参数位置的写入。
        tokens = set(_STRIP_QUOTES.sub(' ', normalized).split())
        for pattern in destructive:
            text = str(pattern).casefold().strip()
            if not text:
                continue
            if _is_command_word_pattern(text):
                if text in verbs or text in tokens:
                    return Decision(ASK, 'destructive',
                                    f'potentially destructive command ({pattern})',
                                    pattern)
            elif text in normalized:
                return Decision(ASK, 'destructive',
                                f'potentially destructive command ({pattern})',
                                pattern)

        # 5) 询问：不透明载荷（编码脚本 / 管道进解释器），内容静态不可知
        opaque = _opaque_payload(command, normalized, verbs)
        if opaque:
            return Decision(ASK, 'opaque_command',
                            f'opaque command payload ({opaque})')

        # 6) 询问：外发网络请求。GET 也算——边界问的不是「这个请求危不危险」，
        #    而是「本机要不要把东西交出去」；读放开之后，没有这一条就等于把
        #    exfiltration 通道留在审批面之外。排在载荷判定之后，是因为
        #    `curl … | sh` 更需要「远程脚本」这个标签，而不是「一次请求」。
        egress = verbs & EGRESS_COMMANDS
        if egress:
            hosts = egress_hosts(command)
            target = f'to {", ".join(sorted(hosts))}' if hosts \
                else 'to an unrecognized destination'
            return Decision(ASK, 'network_egress',
                            f'outbound network request ({sorted(egress)[0]} {target})')

        # 7) 询问：命令里明写着写到工作区外（或系统目录）的路径。文件工具有
        #    路径分区，bash 的 argv 没有；这里补的是「命令里写着路径」的那部分。
        #    只认显式写法（盘符/根/`..` 逃逸），相对路径按 cwd 解析后本来就在
        #    工作区内；读命令不参与（读放开），解释器字符串与命令替换里的路径
        #    看不见——文本分析的边界，真正的写入约束属于 OS 沙箱。
        if self.sandbox_mode == 'workspace-write':
            root = workspace or str(Path.cwd())
            outside = _outside_write_targets(command, cwd=cwd or root,
                                             workspace=root, secrets=secrets)
            if outside and write_grants:
                outside = [
                    target for target in outside
                    if not _grant_covers(target, cwd=cwd or root,
                                         workspace=root, secrets=secrets,
                                         write_grants=write_grants)
                ]
            if outside:
                return Decision(ASK, 'outside_workspace_write',
                                f'write outside workspace needs approval ({outside[0]})')

        # 8) read-only 档：只放公认不改动的命令
        if self.sandbox_mode == 'read-only' and not _looks_read_only(normalized, verbs):
            return Decision(ASK, 'sandbox_read_only',
                            'sandbox is read-only; command may modify state')
        return Decision(ALLOW)

    # ---------- 文件工具 ----------

    def _evaluate_file(self, tool: str, args: dict, path_zone: str, *,
                       workspace: str | None = None,
                       write_grants=(), paths=None) -> Decision:
        path = str(args.get('path', '') or '')
        if path_zone == PATH_INVALID:
            return Decision(DENY, 'invalid_path', f'invalid path ({path})')
        if path_zone == PATH_SECRET:
            return Decision(DENY, 'secret_path',
                            f'access to protected path ({path})')
        # 系统目录是防误操作，不是数据保护：完全自主档可以写，默认档不允许
        # 一次「批准」就把文件写进 Windows / Program Files。
        if path_zone == PATH_SYSTEM:
            if tool in READ_ONLY_FILE_TOOLS or self.sandbox_mode == 'danger-full-access':
                return Decision(ALLOW)
            return Decision(DENY, 'system_path',
                            f'write to protected system path ({path})')
        if self.sandbox_mode == 'danger-full-access':
            return Decision(ALLOW)
        if tool in READ_ONLY_FILE_TOOLS:
            # 读是开放的：项目外的文档、日志、配置本来就是个人助手的日常输入。
            return Decision(ALLOW)
        if self.sandbox_mode == 'read-only':
            # 持久授权放宽的是「要不要问越界写」，不是只读档本身：换档仍要人确认。
            return Decision(ASK, 'sandbox_read_only',
                            f'sandbox is read-only; {tool} needs approval')
        if path_zone == PATH_OUTSIDE:
            targets = list(paths) if paths else [path]
            if (write_grants and _file_paths_granted(
                    targets, cwd=args.get('cwd'), workspace=workspace,
                    write_grants=write_grants)):
                return Decision(ALLOW, 'path_grant',
                                f'persistent path grant covers ({path})')
            return Decision(ASK, 'outside_workspace_write',
                            f'write outside workspace needs approval ({path})')
        return Decision(ALLOW)

    # ---------- 联网 ----------

    def _evaluate_web(self, args: dict, net_rules=()) -> Decision:
        """外发请求与 curl 同语义：无论档位都要审批（exfiltration 是边界问题）。

        这里只做语法校验，不解析 DNS：策略层保持纯函数，私网/重定向校验在
        网络层（web_access.check_url）发出请求前完成。

        net_rules 是人显式给出的域名名单（``((pattern, scope), ...)``）。它只影响
        「要不要问」：deny 直接硬拒，allow 跳过审批；没命中就维持默认询问。
        名单不能绕过 URL 语法校验，也不会让私网地址变合法。
        """
        from web_access import WebAccessError, validate_url

        url = str(args.get('url', '') or '')
        try:
            parsed = validate_url(url)
        except WebAccessError as exc:
            return Decision(DENY, 'invalid_url', f'invalid URL ({exc})')
        host = parsed.hostname or ''
        if net_rules:
            from net_policy import ALLOW as NET_ALLOW
            from net_policy import DENY as NET_DENY
            from net_policy import classify

            verdict = classify(host, net_rules)
            if verdict == NET_DENY:
                return Decision(DENY, 'domain_denied',
                                f'network access to {host} is denied by the domain policy')
            if verdict == NET_ALLOW:
                return Decision(ALLOW, 'domain_allowed',
                                f'network access to {host} is allowed by the domain policy')
        return Decision(ASK, 'network_egress',
                        f'outbound network request (web_fetch to {host})')

    # ---------- worktree ----------

    def _evaluate_worktree(self, tool: str, args: dict) -> Decision:
        # 这三个工具会动 git 分支和工作树，旧实现完全不在判定范围内：
        # remove_worktree(discard_changes=True) 会跑 `git worktree remove --force`
        # 加 `git branch -D`，属于破坏性操作。
        if self.sandbox_mode == 'danger-full-access' or tool != 'remove_worktree':
            return Decision(ALLOW)
        if args.get('discard_changes'):
            return Decision(ASK, 'destructive',
                            'discarding uncommitted changes in a worktree')
        if self.sandbox_mode == 'read-only':
            return Decision(ASK, 'sandbox_read_only',
                            'sandbox is read-only; removing a worktree needs approval')
        return Decision(ALLOW)

    # ---------- 档位收口 ----------

    def _apply_approval_policy(self, decision: Decision) -> Decision:
        """approval_policy 只调节「询问」的去向，永不放松「硬拒」。

        never 的语义是「别问我要不要」。Codex 敢让询问直接通过，是因为 workspace-write
        有 OS 沙箱兜底；本项目 Stage 1 没有，放行等于静默执行 rm -rf / curl | sh。
        所以除 danger-full-access（用户显式选择完全自主）外，需要审批的操作一律失败，
        与 read-only + never 的既有行为保持一致。
        """
        if decision.action != ASK:
            return decision
        if self.approval_policy != 'never':
            return decision
        if self.sandbox_mode == 'danger-full-access':
            return Decision(ALLOW, decision.code, decision.reason, decision.pattern)
        return Decision(DENY, decision.code, decision.reason, decision.pattern)


# ============================================================
# 配置解析
# ============================================================

def load_policy(environ=None) -> Policy:
    """每次调用即时读取，所以 /sandbox 与审批档切换无需重启即可生效。

    非法值不静默降级：按默认档处理并提示（同一条只提示一次，避免每次工具调用
    刷屏）——静默回落会让用户以为开了保护其实没开。
    """
    env = os.environ if environ is None else environ
    mode = (env.get(SANDBOX_ENV) or '').strip().casefold()
    if not mode:
        mode = DEFAULT_SANDBOX_MODE
    elif mode not in SANDBOX_MODES:
        _warn(f'unknown {SANDBOX_ENV} {mode!r}, using {DEFAULT_SANDBOX_MODE}')
        mode = DEFAULT_SANDBOX_MODE
    approval = (env.get(APPROVAL_ENV) or '').strip().casefold()
    if not approval:
        # 旧开关只在没有显式配置新档位时兜底：/sandbox approvals 的选择不该被
        # 一个遗留环境变量悄悄覆盖掉。
        approval = ('untrusted'
                    if (env.get(LEGACY_APPROVAL_ALL_ENV) or '').strip() == '1'
                    else DEFAULT_APPROVAL_POLICY)
    elif approval not in APPROVAL_POLICIES:
        _warn(f'unknown {APPROVAL_ENV} {approval!r}, using {DEFAULT_APPROVAL_POLICY}')
        approval = DEFAULT_APPROVAL_POLICY
    return Policy(mode, approval)


def set_tier(sandbox_mode: str | None = None, approval_policy: str | None = None,
             environ=None) -> Policy:
    """运行时改档位：写回环境并返回生效后的快照（load_policy 即读即用）。

    非法值直接抛 ValueError —— 打错一个档位名却「设置成功」是最糟的失败方式。
    """
    env = os.environ if environ is None else environ
    if sandbox_mode is not None:
        env[SANDBOX_ENV] = _checked(sandbox_mode, SANDBOX_MODES, SANDBOX_ENV)
    if approval_policy is not None:
        env[APPROVAL_ENV] = _checked(approval_policy, APPROVAL_POLICIES, APPROVAL_ENV)
    return load_policy(env)


def _checked(value: str, allowed, label: str) -> str:
    normalized = value.strip().casefold()
    if normalized not in allowed:
        raise ValueError(f'unknown {label} {value!r}; choose one of '
                         f'{", ".join(allowed)}')
    return normalized


def _warn(message: str) -> None:
    global _WARNED
    if message in _WARNED:
        return
    _WARNED = _WARNED | {message}   # 复制而非原地改：便于测试整体重置
    import sys
    print(f'[policy] {message}', file=sys.stderr)


# ============================================================
# 路径分类（文件工具与策略层共用）
# ============================================================

def resolve_tool_path(path: str, *, cwd=None, workspace) -> Path:
    """把工具参数解析成绝对路径。

    相对路径基于 cwd（会话/工作树），没有 cwd 时基于工作区；`~` 按用户目录
    展开。先 resolve 再判分区，符号链接/junction 指向哪里就按哪里算。
    """
    base = Path(cwd).expanduser().resolve() if cwd else Path(workspace).resolve()
    candidate = Path(path).expanduser()
    return (candidate if candidate.is_absolute() else base / candidate).resolve()


def is_secret_path(path, secrets=()) -> bool:
    """文件名是否命中秘密列表（大小写不敏感，与 Windows 文件名语义一致）。

    `.env.*` 会连 `.env.example` 这类公开模板一起盖住；模板本来就是给开发者抄的，
    硬拒只会把正常工作流赶进 bash，所以在这里点名放行（仍不匹配私钥同名文件）。
    """
    name = Path(path).name.casefold()
    if name in PUBLIC_SECRET_NAMES:
        return False
    return any(fnmatchcase(name, str(pattern).casefold())
               for pattern in secrets if str(pattern))


def secret_references(command: str, secrets=()) -> list[str]:
    """命令 token 里点名到的秘密文件（按文件名匹配，去重保序）。

    只服务 bash 判定：不解析 argv 语义，`-T .env` 和 `cp .env` 一样看得见。
    调用方决定升级还是硬拒；名单为空时不做任何工作。
    """
    if not secrets:
        return []
    found = []
    for token in _STRIP_QUOTES.sub('', command).split():
        token = token.strip('"\'')
        if token and token not in found and is_secret_path(token, secrets):
            found.append(token)
    return found


def system_roots() -> tuple[Path, ...]:
    """系统目录的写保护范围（读不受限）。

    Windows 取 SystemRoot 与 Program Files 两项；POSIX 取常见的系统前缀。
    不追求穷尽——这里是防误写的硬拦，不是完整的 ACL 复刻。
    """
    if os.name == 'nt':
        names = ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)')
        raw = [os.environ.get(name) for name in names]
        raw.append(r'C:\Windows')
    else:
        raw = ['/etc', '/usr', '/bin', '/sbin', '/boot', '/dev', '/proc', '/sys']
    roots = []
    for value in raw:
        if not value:
            continue
        try:
            root = Path(value).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def classify_path(path: str, *, cwd=None, workspace, secrets) -> str:
    """返回 PATH_WORKSPACE / PATH_OUTSIDE / PATH_SECRET / PATH_SYSTEM / PATH_INVALID。

    秘密与系统目录的判定先于工作区判定：工作区内的 `.env`、把工作区设在
    `C:\\Windows` 下，同样不该自动读写。
    ⚠ NUL 字节显式检查：Windows 的 Path.resolve() 对含 \\x00 的路径不抛异常
    （str 尾部静默截断），invalid 分支会不可达、含 NUL 路径被当工作区文件放行。
    """
    text = str(path or '')
    if '\x00' in text:
        return PATH_INVALID
    try:
        resolved = resolve_tool_path(text, cwd=cwd, workspace=workspace)
        workspace = Path(workspace).resolve()
    except (OSError, RuntimeError, ValueError):
        return PATH_INVALID
    if is_secret_path(resolved, secrets):
        return PATH_SECRET
    if any(resolved.is_relative_to(root) for root in system_roots()):
        return PATH_SYSTEM
    if resolved.is_relative_to(workspace):
        return PATH_WORKSPACE
    return PATH_OUTSIDE


# ============================================================
# bash 越界写检查（依赖本模块的路径分区）
# ============================================================

def _outside_write_targets(command: str, *, cwd, workspace, secrets) -> list[str]:
    """命令里显式写到工作区外或系统目录的目标（去重保序）。"""
    found = []
    for target in _all_write_targets(command):
        if classify_path(target, cwd=cwd, workspace=workspace,
                         secrets=secrets) in (PATH_OUTSIDE, PATH_SYSTEM):
            found.append(target)
    return found


# ============================================================
# 持久目录授权（grants）的覆盖判定
# ============================================================
# 授权只在「工作区外、且不是秘密/系统目录」这一格里起作用。系统目录即使有人
# 手工往账本里塞了路径也不放宽 —— 重新分类而不是直接信任授权表，避免账本被
# 误编辑后变成提权通道。

def _grant_covers(target: str, *, cwd, workspace, secrets, write_grants) -> bool:
    """bash 的单个写入目标是否被某条持久授权覆盖。"""
    if not write_grants:
        return False
    try:
        # 授权只放宽 PATH_OUTSIDE；秘密/系统目录命中时保持原判定。
        if classify_path(target, cwd=cwd, workspace=workspace,
                         secrets=secrets) != PATH_OUTSIDE:
            return False
        resolved = resolve_tool_path(target, cwd=cwd, workspace=workspace)
    except (OSError, RuntimeError, ValueError):
        return False
    return any(resolved == grant or resolved.is_relative_to(grant)
               for grant in write_grants)


def _file_paths_granted(paths, *, cwd, workspace, write_grants) -> bool:
    """文件工具的所有目标是否都在授权目录内（工作区内路径不计）。"""
    if not write_grants or not paths:
        return False
    base = workspace or cwd or str(Path.cwd())
    root = Path(base).resolve()
    for raw in paths:
        try:
            resolved = resolve_tool_path(str(raw), cwd=cwd, workspace=root)
        except (OSError, RuntimeError, ValueError):
            return False
        if resolved.is_relative_to(root):
            continue
        if not any(resolved == grant or resolved.is_relative_to(grant)
                   for grant in write_grants):
            return False
    return True
