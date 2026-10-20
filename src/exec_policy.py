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
import posixpath
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
# git 的写子命令：既进不透明载荷（要审批），也不给「同类」免审。两处共用
# 同一份名单——分开写两份，迟早一处放行一处拒绝。
GIT_WRITE_SUBCOMMANDS = frozenset({
    'clean', 'push', 'reset', 'checkout', 'switch', 'commit', 'rebase',
    'merge', 'tag',
})

# 包装器：真实动作藏在后面的 token 里，需要解一层再看命令词。
_WRAPPERS = frozenset({'cmd', 'sh', 'bash', 'zsh', 'powershell', 'pwsh'})
_WRAP_FLAGS = frozenset({'-c', '/c', '-command'})
# 前缀包装器：动作在后面的位置参数里（nohup rm … / xargs -n 1 rm …）。
# 按外层名字判定，`nohup <任意命令>` 就整类放行；判定与同类键都要看内层。
_PREFIX_WRAPPERS = frozenset({
    'nohup', 'xargs', 'env', 'time', 'timeout', 'watch',
})
# 这些选项会把紧随其后的一个 token 当取值（`xargs -n 1`、`timeout -s KILL`），
# 剥包装器时连取值一起跳过。
_OPTION_WITH_VALUE = frozenset({
    '-n', '-I', '-P', '-L', '-s', '-E', '-d', '-e', '-u', '-a',
})
# 不透明载荷：无法静态判断内容，一律询问（不是拒绝——本地跑一次性脚本是正常需求）。
_OPAQUE_FLAGS = ('-enc', '-encodedcommand', '/enc', '/e', '-enc', '--encodedcommand')
_INTERPRETERS = frozenset({'python', 'python2', 'python3', 'pypy', 'node', 'deno',
                           'ruby', 'perl', 'php', 'lua'})
_OPAQUE_SCRIPT_FLAGS = frozenset({'-c', '-e', '--eval', '-command', '/command'})
# 外部操作：目标、副作用和出口都不在命令行里（云存储/远程执行/包管理器）。
# 不是拒绝——读云文档、跑一次 pip 都是正常需求；但「安装即执行 postinstall」
# 「cp 到对象存储」这类动作必须经过一次明确同意。只读子命令（docker ps）
# 一并问：判定层不认子命令语义，宁可按最坏情况算（见 DEBUG_LOG 的记录）。
EXTERNAL_OPERATIONS = frozenset({
    'ssh', 'scp', 'sftp', 'rsync', 'docker', 'podman', 'kubectl', 'helm',
    'terraform', 'aws', 'az', 'gcloud', 'gsutil', 's3cmd', 'rclone', 'ossutil',
    'npm', 'npx', 'pnpm', 'yarn', 'pip', 'uv', 'poetry', 'conda',
})
# 进程启动器：参数里才是真正要跑的程序，判定层看不出来。
_LAUNCHER_COMMANDS = frozenset({'start-process', 'start-job', 'invoke-command'})
# 没有「同类」可记：批一次就等于把这一类命令整会话开放。
_NO_SESSION_GRANT = (frozenset({'git-lfs'})
                     | EXTERNAL_OPERATIONS | _LAUNCHER_COMMANDS)

# 分号/管道/逻辑串联都是「一条命令里的多个动作」，逐段判定。
# 换行在 POSIX shell 里同样分隔命令；Windows cmd 行为不同，但判定层按会执行的
# 最坏情况处理，不能因为平台差异把第二行漏掉。
_SEGMENT_SPLIT = re.compile(r'\||&&|;|&|[\r\n]+')
# 带捕获组的分隔符：逐段剥包装器时要原样拼回去，否则 `curl … | sh` 的
# 管道结构会被抹平，opaque 标签就落到别的分支上了。
_SEGMENT_KEEP = re.compile(r'(\||&&|;|&|[\r\n]+)')
_PATH_SPLIT = re.compile(r'[\\/]+')
# bash 里 su""do 会被解析成 sudo，所以命令名要在「去引号」的副本上再取一次
_STRIP_QUOTES = re.compile(r'["\'`]')

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
# 查版本/帮助是只读用法，不该为它弹审批。刻意不含 `-v`：docker 的 -v 是
# 卷挂载、不是版本，按工具区分版本开关不值得，少放行一个不会误事。
QUERY_FLAGS = frozenset({'-V', '--version', '-h', '--help'})

# 会写盘的命令：只有它们的路径参数参与「写到工作区外」判定。读命令
# （cat / find / du …）不进表——读放开是既定策略，不因为路径在工作区外就弹审批。
FILE_WRITE_COMMANDS = frozenset({
    'cp', 'mv', 'install', 'tee', 'dd', 'ln', 'touch', 'mkdir', 'md',
    'sed', 'tar', 'unzip', '7z', 'copy', 'xcopy', 'robocopy', 'move', 'ren',
    'set-content', 'add-content', 'out-file', 'new-item', 'copy-item',
    'move-item',
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
                 deny_list=(), destructive=(), secrets=(),
                 path_zone: str = PATH_OUTSIDE,
                 external_tools=(), workspace: str | None = None) -> Decision:
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
        """
        args = args if isinstance(args, dict) else {}
        if tool == 'bash':
            decision = self._evaluate_bash(str(args.get('command', '') or ''),
                                           deny_list, destructive, secrets,
                                           cwd=args.get('cwd'),
                                           workspace=workspace)
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

    def _evaluate_bash(self, command: str, deny_list, destructive,
                       secrets=(), *, cwd: str | None = None,
                       workspace: str | None = None) -> Decision:
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
            if outside:
                return Decision(ASK, 'outside_workspace_write',
                                f'write outside workspace needs approval ({outside[0]})')

        # 8) read-only 档：只放公认不改动的命令
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


def _rm_targets_root(words: list[str]) -> bool:
    """`rm` 的参数是否带递归+强制开关，且目标就是文件系统根。"""
    flags: set[str] = set()
    targets = []
    for word in words:
        if word == '--':
            continue
        if word.startswith('--'):
            flag = {'--recursive': 'r', '--force': 'f'}.get(word)
            if flag:
                flags.add(flag)
            continue
        if word.startswith('-') and word != '-':
            flags.update(char for char in word[1:] if char in 'rf')
            continue
        targets.append(word)
    return ({'r', 'f'} <= flags
            and any(target.startswith('/')
                    and not posixpath.normpath(target).strip('/*')
                    for target in targets))


def _deletes_filesystem_root(command: str) -> bool:
    """命令里是否有目标为根的 `rm -rf`（含 `/*` 与命令替换）。

    旧实现按子串 `-rf /` 判定，`rm -rf /tmp/a` 也被硬拒。这里按 rm 的 token
    判定：开关可以合并或分开写，目标必须恰是 `/` 或 `/*`。
    """
    for segment in _segments(command):
        for part in re.split(r'[()`]', segment):
            words = _STRIP_QUOTES.sub(' ', part).split()
            for start, word in enumerate(words):
                if _basename(word) == 'rm' and _rm_targets_root(words[start + 1:]):
                    return True
    return False


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
    """从 `cmd /c "del x"` 这类包装里取出所有被包的命令名，只解一层。

    载荷里还可能再包一层前缀包装器（`bash -c 'nohup aws …'`）：先按段剥掉，
    否则 verbs 里只有 nohup，出口/外部操作检查都会漏。
    """
    nested = ' '.join(_wrapped_tokens(tokens))
    nested = _strip_wrappers_in_segments(nested) if nested else nested
    return {_basename(part.split()[0])
            for part in _segments(nested or '')
            if part.split()}


def _wrapped_verb(tokens: list[str]) -> str:
    """包装器载荷里的第一个命令名：`cmd /c "del x"` → del。解不出来返回空串。"""
    nested = ' '.join(_wrapped_tokens(tokens))
    nested = _strip_wrappers_in_segments(nested) if nested else nested
    for part in _segments(nested or ''):
        words = part.split()
        if words:
            return _basename(words[0])
    return ''


def _skip_options(tokens: list[str]) -> list[str]:
    """跳过开头的选项（含取值），返回第一个位置参数起的内容。"""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == '--':
            return tokens[index + 1:]
        if not token.startswith('-') or token == '-':
            break
        index += 1
        if token in _OPTION_WITH_VALUE and index < len(tokens):
            index += 1
    return tokens[index:]


def _skip_env_prefix(tokens: list[str]) -> list[str]:
    """env 的选项与 VAR=1 赋值可以交替出现（`env -u FOO A=1 cmd`）。"""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == '--':
            return tokens[index + 1:]
        if token.startswith('-'):
            index += 1
            if token in _OPTION_WITH_VALUE and index < len(tokens):
                index += 1
            continue
        if '=' in token:
            index += 1
            continue
        break
    return tokens[index:]


def _strip_prefix_wrapper(command: str) -> str | None:
    """剥掉一层前缀包装器，返回内层命令文本；不是前缀包装器返回 None。

    只认形态固定的几个（nohup / xargs / env / time / timeout / watch）：
    timeout 的第一个位置参数是时长，env 前面可能有 VAR=1 赋值，xargs 的
    `-n 1` 之类要把取值一起跳过。剥完为空表示后面没有命令，调用方按
    「不可判定」处理，不猜内层是什么。
    """
    tokens = command.strip().split()
    if not tokens:
        return None
    verb = _basename(tokens[0])
    if verb not in _PREFIX_WRAPPERS:
        return None
    rest = tokens[1:]
    rest = _skip_env_prefix(rest) if verb == 'env' else _skip_options(rest)
    if verb == 'timeout' and rest:
        rest = rest[1:]            # timeout DURATION CMD
    return ' '.join(rest)


def _strip_wrappers_in_segments(command: str) -> str | None:
    """剥掉每个命令段开头的前缀包装器；任一段剥完为空返回 None。

    判定必须按段做：复合命令的每一段都可能包着 nohup/xargs，只剥整条命令
    的开头，`echo x && nohup aws s3 cp .env …` 的后半段就会绕过外部操作与
    秘密外发检查——而同类键本来就是按段算的，判定不能比它更松。
    """
    pieces = []
    for index, piece in enumerate(_SEGMENT_KEEP.split(command)):
        if index % 2:
            pieces.append(piece)      # 分隔符原样保留
            continue
        segment = piece.strip()
        while segment:
            inner = _strip_prefix_wrapper(segment)
            if inner is None:
                break
            if not inner.strip():
                return None
            segment = inner
        pieces.append(segment)
    return ' '.join(pieces)


_REDIRECT_TOKEN = re.compile(r'^[0-9&]*>>?')
_PATHISH_PREFIX = re.compile(r'^(?:[A-Za-z]:|\\\\|~)')
_POSIX_ABSOLUTE = re.compile(r'^/[^/]+/')


def _looks_pathish(token: str) -> bool:
    """显式指向某处的路径：盘符/UNC/家目录/`..` 段，或含两段的 POSIX 绝对路径。

    单段的 `/x` 不认：cmd 与 PowerShell 的开关就长这样（`cmd /c`、`xcopy /s`），
    当路径处理会让包装器命令白白丢掉同类键。URL 不算路径；`build/out.txt`
    这类相对路径按 cwd 解析后本来就在工作区里，不值当为它多问一次。
    """
    token = token.strip('"\'')
    if not token or '://' in token:
        return False
    if _PATHISH_PREFIX.match(token) or _POSIX_ABSOLUTE.match(token):
        return True
    return any(part == '..' for part in re.split(r'[\\/]+', token))


def _write_targets(segment: str) -> list[str]:
    """这一段里会写盘的路径：重定向目标，或写命令的路径参数。

    引号不做语法解析（同其余文本分析）：含空格的路径只会取到前半截，但那段
    通常已足以判定分区；取不到的部分属于 OS 沙箱的职责。
    """
    tokens = segment.split()
    if not tokens:
        return []
    write_verb = _basename(tokens[0]) in FILE_WRITE_COMMANDS
    targets = []
    for index, raw in enumerate(tokens):
        token = raw.strip('"\'')
        redirect = _REDIRECT_TOKEN.match(token)
        if redirect:
            rest = token[redirect.end():]
            if rest:
                targets.append(rest)
            elif index + 1 < len(tokens):
                targets.append(tokens[index + 1].strip('"\''))
            continue
        if not write_verb:
            continue
        # `of=C:/x`、`--output=C:/x`：目标在等号后面
        candidate = token.rsplit('=', 1)[-1] if '=' in token else token
        if _looks_pathish(candidate):
            targets.append(candidate)
    return targets


def _all_write_targets(command: str) -> list[str]:
    """整条命令能静态看到的写目标，含 shell 包装器载荷里的（保序去重）。

    只扫外层段会漏掉 `bash -c 'cp a C:/x'`：外层动词是 bash，候选路径看起来
    只是参数。包装器载荷本来就要按内层命令判定（同 _command_verbs 的规则），
    这里下钻同样一层；总段数设个上限，无限嵌套不是这一层的职责。
    """
    targets = []
    pending = _segments(command)
    budget = 32
    while pending and budget:
        budget -= 1
        segment = pending.pop(0)
        targets.extend(_write_targets(segment))
        tokens = segment.split()
        if tokens and _basename(tokens[0]) in _WRAPPERS:
            nested = ' '.join(_wrapped_tokens(tokens))
            inner = _strip_wrappers_in_segments(nested) if nested else None
            if inner:
                pending.extend(_segments(inner))
    return list(dict.fromkeys(targets))


def _outside_write_targets(command: str, *, cwd, workspace, secrets) -> list[str]:
    """命令里显式写到工作区外或系统目录的目标（去重保序）。"""
    found = []
    for target in _all_write_targets(command):
        if classify_path(target, cwd=cwd, workspace=workspace,
                         secrets=secrets) in (PATH_OUTSIDE, PATH_SYSTEM):
            found.append(target)
    return found


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
    # 按原大小写分段：`-V` 与 `-v` 在外部操作里含义不同（pip -V 是版本，
    # docker -v 是卷挂载），用 casefold 后的文本会把这个区别抹掉。
    for segment in _segments(command):
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
        if verb in EXTERNAL_OPERATIONS:
            # 只认「命令 + 查询开关」这一种形态：参数里任意位置出现 --help 就
            # 放行的话，`npm run build -- --help` 会跟着被当成只读查询。
            # 先按原样比一次再按 casefold 比：`-V` 折叠成 `-v` 会落不到集合里，
            # 而 `-v` 在 docker 里是卷挂载，不能无条件放行。
            if len(tokens) == 2 and (tokens[1] in QUERY_FLAGS
                                     or tokens[1].casefold() in QUERY_FLAGS):
                continue
            return f'{verb} external operation'
        if verb in _LAUNCHER_COMMANDS:
            return f'{verb} launcher'
        if verb == 'git':
            # 这一段按原大小写保留（-V/-v 要区分），git 子命令的匹配自己折叠
            subcommand = _git_subcommand(segment.casefold())
            if subcommand in GIT_WRITE_SUBCOMMANDS:
                return f'git {subcommand} operation'
        if verb in _WRAPPERS:
            nested = ' '.join(_wrapped_tokens(tokens))
            if nested:
                nested = _strip_wrappers_in_segments(nested)
                if nested is None:
                    return 'wrapped command without a payload'
                if _opaque_payload(nested, nested.casefold(), _command_verbs(nested)):
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
    # 同类键必须落在真正执行的内层命令上：按 nohup/xargs 归类，批一次就把
    # 「任意 nohup 命令」放进了本会话白名单。逐层剥，与判定路径同一份规则。
    while verb in _PREFIX_WRAPPERS:
        segment = _strip_prefix_wrapper(segment) or ''
        words = segment.split()
        if not words:
            return None
        verb = _basename(words[0])
    # 写上下文里的路径会触发 outside_workspace_write，而会话白名单排在审批
    # 之前：照旧记 'cp' 的话，批准一次工作区内的拷贝就能免审 `cp .env C:/…`，
    # 刚加的越界写检查被原地旁路。重定向同理（`echo hi > x` 的键不该覆盖
    # `echo hi > C:/…`）。读命令不在此列（读本来放开）；删除命令的免审键
    # 本来就不承诺路径范围，另行处理。
    if _all_write_targets(segment):
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
        if subcommand in GIT_WRITE_SUBCOMMANDS:
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
