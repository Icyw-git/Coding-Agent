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
# 写入/编辑内容的上限：卡片要能看出「写了什么」，又要留在 30KB 卡片
# 上限内。600 字够看清一段代码或配置的开头与结尾。
WRITE_PREVIEW_CHARS = 600
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


def _summarize_content(text: str, limit: int = WRITE_PREVIEW_CHARS) -> str:
    """写入/编辑内容在登记时的摘要：留头尾，标明总长度。

    和 bash 的 command 同理：只留头部会把「写到文件末尾的东西」整段切掉，
    而审批人恰恰要靠它判断这次写入是不是想干的事。
    """
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - limit
    return (text[:head]
            + chr(10) + f'… （省略 {omitted} 字）…' + chr(10)
            + text[-tail:])


def summarize(tool: str, args: dict) -> dict:
    """登记用的参数摘要：命令与写入内容留头尾，其余字段走通用上限。"""
    compacted = {}
    for key, value in args.items():
        if tool == 'bash' and key == 'command' and isinstance(value, str):
            compacted[key] = _summarize_command(value)
        elif (tool in ('write', 'edit')
              and key in ('content', 'old_text', 'new_text')
              and isinstance(value, str) and len(value) > WRITE_PREVIEW_CHARS):
            compacted[key] = _summarize_content(value)
        else:
            compacted[key] = _compact_value(value, MAX_DEPTH)
    return compacted


# 工具的中文标签。审批卡片和 CLI 提示都显示「中文名 (原名)」：中文让人
# 一眼知道在干什么，原名留着方便对照日志/文档排查。未登记的工具只显示原名。
_TOOL_LABELS = {
    'bash': '执行命令',
    'read': '读文件',
    'write': '写文件',
    'edit': '改文件',
    'apply_patch': '改多个文件',
    'view_image': '看图',
    'read_document': '读文档',
    'render_document': '渲染文档页',
    'glob': '找文件',
    'grep': '搜内容',
    'ls': '列目录',
    'web_fetch': '访问网页',
    'screen_capture': '截屏',
    'screen_ocr': '识别截图文字',
    'clipboard_read': '读剪贴板',
    'clipboard_write': '写剪贴板',
    'desktop_snapshot': '看前台窗口',
    'desktop_windows': '列出窗口',
    'desktop_action': '操作键鼠',
    'delete_paths': '删除（进回收站）',
    'schedule_cron': '建定时任务',
    'cancel_cron': '删除定时任务',
    'list_crons': '看定时任务',
    'spawn_teammate': '派一个助手',
    'send_message': '给助手发消息',
    'check_inbox': '收助手消息',
    'request_shutdown': '让助手收工',
    'request_plan': '请助手出方案',
    'review_plan': '审助手的方案',
    'create_task': '建任务',
    'todo_write': '更新待办',
    'move_paths': '移动/重命名文件',
    'undo_moves': '撤销文件移动',
    'remind': '设置提醒',
    'remember': '记住一条',
    'forget': '删除一条记忆',
    'recall': '查记忆',
}


#: 决策码 → 给人看的原因。
#:
#: 审批卡片上的 `reason` 原本是 ``Decision.reason``——**写给模型看的英文**，
#: 例如 "moves or renames files (reversible via undo_moves, but still changes
#: the filesystem)"。而用户正是在这一屏决定要不要批准，给他英文技术描述
#: 等于没解释。
#:
#: 按 code 映射而不是按文本替换：文本会随实现改动，code 是稳定标识。
_REASON_ZH = {
    'destructive': '看起来是破坏性命令',
    'opaque_command': '命令里含有无法静态判断的部分（解释器、变量展开等）',
    'sandbox_read_only': '当前是只读档，写操作需要你确认',
    'outside_workspace_write': '要写到工作区外的路径',
    'network_egress': '要向网络发送请求',
    'external_tool': '第三方工具，副作用无法预先判断',
    'secret_egress': '命令同时提到了秘密文件和对外发送',
    'untrusted': '当前档位要求每条命令都确认',
    'screen_access': '要读取本机屏幕',
    'clipboard_access': '要读剪贴板（可能含密码或私信）',
    'clipboard_write': '要写剪贴板（会覆盖你刚复制的内容）',
    'desktop_control': '要操作本机键鼠',
    'delete_paths': '要删除文件（会进回收站，可恢复）',
    'move_paths': '要移动或重命名文件（可用 /undo-move 撤销）',
    'undo_moves': '要撤销上一批文件移动（不会覆盖已有文件）',
    'remember_memory': '要写入一条长期记忆（会影响之后每个会话）',
    'forget_memory': '要永久删除一条记忆（不可恢复）',
    'path_grant': '要长期放行一个目录的写权限',
    'domain_allowed': '这个域名已设为免审',
    'invalid_url': 'URL 不合法或指向非公网地址',
    'invalid_path': '路径不合法',
    'invalid_patch': '补丁格式无法解析',
    'deny_pattern': '命令命中黑名单',
    'deny_root_delete': '命令会删除文件系统根目录',
    'system_path': '要写系统目录',
    'secret_path': '要读写秘密文件',
    'domain_denied': '这个域名被设为拒绝',
    'rule_allowed': '用户规则放行',
}


def reason_zh(code: str, reason: str = '') -> str:
    """审批原因的中文解释；没有映射时退回原文（宁可显示英文也不丢信息）。"""
    text = _REASON_ZH.get(str(code or '').strip())
    if text:
        return text
    return ' '.join(str(reason or '').split())


def tool_name_zh(tool: str) -> str:
    """只要中文名，不带原名。

    进度行是**单行、会被反复覆盖**的短文案（``⏳ 2.3s · 正在执行 …``），
    塞进 ``看前台窗口 (desktop_snapshot)`` 会挤掉真正重要的"已用时"。审批面
    用 ``tool_label()``（带原名便于对照日志），这里用短名。
    """
    name = str(tool or '').strip()
    return _TOOL_LABELS.get(name) or name or '工具'


def tool_label(tool: str) -> str:
    """「中文名 (原名)」；没有中文名时只回原名。

    卡片上写 `desktop_action` 对非技术用户等于没写，但完全不显示原名又不好
    对照日志。两个都给。
    """
    name = str(tool or '').strip()
    label = _TOOL_LABELS.get(name)
    if label and label != name:
        return f'{label} ({name})'
    return name or '工具'


def _pairs(args: dict) -> str:
    return ', '.join(f'{key}={value}' for key, value in args.items()
                     if value not in (None, ''))


def _screen_detail(tool: str, args: dict) -> str:
    if tool == 'screen_capture':
        screen = args.get('screen') or 'primary'
        region = args.get('region') or 'full screen'
        delay = args.get('delay_ms')
        suffix = f' after {delay}ms' if delay else ''
        return f'capture the local {screen} screen ({region}){suffix}'
    path = args.get('path') or 'the latest screenshot'
    language = args.get('lang') or 'zh-Hans-CN'
    region = args.get('region')
    suffix = f', region {region}' if region else ''
    return f'run local OCR on {path} (lang={language}{suffix})'


def _clipboard_detail(tool: str, args: dict) -> str:
    if tool == 'clipboard_read':
        return 'read the current local clipboard text'
    text = args.get('text')
    chars = len(text) if isinstance(text, str) else 0
    preview = ' '.join(text[:80].split()) if isinstance(text, str) else ''
    suffix = f' — starts with: {preview}' if preview else ''
    return f'replace the local clipboard with {chars} chars{suffix}'


def _desktop_step_summary(step) -> str:
    if not isinstance(step, dict):
        return '?'
    action = step.get('action') or '?'
    if action == 'move':
        return f"move({step.get('x')},{step.get('y')})"
    if action == 'click':
        button = step.get('button') or 'left'
        count = step.get('count') or 1
        label = f'{button} ' if button != 'left' else ''
        label += 'double click' if count == 2 else 'click'
        return f"{label}({step.get('x')},{step.get('y')})"
    if action == 'type':
        text = step.get('text') if isinstance(step.get('text'), str) else ''
        preview = ' '.join(text[:40].split())
        return f'type len={len(text)}' + (f' "{preview}"' if preview else '')
    if action == 'key':
        return f"key={step.get('key', '')}"
    if action == 'scroll':
        return f"scroll={step.get('amount', '')}"
    if action == 'wait':
        return f"wait {step.get('ms', '')}ms"
    return str(action)


def _desktop_detail(tool: str, args: dict) -> str:
    if tool == 'desktop_snapshot':
        return 'inspect foreground window metadata and focused UI control'
    if tool == 'desktop_windows':
        return 'list visible top-level windows (read-only)'
    action = args.get('action') or 'unknown'
    fields = [f'action={action}']
    if args.get('expected_pid') is not None:
        fields.append(f"foreground pid={args['expected_pid']}")
    if args.get('expected_window_id'):
        fields.append(f"window id={args['expected_window_id']}")
    steps = args.get('steps')
    if action == 'sequence' and isinstance(steps, list) and steps:
        rendered = ' '.join(
            f'{index}.{_desktop_step_summary(step)}'
            for index, step in enumerate(steps, 1))
        fields.append(f'steps ({len(steps)})={rendered}')
    elif action in ('move', 'click'):
        fields.append(f"coordinates=({args.get('x')},{args.get('y')})")
        if action == 'click':
            fields.append(f"button={args.get('button') or 'left'}")
            if args.get('count') not in (None, 1):
                fields.append(f'count={args["count"]}')
    elif action == 'type':
        text = args.get('text') if isinstance(args.get('text'), str) else ''
        fields.append(f'text length={len(text)}')
        if text:
            fields.append('text=' + ' '.join(text[:80].split()))
    elif action == 'key':
        fields.append(f"key={args.get('key', '')}")
    elif action == 'scroll':
        fields.append(f"amount={args.get('amount', '')}")
    elif action == 'focus':
        fields.append('bring that window to the foreground')
    return 'desktop input: ' + ', '.join(fields)


def _delete_detail(args: dict) -> str:
    """删除审批要让人看到**每一条**目标：路径可能展开成多个文件。"""
    paths = args.get('paths')
    if isinstance(paths, str):
        paths = [paths]
    items = [str(item) for item in (paths or []) if str(item).strip()]
    if not items:
        return 'move nothing to the recycle bin (no paths)'
    head = (f'move {len(items)} item(s) to the recycle bin (restorable):'
            if len(items) > 1 else 'move to the recycle bin (restorable):')
    lines = [head] + [f'  {item}' for item in items[:20]]
    if len(items) > 20:
        lines.append(f'  … {len(items) - 20} more')
    return chr(10).join(lines)


def _content_preview(text, limit: int = 300) -> str:
    """写入内容的预览：保留头尾，中间省略。

    审批越界写时，人需要判断的是「这台机器要把什么东西写到哪里」。只给路径
    没法判断，给全文又会把卡片撑爆（飞书卡片有 30KB 上限，手机上也读不完）。
    所以留头尾、标出总长度。
    """
    text = text if isinstance(text, str) else str(text or '')
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - limit
    return (text[:head]
            + chr(10) + f'… （共 {len(text)} 字，省略 {omitted} 字）…' + chr(10)
            + text[-tail:])


def _write_detail(tool: str, args: dict) -> str:
    path = args.get('path') or '(no path)'
    content = args.get('content')
    if not isinstance(content, str):
        return f'{path}（无内容摘要）'
    verb = 'append to' if args.get('append') else 'write'
    preview = _content_preview(content)
    fence = chr(10) + '```' + chr(10)
    return (f'{verb} {path} ({len(content)} chars):'
            + fence + preview + chr(10) + '```')


def _edit_detail(args: dict) -> str:
    path = args.get('path') or '(no path)'
    old = args.get('old_text')
    new = args.get('new_text')
    if not isinstance(old, str) or not isinstance(new, str):
        return path
    replace_all = '（全部替换所有匹配）' if args.get('replace_all') else ''
    old_lines = old.splitlines()[:8] or ['(空)']
    new_lines = new.splitlines()[:8] or ['(空)']
    removed = chr(10).join(f'- {line}' for line in old_lines)
    added = chr(10).join(f'+ {line}' for line in new_lines)
    return (f'{path}{replace_all}' + chr(10)
            + f'--- 原内容 ({len(old)} chars)' + chr(10) + removed + chr(10)
            + f'+++ 新内容 ({len(new)} chars)' + chr(10) + added)


def _patch_summary(patch: str) -> tuple[list[str], str]:
    """从补丁文本里提取「改了什么文件」，返回 (摘要行, 其余内容)。

    审批补丁的人第一眼要看的是**影响面**：动了几处、哪些是新增哪些是删除。
    直接甩一份 20KB 的 diff 上去，既看不到影响面，也超出卡片长度。
    """
    actions = {
        '*** Add File:': 'add',
        '*** Update File:': 'update',
        '*** Delete File:': 'delete',
        '*** Move to:': 'move',
    }
    summary = []
    total_add = total_del = 0
    for line in str(patch or '').splitlines():
        for marker, verb in actions.items():
            if line.startswith(marker):
                path = line[len(marker):].strip()
                if verb == 'move':
                    summary.append(f'  → rename to {path}')
                else:
                    summary.append(f'  {verb} {path}')
                break
        else:
            # 先排除 diff 头：`+++`/`---` 在 startswith('+')/('-') 下会算成增删。
            if line.startswith(('+++', '---')):
                continue
            if line.startswith('+'):
                total_add += 1
            elif line.startswith('-'):
                total_del += 1
    stats = f'  (+{total_add}/-{total_del} lines)' if (total_add or total_del) else ''
    return summary, stats


def _apply_patch_detail(args: dict) -> str:
    patch = args.get('patch')
    if not isinstance(patch, str) or not patch.strip():
        return 'no patch content'
    summary, stats = _patch_summary(patch)
    if not summary:
        return _content_preview(patch, limit=400)
    head = f'patch touches {len(summary)} path(s):' + stats
    return head + chr(10) + chr(10).join(summary)


#: 记忆类型的含义。审批时看到 ``type=preference`` 不如看到中文。
_MEMORY_TYPE_LABELS = {
    'preference': '偏好',
    'correction': '纠正',
    'fact': '事实',
    'context': '背景',
}


def _move_detail(args) -> str:
    """移动审批的内容。

    绝不能把原始 args 打成 ``moves=[{'from': ...}]``——用户正是在这里决定
    "要不要让它动这些文件"，给他一个 Python dict 等于让他无法核对。
    """
    moves = args.get('moves')
    if isinstance(moves, dict):
        moves = [moves]
    if not isinstance(moves, list) or not moves:
        return '（没有可移动的条目）'
    lines = [f'移动 {len(moves)} 项：']
    for item in moves[:20]:
        if not isinstance(item, dict):
            lines.append(f'  {item}')
            continue
        lines.append(f"  {item.get('from') or '?'} -> {item.get('to') or '?'}")
    if len(moves) > 20:
        lines.append(f'  … 还有 {len(moves) - 20} 项')
    if args.get('dry_run') is False:
        lines.append('（会真的移动文件，可用 /undo-move 撤销）')
    return chr(10).join(lines)


def _memory_detail(tool: str, args) -> str:
    """记忆审批的内容：说清记什么、删什么，而不是 raw kwargs。"""
    if tool == 'forget':
        return f"删除记忆：{args.get('name') or '?'}"
    if tool == 'recall':
        query = str(args.get('query') or '').strip()
        return f'查询记忆：{query}' if query else '列出全部记忆'
    kind = str(args.get('type') or 'preference')
    label = _MEMORY_TYPE_LABELS.get(kind, kind)
    text = ' '.join(str(args.get('text') or '').split())
    name = str(args.get('name') or '').strip()
    head = f'记住（{label}）' + (f'：{name}' if name else '')
    return head + chr(10) + text[:300]


def detail(tool: str, args) -> str:
    """审批面上给人看的操作内容：命令独占整段，其余按 key=value 摘要。

    入参通常是 summarize 之后的存储值，但原始参数也能直接传（展示层不放大
    内容，只换写法）。
    """
    args = args if isinstance(args, dict) else {}
    if tool in ('screen_capture', 'screen_ocr'):
        return safe_text(_screen_detail(tool, args))
    if tool in ('clipboard_read', 'clipboard_write'):
        return safe_text(_clipboard_detail(tool, args))
    if tool in ('desktop_snapshot', 'desktop_windows', 'desktop_action'):
        return safe_text(_desktop_detail(tool, args))
    if tool == 'delete_paths':
        return safe_text(_delete_detail(args))
    if tool == 'move_paths':
        return safe_text(_move_detail(args))
    if tool == 'undo_moves':
        batch = str(args.get('batch_id') or '').strip()
        return safe_text(f'撤销批次：{batch}' if batch else '撤销最近一批移动')
    if tool == 'remind':
        when = args.get('when') or '?'
        body = args.get('text') or ''
        return safe_text(f'提醒时间：{when}' + chr(10) + f'内容：{body}')
    if tool in ('remember', 'forget', 'recall'):
        return safe_text(_memory_detail(tool, args))
    if tool == 'write':
        return safe_text(_write_detail(tool, args))
    if tool == 'edit':
        return safe_text(_edit_detail(args))
    if tool == 'apply_patch':
        return safe_text(_apply_patch_detail(args))
    command = args.get('command') if tool == 'bash' else None
    if not isinstance(command, str):
        return safe_text(_pairs(args))
    rest = _pairs({key: value for key, value in args.items() if key != 'command'})
    return safe_text(f'{command}\n{rest}' if rest else command)
