"""执行策略层：一次纯函数决策，替代散落在 hooks / tools / agent_teams 的三套安全判定。

⚠ 边界：本模块只回答「该不该做」，不做 OS 级强制。read-only 档同样是策略级约束
（模型可以被提示词绕过，只是所有入口的判定终于一致）。内核级文件/网络隔离属于
后续阶段（AppContainer / Job Object），不要把它当安全边界对外承诺。

两个档位，命名与 Codex 对齐，默认值保持本项目的历史实际行为：

    sandbox_mode     read-only | workspace-write(默认) | danger-full-access
    approval_policy  untrusted | on-request(默认) | never

    workspace-write + on-request = 重构前的实际行为：黑名单硬拒、破坏性命令询问、
                                   工作区内文件工具读写放行、越界文件工具硬拒。
                                   shell 命令是文本启发式，不能保证写入路径受工作区约束。

调用方负责注入黑名单（hooks / tools 从 harness 读），本模块不 import harness，
因此可脱离运行时单测，也不会因新增全局属性而打爆既有的 monkeypatch 点。
"""

import os
import re
from dataclasses import dataclass

ALLOW = 'allow'
ASK = 'ask'
DENY = 'deny'

SANDBOX_MODES = ('read-only', 'workspace-write', 'danger-full-access')
APPROVAL_POLICIES = ('untrusted', 'on-request', 'never')

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

# 分号/管道/逻辑串联都是「一条命令里的多个动作」，逐段判定。
_SEGMENT_SPLIT = re.compile(r'\||&&|;|&')
_PATH_SPLIT = re.compile(r'[\\/]+')
# bash 里 su""do 会被解析成 sudo，所以命令名要在「去引号」的副本上再取一次
_STRIP_QUOTES = re.compile(r'["\'`]')
# rm -rf --no-preserve-root / 这类「换写法绕过子串」的根删。
_ROOT_DELETE_FLAGS = ('--no-preserve-root', '-rf /', '-fr /')

READ_WRITE_TOOLS = ('write', 'edit')
WORKTREE_TOOLS = {'create_worktree': 'create worktree',
                  'remove_worktree': 'remove worktree',
                  'keep_worktree': 'keep worktree'}


@dataclass(frozen=True)
class Decision:
    """一次工具调用的策略结论。code 是给日志/测试用的稳定标识，reason 给人看。"""
    action: str                     # ALLOW | ASK | DENY
    code: str = ''                  # deny_pattern | destructive | out_of_workspace | ...
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
                 inside_workspace: bool = True) -> Decision:
        """判定一次工具调用。

        deny_list / destructive 由调用方注入（生产上来自 harness 模块全局，
        测试里是自带的假列表）；inside_workspace 由已经解析过路径的调用方给出，
        本模块不重复实现路径逻辑。ASK 之后是问人还是直接拒，由调用方按自己是否
        处在可交互上下文里决定，本模块不关心。
        """
        args = args if isinstance(args, dict) else {}
        if tool == 'bash':
            decision = self._evaluate_bash(str(args.get('command', '') or ''),
                                           deny_list, destructive)
        elif tool in READ_WRITE_TOOLS or tool == 'read':
            decision = self._evaluate_file(tool, args, inside_workspace)
        elif tool in WORKTREE_TOOLS:
            decision = self._evaluate_worktree(tool, args)
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
        if self.approval_policy == 'untrusted':
            return Decision(ASK, 'untrusted', 'all bash commands require approval')

        # 3) danger-full-access：只保留上面的硬黑名单，提示级限制全部取消。
        #    三个档位必须有真实区别，否则配置项只是装饰。
        if self.sandbox_mode == 'danger-full-access':
            return Decision(ALLOW)

        # 4) 询问：破坏性子串（保持既有条目语义）
        for pattern in destructive:
            text = str(pattern).casefold()
            if text and text in normalized:
                return Decision(ASK, 'destructive',
                                f'potentially destructive command ({pattern})',
                                pattern)

        # 5) 询问：不透明载荷（编码脚本 / 管道进解释器），内容静态不可知
        opaque = _opaque_payload(command, normalized, verbs)
        if opaque:
            return Decision(ASK, 'opaque_command',
                            f'opaque command payload ({opaque})')

        # 6) read-only 档：只放公认不改动的命令
        if self.sandbox_mode == 'read-only' and not _looks_read_only(normalized, verbs):
            return Decision(ASK, 'sandbox_read_only',
                            'sandbox is read-only; command may modify state')
        return Decision(ALLOW)

    # ---------- 文件工具 ----------

    def _evaluate_file(self, tool: str, args: dict, inside_workspace: bool) -> Decision:
        if self.sandbox_mode == 'danger-full-access':
            return Decision(ALLOW)
        if not inside_workspace:
            # 工作区边界是 tools.safe_path 的硬检查，批准也改不了结果。旧实现先问
            # 一次「越界访问」再让 safe_path 报错，等于给用户一个不起作用的按钮：
            # 这里直接判硬拒，确需越界读写请切 danger-full-access 档。
            path = str(args.get('path', '') or '')
            return Decision(DENY, 'out_of_workspace',
                            f'access outside workspace ({path})')
        if tool == 'read':
            return Decision(ALLOW)
        if self.sandbox_mode == 'read-only':
            return Decision(ASK, 'sandbox_read_only',
                            f'sandbox is read-only; {tool} needs approval')
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

        never 的语义是「别问我要不要」，于是普通询问直接放行；但 read-only 档的
        询问代表「这个档位不允许」，没有交互可确认时只能硬拒 —— 与 Codex 的
        sandbox_mode=read-only + approval_policy=never 行为一致（写操作直接失败）。
        """
        if decision.action != ASK:
            return decision
        if self.approval_policy != 'never':
            return decision
        if decision.code == 'sandbox_read_only' or self.sandbox_mode == 'read-only':
            return Decision(DENY, decision.code or 'sandbox_read_only',
                            decision.reason, decision.pattern)
        return Decision(ALLOW, decision.code, decision.reason, decision.pattern)


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


def _wrapped_verbs(tokens: list[str]) -> set[str]:
    """从 `cmd /c "del x"` 这类包装里取出被包的命令名，只解一层。

    标志位按原文比对：_basename 会把 '/c' 拆成 'c'（它按路径分段取末段），
    用归一后的值匹配 '/c' 永远匹配不上。
    """
    for index in range(1, min(len(tokens), 4)):
        if tokens[index].strip('"\'').casefold() not in _WRAP_FLAGS:
            continue
        if index + 1 >= len(tokens):
            break
        inner = tokens[index + 1].strip('"\'')
        return {_basename(nested.split()[0])
                for nested in _segments(inner) if nested.split()}
    return set()


def _opaque_payload(command: str, normalized: str, verbs: set[str]) -> str:
    """识别内容不可静态判断的载荷形式，返回命中的特征串。"""
    for flag in _OPAQUE_FLAGS:
        if f' {flag} ' in f' {normalized} ' or f' {flag}: ' in normalized:
            return flag
    if 'invoke-expression' in normalized or 'iex(' in normalized:
        return 'invoke-expression'
    # curl ... | sh / wget ... | bash
    if ('curl' in verbs or 'wget' in verbs) and (
            normalized.count('|') and ('sh' in verbs or 'bash' in verbs or 'zsh' in verbs)):
        return 'pipe-to-shell'
    return ''


def _looks_read_only(normalized: str, verbs: set[str]) -> bool:
    """只放白名单命令；每个串联段单独检查，git 只放明确只读子命令。

    解释器不进白名单，重定向写入也不放行。只检查整条命令的第一个 git 子命令会
    让 `git status && git push` 漏判；`git config` 也能修改全局配置，不能列为只读。
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
    return True


def _git_subcommand(command: str) -> str | None:
    match = re.search(r'\bgit\b\s*([A-Za-z0-9._-]+)', command)
    return match.group(1).casefold() if match else None


# ============================================================
# 同类判定与授权凭据
# ============================================================

def approval_class(tool: str, args) -> str:
    """同类判定：bash 按命令首词（rm/del/git…），其余按工具名。

    hooks 与 feishu_gateway 原本各有一份同规则实现（决议时 / 发卡时两个时机），
    现在统一到这里，两边都从这里导入。args 允许是非 dict（网关侧的宽松输入）。
    """
    if tool == 'bash':
        raw = args.get('command', '') if isinstance(args, dict) else args
        command = str(raw or '').strip()
        return command.split(None, 1)[0].casefold() if command else ''
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
        return f'{tool}:{str(args.get("path", "")).strip()}'
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
