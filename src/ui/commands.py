"""TUI 命令注册表：元数据、别名、模糊建议与帮助（参考 Pi slash-commands）。

命令实现仍由 App 分发；这里只提供统一的 name/description/argument hint，
让补全、/help 与「你是不是想输入」共用一份真值。
"""
from __future__ import annotations

from dataclasses import dataclass
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
        names = set(harness.MODEL_WINDOWS)
        current = harness.os.getenv('LLM_MODEL_ID', '')
        if current:
            names.add(current)
    except Exception:
        return []
    items = [(name, name) for name in sorted(names)]
    if prefix:
        needle = prefix.casefold()
        items = [item for item in items if needle in item[0].casefold()]
    return items


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
    values = ('gc', 'gc 7', 'gc 30', 'gc 7 --apply')
    return [(value, value) for value in values
            if not prefix or value.startswith(prefix.casefold())]


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec('help', '显示帮助', '[命令]', ('?',),
                argument_completions=_help_arguments),
    CommandSpec('status', '查看当前会话与运行状态', aliases=('session',)),
    CommandSpec('model', '选择/查看模型',
                argument_completions=_model_arguments),
    CommandSpec('pwd', '查看当前工作区'),
    CommandSpec('sessions', '打开会话选择器', aliases=('transcripts',)),
    CommandSpec('resume', '恢复会话', '<序号|ID前缀|标题>',
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
    CommandSpec('cache', '工具缓存统计/回收', '[gc [天数] [--apply]]',
                argument_completions=_cache_arguments),
    CommandSpec('sandbox', '查看/切换执行档位', '[档位|approvals <档位>]',
                argument_completions=_sandbox_arguments),
    CommandSpec('checkpoint', '创建快照', '[标签]', ported=False),
    CommandSpec('checkpoints', '列出当前会话快照'),
    CommandSpec('rewind', '回溯快照', ported=False),
    CommandSpec('undo', '撤销最近一回合', ported=False),
    CommandSpec('fork', '从用户消息分叉', '[事件序号]'),
    CommandSpec('clone', '从当前叶复制会话（分叉）'),
    CommandSpec('archive', '归档旧会话', ported=False),
    CommandSpec('delete', '删除已归档会话', ported=False),
    CommandSpec('watch', '跟踪后台任务', ported=False),
    CommandSpec('copy', '复制最近一次工具输出'),
    CommandSpec('expand', '展开最近一个工具块'),
    CommandSpec('approvals', '查看待审批操作'),
    CommandSpec('revoke', '撤销同类预授权', ported=False),
    CommandSpec('workspaces', '列出工作区', ported=False),
    CommandSpec('workspace', '维护工作区', ported=False),
    CommandSpec('cd', '切换工作区', ported=False),
    CommandSpec('new', '新建会话（当前工作区）', aliases=('n',)),
    CommandSpec('compact', '压缩当前上下文'),
    CommandSpec('find', '跨会话搜索', ported=False),
    CommandSpec('rename', '重命名会话', '<标题>', aliases=('name',)),
    CommandSpec('more', '加载更早的历史', ported=False),
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
    ported = [spec for spec in COMMANDS if spec.ported]
    unported = [spec for spec in COMMANDS if not spec.ported]
    lines = ['TUI commands:']
    for spec in ported:
        lines.append(f'  {spec.usage:<28}{spec.description}')
    if unported:
        lines.append('Only in line REPL (--ui ansi):')
        lines.append('  ' + '  '.join(f'/{spec.name}' for spec in unported))
    skills = skill_commands()
    if skills:
        lines.append(f'Skills ({len(skills)}): ' + '  '.join(
            f'/{spec.name}' for spec in skills))
    return chr(10).join(lines)


HOTKEYS = (
    ('Enter', '发送消息；有候选时先采用（命令直接执行）'),
    ('Ctrl+J', '换行'),
    ('Tab', '采用补全'),
    ('↑ / ↓', '有候选时切换；否则移动光标'),
    ('Alt+↑ / Alt+↓', '切换补全候选 / 无候选时 Alt+↑ 取回排队消息'),
    ('Esc', '关闭补全候选；回合中取消'),
    ('Ctrl+O', '展开最近工具块'),
    ('Ctrl+C', '/quit', '有选中文本时复制，否则退出'),
    ('Ctrl+Shift+C', '/copy', '复制选中文本 / 最近工具输出'),
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
