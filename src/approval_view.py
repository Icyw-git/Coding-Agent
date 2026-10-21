"""审批面渲染：把「这次要批准什么」压到可控大小，再转成人眼可读的安全文本。

两个时机、两份职责，别混用：

    summarize(tool, args)  登记时（PermissionGate.request）—— 决定审批历史、
                           HTTP 响应、消息卡片里出现的参数上限；
    detail(tool, args)     展示时（CLI 提示、/approvals、审批卡片）—— 命令整段
                           可读，不可见字符换成 \\xNN。

bash 的 command 是唯一按「留头尾、中间省略」处理的字段：它就是审批对象，只留
前 200 字符会把同一条命令里的后半段动作（第二个管道段、重定向目标）切掉。首词
必须保住 —— 审批人要先看清这条命令在做什么（exec_policy.approval_class 读的是
登记后的摘要）。被截断的命令不提供「本次会话允许同类」：省略标记独占一行，
按段归类与头尾对不上，判定键为空；审批人没看到全貌，就不该顺手预授权一整类。
"""
import re

# 普通参数值：短摘要即可，write 的整份 content、MCP 的大参数不该进审批面。
PREVIEW_CHARS = 200
MAX_ITEMS = 20
MAX_DEPTH = 2
# 命令：200 字符不足以看清一条真实命令，按头 1200 / 尾 400 保留。
COMMAND_HEAD_CHARS = 1200
COMMAND_TAIL_CHARS = 400

# tab 与换行保留（多行命令按行才看得清），其余 C0 与 DEL 都会被终端或卡片当成
# 控制序列解析：让被审批的命令去改写审批界面，等于审批提示可以被注入。
_CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')


def _escape(match: re.Match) -> str:
    return f'\\x{ord(match.group()):02x}'


def safe_text(value) -> str:
    """终端/卡片可见文本：不可见字符换成转义写法，内容语义不变。"""
    return _CONTROL.sub(_escape, value if isinstance(value, str) else str(value))


def _compact_value(value, depth: int):
    if isinstance(value, str):
        if len(value) <= PREVIEW_CHARS:
            return value
        return value[:PREVIEW_CHARS] + f'… ({len(value)} chars)'
    if depth <= 0:
        return '…'
    if isinstance(value, dict):
        items = list(value.items())
        compacted = {str(key): _compact_value(item, depth - 1)
                     for key, item in items[:MAX_ITEMS]}
        if len(items) > MAX_ITEMS:
            compacted['…'] = f'{len(items) - MAX_ITEMS} more keys'
        return compacted
    if isinstance(value, (list, tuple)):
        compacted = [_compact_value(item, depth - 1)
                     for item in value[:MAX_ITEMS]]
        if len(value) > MAX_ITEMS:
            compacted.append(f'… ({len(value)} items)')
        return compacted
    return value


def _summarize_command(command: str) -> str:
    omitted = len(command) - COMMAND_HEAD_CHARS - COMMAND_TAIL_CHARS
    if omitted <= 0:
        return command
    return (command[:COMMAND_HEAD_CHARS]
            + f'\n… 中间省略 {omitted} 字符 …\n'
            + command[-COMMAND_TAIL_CHARS:])


def summarize(tool: str, args: dict) -> dict:
    """登记用的参数摘要：命令留头尾，其余字段走通用上限。"""
    compacted = {}
    for key, value in args.items():
        if tool == 'bash' and key == 'command' and isinstance(value, str):
            compacted[key] = _summarize_command(value)
        else:
            compacted[key] = _compact_value(value, MAX_DEPTH)
    return compacted


def _pairs(args: dict) -> str:
    return ', '.join(f'{key}={value}' for key, value in args.items()
                     if value not in (None, ''))


def detail(tool: str, args) -> str:
    """审批面上给人看的操作内容：命令独占整段，其余按 key=value 摘要。

    入参通常是 summarize 之后的存储值，但原始参数也能直接传（展示层不放大
    内容，只换写法）。
    """
    args = args if isinstance(args, dict) else {}
    command = args.get('command') if tool == 'bash' else None
    if not isinstance(command, str):
        return safe_text(_pairs(args))
    rest = _pairs({key: value for key, value in args.items() if key != 'command'})
    return safe_text(f'{command}\n{rest}' if rest else command)
