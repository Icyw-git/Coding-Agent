"""bash 命令的文本分析：尽力而为，不是解析器。

从 exec_policy 拆出：黑名单命中、包装器剥离、不透明载荷、外发目标、越界写
候选与「同类」授权键都只依赖命令文本，不依赖路径分区与档位快照。exec_policy
继续负责决策矩阵和路径分类，并 re-export 这里的公开名字，既有导入点不变。

⚠ 这里的判定是文本启发式，真正的兜底属于 OS 级沙箱；不要把它当安全边界。
"""

import posixpath
import re

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
    if tool in ('desktop_snapshot', 'desktop_inspect', 'desktop_wait',
                'desktop_displays', 'desktop_windows', 'desktop_action'):
        # 桌面工具一律不给「同会话同类免审」的键：exec_policy 里它们每次都
        # 走 ASK，给个可复用的键等于开了后门。清单从 exec_policy 同步。
        return ''
    if tool in ('read', 'write', 'edit', 'apply_patch'):
        return ''
    if tool == 'web_fetch':
        from web_access import WebAccessError, validate_url

        try:
            return f'web_fetch:{validate_url(str(args.get("url", "") or "")).hostname}'
        except WebAccessError:
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
    if tool == 'apply_patch':
        # 补丁内容本身就是授权范围；cwd 只在非空时参与，空串与缺省等价
        # （handler 重建参数时会省略空的 cwd，两者必须得到同一个键）。
        cwd = str(args.get('cwd', '') or '').strip()
        suffix = f'apply_patch:{args.get("patch", "")}'
        return f'{suffix}@{cwd}' if cwd else suffix
    if tool == 'web_fetch':
        return f'web_fetch:{str(args.get("url", "")).strip()}'
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
