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
                                   shell 命令是文本启发式，不能保证写入路径受工作区约束。

调用方负责注入黑名单（hooks / tools 从 harness 读），本模块不 import harness，
因此可脱离运行时单测，也不会因新增全局属性而打爆既有的 monkeypatch 点。
"""

import os
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

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

# 只读命令白名单：仅 sandbox_mode='read-only' 时用于免审。刻意保守——只收录
# 公认不改动的命令；拿不准的一律询问，比误放行好。不追求穷尽。
READ_ONLY_COMMANDS = frozenset({
    'dir', 'ls', 'cat', 'type', 'head', 'tail', 'findstr', 'grep', 'wc', 'stat',
    'pwd', 'which', 'where', 'whoami', 'hostname', 'date', 'echo', 'ver',
    'git',        # 配合 _git_subcommand 只放只读子命令
})
GIT_READONLY_SUBCOMMANDS = frozenset({
    'status', 'log', 'diff', 'show', 'blame', 'rev-parse', 'ls-files',
})

# 包装器：真实动作藏在后面的 token 里，需要解一层再看命令词。
_WRAPPERS = frozenset({'cmd', 'sh', 'bash', 'zsh', 'powershell', 'pwsh'})
_WRAP_FLAGS = frozenset({'-c', '/c', '-command'})
# 不透明载荷：无法静态判断内容，一律询问（不是拒绝——本地跑一次性脚本是正常需求）。
_OPAQUE_FLAGS = ('-enc', '-encodedcommand', '/enc', '/e', '-enc', '--encodedcommand')
_INTERPRETERS = frozenset({'python', 'python2', 'python3', 'pypy', 'node', 'deno',
                           'ruby', 'perl', 'php', 'lua'})
_OPAQUE_SCRIPT_FLAGS = frozenset({'-c', '-e', '--eval', '-command', '/command'})
_NO_SESSION_GRANT = frozenset({
    'ssh', 'scp', 'sftp', 'docker', 'podman', 'kubectl', 'terraform',
    'npm', 'npx', 'pip', 'uv', 'git-lfs',
})

# 分号/管道/逻辑串联都是「一条命令里的多个动作」，逐段判定。
# 换行在 POSIX shell 里同样分隔命令；Windows cmd 行为不同，但判定层按会执行的
# 最坏情况处理，不能因为平台差异把第二行漏掉。
_SEGMENT_SPLIT = re.compile(r'\||&&|;|&|[\r\n]+')
_PATH_SPLIT = re.compile(r'[\\/]+')
# bash 里 su""do 会被解析成 sudo，所以命令名要在「去引号」的副本上再取一次
_STRIP_QUOTES = re.compile(r'["\'`]')
# rm -rf --no-preserve-root / 这类「换写法绕过子串」的根删。
_ROOT_DELETE_FLAGS = ('--no-preserve-root', '-rf /', '-fr /')

READ_WRITE_TOOLS = ('write', 'edit')
# 只读文件工具：读哪里都放行，但秘密路径与解析失败的路径硬拒。
READ_ONLY_FILE_TOOLS = ('read', 'glob', 'grep', 'ls')

# 外发命令：读已经按路径放开，「发得出去」就是这一轮新增的唯一能力面。
# 这里不问「这个 GET 危不危险」，只保证本机内容离开本机必须经过人同意；
# 名单刻意保守，只收公认的一次性网络客户端，不追求穷尽（python/node 的载荷
# 已经由 _opaque_payload 走审批）。
EGRESS_COMMANDS = frozenset({
    'curl', 'wget', 'http', 'https', 'aria2c', 'nc', 'netcat', 'telnet',
    'invoke-webrequest', 'iwr', 'invoke-restmethod', 'irm',
})
_URL_TARGET = re.compile(r'(?:https?|ftp|ws|wss)://(?:[^/@\s]+@)?([^/?#\s]+)',
                        re.IGNORECASE)
# 带内容或改方法的请求。这类命令即使目标是同一台主机也不记「同类」：
# 批准一次 GET 就免审整会话向该主机发任意文件/任意方法，是一条完整的外传通路。
# `-x` 在 curl 里是代理、在部分客户端里是方法，大小写折叠后一并从严。
EGRESS_SEND_FLAGS = frozenset({
    '-d', '--data', '--data-raw', '--data-binary', '--data-urlencode', '--json',
    '-f', '--form', '--form-string', '-t', '--upload-file',
    '-body', '-infile', '--post-data', '--post-file',
    '-x', '--request',
})
WORKTREE_TOOLS = {'create_worktree': 'create worktree',
                  'remove_worktree': 'remove worktree',
                  'keep_worktree': 'keep worktree'}


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
                 deny_list=(), destructive=(),
                 path_zone: str = PATH_OUTSIDE,
                 external_tools=()) -> Decision:
        """判定一次工具调用。

        deny_list / destructive 由调用方注入（生产上来自 harness 模块全局，
        测试里是自带的假列表）；path_zone 由调用方用 classify_path 解析后给出，
        本模块不重复实现路径逻辑。省略 path_zone 时按 outside 处理：漏判分类的
        文件写入走审批，而不是因为默认「在工作区内」被静默放行。

        ASK 之后是问人还是直接拒，由调用方按自己是否处在可交互上下文里决定，
        本模块不关心。

        external_tools 是调用方登记的第三方工具名（MCP 注册进来的那批）。本模块
        只认识内置工具的副作用，第三方工具能写盘、能发请求，内容静态不可知 ——
        一律询问，而不是猜一个 ALLOW。名单为空时行为与既往完全一致。
        """
        args = args if isinstance(args, dict) else {}
        if tool == 'bash':
            decision = self._evaluate_bash(str(args.get('command', '') or ''),
                                           deny_list, destructive)
        elif tool in READ_WRITE_TOOLS or tool in READ_ONLY_FILE_TOOLS:
            decision = self._evaluate_file(tool, args, path_zone)
        elif tool in WORKTREE_TOOLS:
            decision = self._evaluate_worktree(tool, args)
        elif tool in external_tools:
            decision = Decision(
                ASK, 'external_tool',
                f'{tool} is a third-party tool; its side effects are not '
                'known to the policy layer')
        else:
            decision = Decision(ALLOW)
        return self._apply_approval_policy(decision)

    # ---------- bash ----------

    def _evaluate_bash(self, command: str, deny_list, destructive) -> Decision:
        if not command.strip():
            return Decision(ALLOW)
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
        if any(flag in normalized for flag in _ROOT_DELETE_FLAGS):
            return Decision(DENY, 'deny_root_delete',
                            'denied filesystem-root deletion', '--no-preserve-root')

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

        # 7) read-only 档：只放公认不改动的命令
        if self.sandbox_mode == 'read-only' and not _looks_read_only(normalized, verbs):
            return Decision(ASK, 'sandbox_read_only',
                            'sandbox is read-only; command may modify state')
        return Decision(ALLOW)

    # ---------- 文件工具 ----------

    def _evaluate_file(self, tool: str, args: dict, path_zone: str) -> Decision:
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
            return Decision(ASK, 'sandbox_read_only',
                            f'sandbox is read-only; {tool} needs approval')
        if path_zone == PATH_OUTSIDE:
            return Decision(ASK, 'outside_workspace_write',
                            f'write outside workspace needs approval ({path})')
        return Decision(ALLOW)

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


def classify_path(path: str, *, cwd=None, workspace, secrets=()) -> str:
    """返回 PATH_WORKSPACE / PATH_OUTSIDE / PATH_SECRET / PATH_SYSTEM / PATH_INVALID。

    秘密与系统目录的判定先于工作区判定：工作区内的 `.env`、把工作区设在
    `C:\\Windows` 下，同样不该自动读写。
    """
    try:
        resolved = resolve_tool_path(path, cwd=cwd, workspace=workspace)
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
# 命令文本分析（尽力而为，不是解析器）
# ============================================================

def _is_command_word_pattern(text: str) -> bool:
    """黑名单条目是否是「一个命令名」：不含空格、斜杠、重定向符。"""
    return not any(ch in text for ch in (' ', '/', '\\', '>', '<'))


def _basename(token: str) -> str:
    """取命令名：去引号、去路径、去扩展名，casefold。"""
    token = token.strip().strip('"\'').casefold()
    if not token:
        return ''
    token = _PATH_SPLIT.split(token)[-1]
    return re.sub(r'\.(exe|bat|cmd|ps1|com)$', '', token)


def _segments(command: str) -> list[str]:
    return [part.strip() for part in _SEGMENT_SPLIT.split(command) if part.strip()]


def _command_verbs(command: str) -> set[str]:
    """每条命令段的首个实义命令名，外加 cmd /c、powershell -c 包装后的内层命令名。

    绝对路径调用（C:\\Windows\\System32\\shutdown.exe）由 _basename 归一，
    所以旧子串匹配漏掉的路径写法在这里会还原成 'shutdown'。
    """
    verbs = set()
    for segment in _segments(command):
        tokens = segment.split()
        if not tokens:
            continue
        verb = _basename(tokens[0])
        verbs.add(verb)
        if verb in _WRAPPERS:
            verbs |= _wrapped_verbs(tokens)
    return {verb for verb in verbs if verb}


def egress_hosts(text: str) -> set[str]:
    """命令里出现的网络目标主机（去 userinfo、去单段端口、IPv6 去括号）。

    只认 URL 写法：`nc host 1234` 这类裸主机名参数没有可靠的取值边界，猜出来的
    键会把免审范围放大到任意主机，所以这里宁可不返回目标（同类键为空、每次问）。
    """
    hosts = set()
    for match in _URL_TARGET.finditer(text):
        # 命令里常见带引号的 URL：引号不是主机名的一部分，留着会让两次相同的
        # 请求算出不同的同类键。
        host = match.group(1).casefold().rstrip('"\'')
        if host.startswith('['):                      # [::1]:21 形式
            host = host[1:host.index(']')] if ']' in host else host.strip('[]')
        elif host.count(':') == 1:                    # host:port
            host = host.split(':')[0]
        if host:
            hosts.add(host)
    return hosts


def _wrapped_tokens(tokens: list[str]) -> list[str]:
    """cmd /c "…"、bash -c '…' 里被包装的那段原文；不是包装器写法返回空表。

    标志位按原文比对：_basename 会把 '/c' 拆成 'c'（它按路径分段取末段），
    用归一后的值匹配 '/c' 永远匹配不上。
    """
    for index in range(1, min(len(tokens), 4)):
        if tokens[index].strip('"\'').casefold() in _WRAP_FLAGS:
            return tokens[index + 1:]
    return []


def _wrapped_verbs(tokens: list[str]) -> set[str]:
    """从 `cmd /c "del x"` 这类包装里取出所有被包的命令名，只解一层。"""
    return {_basename(nested.split()[0])
            for nested in _segments(' '.join(_wrapped_tokens(tokens)))
            if nested.split()}


def _wrapped_verb(tokens: list[str]) -> str:
    """包装器载荷里的第一个命令名：`cmd /c "del x"` → del。解不出来返回空串。"""
    for nested in _segments(' '.join(_wrapped_tokens(tokens))):
        words = nested.split()
        if words:
            return _basename(words[0])
    return ''


def _opaque_payload(command: str, normalized: str, verbs: set[str]) -> str:
    """识别内容不可静态判断的载荷形式，返回命中的特征串。"""
    # 命令替换会执行括号里的真实命令，但只解析首词会把它当成 echo 的参数。
    # 不做嵌套解析：识别到就交给人工审批，宁可多问一次。
    if '$(' in normalized or '`' in normalized:
        return 'command substitution'
    for flag in _OPAQUE_FLAGS:
        if f' {flag} ' in f' {normalized} ' or f' {flag}: ' in normalized:
            return flag
    if 'invoke-expression' in normalized or 'iex(' in normalized:
        return 'invoke-expression'
    for segment in _segments(normalized):
        tokens = [token.strip('"\'') for token in segment.split()]
        if not tokens:
            continue
        verb = _basename(tokens[0])
        flags = {token.casefold().split('=', 1)[0] for token in tokens[1:]}
        if verb in _INTERPRETERS:
            if flags & {'--version', '-v', '-V', '--help', '-h'}:
                continue
            return f'{verb} script payload'
        if verb in ('powershell', 'pwsh') and any(
                token.casefold() in ('-command', '-c', '-encodedcommand', '-enc')
                for token in tokens[1:]):
            return f'{verb} command payload'
        if verb in ('curl', 'wget') and flags & EGRESS_SEND_FLAGS:
            return f'{verb} state-changing request'
        if verb in ('ssh', 'scp', 'sftp', 'docker', 'podman', 'kubectl',
                    'terraform', 'npm'):
            return f'{verb} external operation'
        if verb == 'git':
            subcommand = _git_subcommand(segment)
            if subcommand in ('clean', 'push', 'reset', 'checkout', 'switch',
                              'commit', 'rebase', 'merge', 'tag'):
                return f'git {subcommand} operation'
        if verb in _WRAPPERS:
            nested = ' '.join(_wrapped_tokens(tokens))
            if nested and _opaque_payload(nested, nested.casefold(),
                                          _command_verbs(nested)):
                return 'wrapped command payload'
    # curl ... | sh / wget ... | bash
    if ('curl' in verbs or 'wget' in verbs) and (
            normalized.count('|') and ('sh' in verbs or 'bash' in verbs or 'zsh' in verbs)):
        return 'pipe-to-shell'
    return ''


def _looks_read_only(normalized: str, verbs: set[str]) -> bool:
    """只放白名单命令；每个串联段单独检查，git 只放明确只读子命令。

    解释器不进白名单，重定向写入也不放行。只检查整条命令的第一个 git 子命令会
    让 `git status && git push` 漏判；`git config` 也能修改全局配置，不能列为只读。
    部分命令的「只读」取决于参数（`date -s`、`hostname NAME`、`git --output`），
    一起在这里收紧，避免白名单本身成为写入口。
    """
    if not verbs or '>' in normalized:
        return False
    for segment in _segments(normalized):
        segment_verbs = _command_verbs(segment)
        if not segment_verbs or any(verb not in READ_ONLY_COMMANDS
                                    for verb in segment_verbs):
            return False
        if 'git' in segment_verbs:
            subcommand = _git_subcommand(segment)
            if subcommand not in GIT_READONLY_SUBCOMMANDS:
                return False
            if re.search(r'--output(?:=|\s|$)', segment):
                return False
        if 'date' in segment_verbs and re.search(r'(^|\s)(-s|--set)', segment):
            return False
        if 'hostname' in segment_verbs and len(segment.split()) > 1:
            return False
    return True


def is_readonly_command(command: str) -> bool:
    """单条 bash 命令是否落在只读白名单（无重定向、无解释器、无外发）。

    供并行调度使用：整批 bash 全部只读时按无副作用处理，与只读文件工具一样
    可并行执行。这是调度判据，不是安全边界 —— 安全判定仍在 evaluate()，
    两条路径共用同一份白名单（READ_ONLY_COMMANDS / GIT_READONLY_SUBCOMMANDS）。"""
    normalized = str(command or '').casefold()
    if not normalized.strip():
        return True
    verbs = (_command_verbs(normalized)
             | _command_verbs(_STRIP_QUOTES.sub('', normalized)))
    return _looks_read_only(normalized, verbs)


def _git_subcommand(command: str) -> str | None:
    match = re.search(r'\bgit\b\s*([A-Za-z0-9._-]+)', command)
    return match.group(1).casefold() if match else None


# ============================================================
# 同类判定与授权凭据
# ============================================================

def _bash_segment_class(segment: str) -> str | None:
    """一段命令的可免审类别；返回 None 表示这段没有「同类」可记。"""
    words = segment.split()
    if not words:
        return None
    verb = _basename(words[0])
    if verb in ('pwsh', 'powershell'):
        return None
    if verb in _WRAPPERS:
        verb = _wrapped_verb(words) or verb
    if verb in _NO_SESSION_GRANT or verb in _INTERPRETERS:
        return None
    if verb in EGRESS_COMMANDS:
        # httpie 把方法写成位置参数（`http POST host x=1`），aria2c/iwr 也有自己的
        # 写操作开关：静态分不清读写就不记同类，只给 curl/wget 的普通读请求留出路。
        if verb not in ('curl', 'wget'):
            return None
        flags = {token.strip('"\'').casefold().split('=', 1)[0]
                 for token in words[1:]}
        if flags & EGRESS_SEND_FLAGS:
            return None
        hosts = egress_hosts(segment)
        # 「批准这条 curl」买的是「再向这台主机发几次读请求」，不是「任意网络访问」；
        # 带请求体的发送、改方法的请求、目标多于一个或写成裸主机名（nc host 1234）
        # 都不给免审出路。
        return f'{verb}:{hosts.pop()}' if len(hosts) == 1 else None
    if verb == 'git':
        subcommand = _git_subcommand(segment.casefold()) or verb
        if subcommand in {'clean', 'push', 'reset', 'checkout', 'switch',
                          'commit', 'rebase', 'merge', 'tag'}:
            return None
        return subcommand
    return verb


def approval_class(tool: str, args) -> str:
    """同类判定：bash 按命令名（rm/del/git…）+ 外发目标主机，其余按工具名。

    hooks 与 feishu_gateway 原本各有一份同规则实现（决议时 / 发卡时两个时机），
    现在统一到这里，两边都从这里导入。args 允许是非 dict（网关侧的宽松输入）。

    包装器先解一层：`cmd /c del x` 的动作在 del 上，按 cmd 归类等于「批准一条
    cmd /c 就免审所有 cmd /c」，把 `cmd /c rd /s /q` 一起放进去。解不出载荷时
    （起交互式 shell）仍按包装器自身归类。解释器不在包装器名单里：
    `python -c "import os"` 的第一个词是 import，拿它当同类键没有意义。

    复合命令（&&、;、|、换行）要每段归类一致才免审：只按第一段归类的话，批准
    `ls && rm x` 会写下 bash:ls，之后 `ls; rm -rf ~` 就被同一个键放行了 ——
    按钮的含义是「这一条我认了」，不是「第一个动词我认了」。

    文件工具返回空串：一次「写 a.txt」的批准不该免审同会话的任意路径写入，
    尤其是越界写。按目录的范围授权需要先解析 cwd/工作区，留到路径范围化那一轮。
    """
    if tool in ('read', 'write', 'edit'):
        return ''
    if tool == 'bash':
        raw = args.get('command', '') if isinstance(args, dict) else args
        classes: set[str] = set()
        for segment in _segments(str(raw or '')):
            klass = _bash_segment_class(segment)
            if klass is None or (classes and klass not in classes):
                return ''
            classes.add(klass)
        return classes.pop() if len(classes) == 1 else ''
    return tool


def grant_key(tool: str, args: dict) -> str:
    """一次已获批准调用的指纹，用于让 hook 的决议传递到工具边界。

    只取有安全含义的字段：bash 看 command，文件工具看 path，worktree 看名字，
    其余用排序后的参数。这样模型多传一个 offset 也不会让凭据对不上。
    """
    args = args if isinstance(args, dict) else {}
    if tool == 'bash':
        return f'bash:{str(args.get("command", "")).strip()}'
    if tool in ('read', 'write', 'edit'):
        # 相对路径只有和 cwd 一起才指向唯一目标；worktree 里同名的 a.py
        # 不能消费主工作区那次批准。
        cwd = str(args.get('cwd', '') or '').strip()
        suffix = f'{tool}:{str(args.get("path", "")).strip()}'
        return f'{suffix}@{cwd}' if cwd else suffix
    if tool in WORKTREE_TOOLS:
        return f'{tool}:{str(args.get("name", args.get("path", ""))).strip()}'
    return f'{tool}:{sorted((str(k), str(v)) for k, v in args.items())}'


def session_allow_key(tool: str, klass: str, session_id: str | None) -> str:
    """会话白名单键。

    有 session_id 时按会话分区（旧实现是进程级集合：一个会话批过的 rm，
    另一个会话也免审）。没有会话上下文时退回旧格式，保持既有测试与 CLI 单会话
    场景的行为不变。
    """
    base = f'{tool}:{klass}'
    return f'{session_id}|{base}' if session_id else base
