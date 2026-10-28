"""TUI 命令注册表：元数据、别名、模糊建议与帮助（参考 Pi slash-commands）。

命令实现仍由 App 分发；这里只提供统一的 name/description/argument hint，
让补全、/help 与「你是不是想输入」共用一份真值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ui.fuzzy import fuzzy_filter

ArgumentCompleter = Callable[[str], list[tuple[str, str]]]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    argument_hint: str = ''
    aliases: tuple[str, ...] = ()
    ported: bool = True          # False = 仅行式 REPL 提供
    argument_completions: ArgumentCompleter | None = None

    @property
    def usage(self) -> str:
        return f'/{self.name}' + (f' {self.argument_hint}' if self.argument_hint else '')


def _help_arguments(prefix: str) -> list[tuple[str, str]]:
    specs = fuzzy_filter([spec for spec in COMMANDS if spec.ported],
                         prefix, lambda spec: spec.name)
    return [(spec.name, spec.usage) for spec in specs]


def _resume_arguments(prefix: str) -> list[tuple[str, str]]:
    try:
        import harness
        rows = harness.session_rows(limit=None, include_empty=False)
    except Exception:
        return []
    items = [(row['id'], f"{row['title']} · {row['id'][:8]}")
             for row in rows]
    if prefix:
        needle = prefix.casefold()
        items = [item for item in items
                 if needle in item[0].casefold() or needle in item[1].casefold()]
    return items


def _model_arguments(prefix: str) -> list[tuple[str, str]]:
    try:
        import harness
        settings = harness.llm_config.get_settings()
        names = set(harness.MODEL_WINDOWS) | set(settings.models)
        current = (harness.os.getenv('HARNESS_MODEL_MAIN')
                   or harness.model_for('main'))
        if current:
            names.add(current)
    except Exception:
        return []
    items = [(name, name) for name in sorted(names)]
    if prefix:
        needle = prefix.casefold()
        items = [item for item in items if needle in item[0].casefold()]
    return items


def _checkpoint_gc_arguments(prefix: str) -> list[tuple[str, str]]:
    """复合参数补全：保留已输入的前缀，再补下一个参数。

    apply_suggestion 会替换整个参数，因此候选必须带上前面的 `--all`/`--keep`。
    """
    text = prefix or ''
    tokens = text.split()
    ends_space = bool(text) and text[-1].isspace()
    if ends_space:
        base, current = tokens, ''
    elif tokens:
        base, current = tokens[:-1], tokens[-1]
    else:
        base, current = [], ''

    has_all = '--all' in base
    keep_at = base.index('--keep') if '--keep' in base else -1
    if keep_at >= 0 and keep_at + 1 == len(base):
        # `--keep` 已经输入，只等数字；给出常用值，并保留当前前缀。
        values = (10, 20, 50, 100)
        result = []
        for number in values:
            if current and not str(number).startswith(current):
                continue
            result.append((f"{' '.join(base)} {number}",
                           f'--keep {number}  每个会话保留 {number} 个'))
        if current.isdigit() and 1 <= int(current) <= 10_000:
            value = ' '.join(base) + ' ' + current
            result.insert(0, (value, f'--keep {current}  每个会话保留 {current} 个'))
        return result

    candidates = []
    if not has_all:
        candidates.append((['--all'], '--all  清理所有会话的旧快照'))
    if keep_at < 0:
        for number in (10, 20, 50, 100):
            candidates.append((['--keep', str(number)],
                               f'--keep {number}  每个会话保留 {number} 个'))
    result = []
    for added, label in candidates:
        if current and not added[0].startswith(current):
            continue
        result.append((' '.join([*base, *added]), label))
    return result


def _tool_arguments(prefix: str) -> list[tuple[str, str]]:
    try:
        import harness
        names = sorted(harness.tool_registry)
    except Exception:
        return []
    if prefix:
        needle = prefix.casefold()
        names = [name for name in names if needle in name.casefold()]
    return [(name, name) for name in names]


def _sandbox_arguments(prefix: str) -> list[tuple[str, str]]:
    values = ('read-only', 'workspace-write', 'danger-full-access',
              'approvals untrusted', 'approvals on-request',
              'approvals never')
    return [(value, value) for value in values
            if not prefix or value.startswith(prefix.casefold())]


def _cache_arguments(prefix: str) -> list[tuple[str, str]]:
    text = prefix or ''
    values = ('profile', 'gc', 'gc 7', 'gc 30', 'gc 7 --apply')
    items = [(value, value) for value in values
             if not text or value.startswith(text.casefold())]
    # `gc 30 ` 之后继续补 --apply；候选保留前面的天数。
    parts = text.split()
    if (len(parts) >= 2 and parts[0] == 'gc' and parts[1].isdigit()
            and text.endswith(' ') and '--apply' not in parts
            and len(parts) == 2):
        items.append((f'{text.rstrip()} --apply',
                      f'{text.rstrip()} --apply  执行回收'))
    return items


def _directory_arguments(prefix: str) -> list[tuple[str, str]]:
    """目录补全：`/cd` 与 `/workspace add` 共用，值带尾部分隔符。"""
    try:
        import harness
        workspace = harness.get_workdir()
    except Exception:
        return []
    argument = prefix or ''
    quote = argument[:1] if argument.startswith(('"', "'")) else ''
    raw = argument[1:] if quote else argument
    if quote and raw.endswith(quote):
        raw = raw[:-1]
    expanded = Path(raw).expanduser()
    ends_with_separator = raw.endswith(('/', '\\'))
    parent = expanded if ends_with_separator or not raw else expanded.parent
    leaf = '' if ends_with_separator or not raw else expanded.name
    directory = parent if parent.is_absolute() else Path(workspace) / parent
    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return []
    items = []
    for child in children:
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if not is_dir or not child.name.casefold().startswith(leaf.casefold()):
            continue
        value = str(parent / child.name) + os.sep
        if quote or any(char.isspace() for char in value):
            marker = quote or '"'
            value = marker + value + marker
        items.append((value, f'{child.name}{os.sep}  [目录]'))
    return items


def _archive_arguments(prefix: str) -> list[tuple[str, str]]:
    values = (
        ('30', '归档 30 天未活跃的会话'),
        ('90', '归档 90 天未活跃的会话'),
        ('all', '归档全部非当前会话'),
        ('empty', '只归档空会话'),
        ('restore', '恢复已归档会话'),
    )
    return [(value, f'{value}  {description}') for value, description in values
            if not prefix or value.startswith(prefix.casefold())]


def _workspace_entries() -> list[dict]:
    try:
        import harness
        return harness.registered_workspaces()
    except Exception:
        return []


def _workspace_selector_items(entries: list[dict],
                              prefix: str = '') -> list[tuple[str, str]]:
    needle = (prefix or '').casefold()
    items = []
    for index, entry in enumerate(entries, 1):
        haystack = f"{entry['name']} {entry['path']} {entry['id']}".casefold()
        if needle and needle not in haystack:
            continue
        items.append((str(index), f"{entry['name']}  {entry['path']}"))
    return items


def _workspace_arguments(prefix: str) -> list[tuple[str, str]]:
    text = prefix or ''
    parts = text.split(maxsplit=1)
    action = parts[0].casefold() if parts else ''
    if len(parts) == 1 and not text.endswith(' '):
        values = (('add', '登记目录'), ('remove', '移除登记'),
                  ('list', '查看已登记工作区'))
        return [(value, f'{value}  {description}')
                for value, description in values if value.startswith(action)]
    # 补全值要覆盖整个参数（apply_suggestion 只替换命令名之后的文本），
    # 因此把子命令一起带回，避免选中目录后把 `add`/`remove` 冲掉。
    if action == 'add':
        return [(f'add {value}', label) for value, label in
                _directory_arguments(parts[1] if len(parts) > 1 else '')]
    if action == 'remove':
        return [(f'remove {value}', label) for value, label in
                _workspace_selector_items(
                    _workspace_entries(), parts[1] if len(parts) > 1 else '')]
    return []


def _cd_arguments(prefix: str) -> list[tuple[str, str]]:
    return _directory_arguments(prefix)


def _new_arguments(prefix: str) -> list[tuple[str, str]]:
    text = prefix or ''
    if text.startswith('--ws '):
        return [(f'--ws {value}', label) for value, label in
                _workspace_selector_items(_workspace_entries(), text[5:])]
    if text.startswith('-') and '--ws'.startswith(text.strip()):
        return [('--ws ', '--ws <序号|名称|路径>  指定工作区')]
    return []


def _watch_arguments(prefix: str) -> list[tuple[str, str]]:
    text = prefix or ''
    parts = text.split()
    if parts and parts[0] in ('--list', '-l'):
        return []
    if parts and not parts[0].startswith('-'):
        task = parts[0]
        if len(parts) == 1 and not text.endswith(' '):
            items = _watch_targets()
            needle = text.casefold()
            return [item for item in items
                    if needle in item[0].casefold()
                    or needle in item[1].casefold()]
        if len(parts) == 1 or (len(parts) == 2 and not text.endswith(' ')
                               and parts[1].startswith('-')
                               and '--timeout'.startswith(parts[1])):
            return [(f'{task} --timeout 60',
                     '--timeout 60  最多等待 60 秒')]
        if parts[1] == '--timeout' or '--timeout'.startswith(parts[1]):
            current = parts[2] if len(parts) > 2 else ''
            values = (30, 60, 300)
            return [(f'{task} --timeout {value}',
                     f'--timeout {value}  最多等待 {value} 秒')
                    for value in values
                    if not current or str(value).startswith(current)]
        return []
    items = _watch_targets()
    needle = text.casefold()
    if needle:
        items = [item for item in items
                 if needle in item[0].casefold() or needle in item[1].casefold()]
    return items


def _watch_targets() -> list[tuple[str, str]]:
    items = [('--list', '--list  查看全部后台任务')]
    try:
        import background_task
        for task in background_task.list_background_tasks()[:8]:
            command = str(task.get('command') or '').replace('\n', ' ')[:40]
            items.append((str(task['id']),
                          f"{task['id']}  {task.get('status', '?')}  {command}"))
    except Exception:
        pass
    return items


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec('help', '显示帮助', '[命令]', ('?',),
                argument_completions=_help_arguments),
    CommandSpec('status', '查看当前会话与运行状态', aliases=('session',)),
    CommandSpec('model', '选择/查看模型',
                argument_completions=_model_arguments),
    CommandSpec('pwd', '查看当前工作区', aliases=('p', 'w')),
    CommandSpec('sessions', '列出/选择会话（与 /resume 同界面）',
                aliases=('transcripts', 's')),
    CommandSpec('resume', '恢复会话', '<last|序号|ID前缀|标题>', ('r',),
                argument_completions=_resume_arguments),
    CommandSpec('export', '导出 transcript'),
    CommandSpec('clear', '清空当前对话视图'),
    CommandSpec('allow', '本会话允许某个工具', '<工具>',
                argument_completions=_tool_arguments),
    CommandSpec('deny', '本会话拒绝某个工具', '<工具>',
                argument_completions=_tool_arguments),
    CommandSpec('ask', '恢复某个工具的逐次审批', '<工具>',
                argument_completions=_tool_arguments),
    CommandSpec('hotkeys', '显示快捷键'),
    CommandSpec('quit', '退出', aliases=('exit', 'q')),
    CommandSpec('diff', '查看未提交改动'),
    CommandSpec('review', '只读审阅未提交改动'),
    CommandSpec('cache', '存储 profile / 工具缓存回收', '[profile|gc [天数] [--apply]]',
                argument_completions=_cache_arguments),
    CommandSpec('sandbox', '查看/切换执行档位', '[档位|approvals <档位>]',
                argument_completions=_sandbox_arguments),
    CommandSpec('checkpoint', '创建快照', '[标签]', ('cp',)),
    CommandSpec('checkpoints', '列出当前会话快照', aliases=('cps',)),
    CommandSpec('checkpoint-gc', '手动清理旧快照', '[--all] [--keep N]',
                argument_completions=_checkpoint_gc_arguments),
    CommandSpec('rewind', '回溯快照', '[ID|turn|last|head] [--force] [--session] [--yes]'),
    CommandSpec('undo', '快速回滚最近一回合（= /rewind turn --yes）'),
    CommandSpec('fork', '从用户消息分叉', '[事件序号]'),
    CommandSpec('clone', '同 /fork：从当前叶复制出新会话'),
    CommandSpec('archive', '归档旧会话', '[天数|all|empty|restore]',
                argument_completions=_archive_arguments),
    CommandSpec('delete', '删除已归档会话（需输入“删除”确认）'),
    CommandSpec('watch', '跟踪后台任务', '[bg_id|--list] [--timeout 秒]',
                argument_completions=_watch_arguments),
    CommandSpec('copy', '复制最近一次工具输出'),
    CommandSpec('dequeue', '管理排队消息：取回单条/全部或删除', aliases=()),
    CommandSpec('expand', '展开最近一个工具块'),
    CommandSpec('approvals', '查看待审批操作', '[序号 y|n]', ('perms',)),
    CommandSpec('revoke', '撤销同类预授权与目录写授权'),
    CommandSpec('domains', '域名策略：允许（免审）或拒绝（硬拒）',
                '[allow|deny|remove <主机>] [until 日期] [note 备注]',
                ('domain',)),
    CommandSpec('audit', '查看最近的安全相关事件', '[类型|all]'),
    CommandSpec('usage', '使用统计：token/耗时/工具/审批/记忆',
                '[天数]', ()),
    CommandSpec('undo-move', '撤销上一批文件移动',
                '[batch_id]', ()),
    CommandSpec('improvements', '自我改进：会话/记录/回滚',
                '[begin <目标> | check <机制> | status | end | rollback <id>]',
                ()),
    CommandSpec('rules', '查看用户规则文件（allow/ask/deny）'),
    CommandSpec('memories',
                '记忆：开关 / 来源 / 质量评测',
                '[on|off|readonly|writeonly | why <名字> | quality]',
                ('memory-mode',)),
    CommandSpec('grants', '查看/维护目录写授权',
                '[add "<目录>" [until 日期] [note 备注]|remove "<目录>"]',
                ('grant',)),
    CommandSpec('workspaces', '列出工作区'),
    CommandSpec('workspace', '维护工作区',
                '[list|add <路径> [--name 名称]|remove [选择器]]',
                argument_completions=_workspace_arguments),
    CommandSpec('cd', '切换工作区', '[路径]',
                argument_completions=_cd_arguments),
    CommandSpec('new', '新建会话（默认当前工作区）',
                '[--ws <序号|名称|路径>|会话ID]', ('n',),
                argument_completions=_new_arguments),
    CommandSpec('compact', '压缩当前上下文'),
    CommandSpec('find', '跨会话搜索', '<关键词>', ('search',)),
    CommandSpec('rename', '重命名会话', '<标题>', aliases=('name',)),
    CommandSpec('more', '加载更早的历史'),
)

_BY_NAME = {name.lower(): spec for spec in COMMANDS
            for name in (spec.name, *spec.aliases)}


def skill_commands() -> list[CommandSpec]:
    """动态技能命令（Pi 的 `skill:name`）：工作区切换/技能编辑后自动跟随。"""
    try:
        import skill_system
        skill_system.scan_skills()
        return [CommandSpec(f'skill:{skill["name"]}',
                            f'[skill] {skill["description"]}')
                for skill in skill_system.SKILL_REGISTRY.values()]
    except Exception:
        return []


def all_commands() -> list[CommandSpec]:
    return [*COMMANDS, *skill_commands()]


def find(name: str) -> CommandSpec | None:
    key = (name or '').lstrip('/').lower()
    spec = _BY_NAME.get(key)
    if spec is not None:
        return spec
    if key.startswith('skill:'):
        return next((item for item in skill_commands() if item.name == key), None)
    return None


def matches(query: str) -> list[CommandSpec]:
    """按模糊分数排序；查询为空时返回全部（限 8 条）。"""
    query = (query or '').strip()
    if not query:
        return [spec for spec in COMMANDS if spec.ported][:8]
    return fuzzy_filter(all_commands(), query,
                        lambda spec: f'{spec.name} {spec.description}')[:8]


def argument_suggestions(spec: CommandSpec,
                         prefix: str) -> list[tuple[str, str]]:
    """命令参数补全（对应 Pi 的 `getArgumentCompletions`）；失败时返回空。"""
    if spec.argument_completions is None:
        return []
    try:
        return spec.argument_completions(prefix or '')
    except Exception:
        return []


def did_you_mean(name: str) -> str | None:
    candidates = fuzzy_filter(all_commands(), name, lambda spec: spec.name)
    return f'/{candidates[0].name}' if candidates else None


def help_text() -> str:
    """`/help` 的纯文本（终端与飞书共用）。

    不用定宽对齐：``/improvements`` 的用法串有 70 字，而飞书是比例字体，
    列对齐在那里毫无意义——超长时直接把描述换到下一行，两边都能读。
    """
    ported = [spec for spec in COMMANDS if spec.ported]
    unported = [spec for spec in COMMANDS if not spec.ported]
    column = 24
    lines = [f'命令（{len(ported)} 个）：']
    for spec in ported:
        usage = spec.usage
        if len(usage) <= column:
            lines.append(f'  {usage:<{column}}  {spec.description}')
        else:
            lines.append(f'  {usage}')
            lines.append(f'    {spec.description}')
    if unported:
        lines.append('只能在文字模式（--ui ansi）用：')
        lines.append('  ' + '  '.join(f'/{spec.name}' for spec in unported))
    skills = skill_commands()
    if skills:
        lines.append(f'技能（{len(skills)}）：' + '  '.join(
            f'/{spec.name}' for spec in skills))
    return chr(10).join(lines)


HOTKEYS = (
    ('Enter', '发送消息；忙时进入下回合队列（有候选时先采用）'),
    ('Ctrl+J', '换行'),
    ('Tab', '采用补全'),
    ('↑ / ↓', '有候选时切换；否则移动光标'),
    ('Alt+↑ / Alt+↓', '切换补全候选 / 无候选时 Alt+↑ 打开队列选择器'),
    ('Alt+Enter', '忙时立即进入当前回合队列（steer），空闲时直接发送'),
    ('Esc', '关闭补全候选；回合中取消并取回全部排队消息'),
    ('Ctrl+C', '/quit', '有选中文本时复制，否则退出'),
    ('Ctrl+Shift+C', '/copy', '复制选中文本（/copy 复制最近工具输出）'),
    ('Alt+↑ / Ctrl+G', '/dequeue', '打开队列选择器：↑↓ 选择、Enter 取回、Delete 删除、Ctrl+R 取回全部'),
    ('Ctrl+O', '展开最近工具块'),
    ('Ctrl+F', '搜索当前对话；Enter / Shift+Enter 定位下一条 / 上一条命中'),
    ('Ctrl+Shift+P', '打开命令面板；选择命令运行或回填参数'),
    ('Ctrl+Q', '/quit', '退出'),
)


def hotkeys_text() -> str:
    lines = ['Keybindings:']
    for entry in HOTKEYS:
        if len(entry) == 2:
            key, desc = entry
            lines.append(f'  {key:<18}{desc}')
        else:
            key, alias, desc = entry
            lines.append(f'  {key:<10}/{alias:<8}{desc}')
    return chr(10).join(lines)
