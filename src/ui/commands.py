"""TUI 命令注册表：元数据、别名、模糊建议与帮助（参考 Pi slash-commands）。

命令实现仍由 App 分发；这里只提供统一的 name/description/argument hint，
让补全、/help 与「你是不是想输入」共用一份真值。
"""
from __future__ import annotations

from dataclasses import dataclass

from ui.fuzzy import fuzzy_filter


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    argument_hint: str = ''
    aliases: tuple[str, ...] = ()
    ported: bool = True          # False = 仅行式 REPL 提供

    @property
    def usage(self) -> str:
        return f'/{self.name}' + (f' {self.argument_hint}' if self.argument_hint else '')


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec('help', '显示帮助', '[命令]', ('?',)),
    CommandSpec('status', '查看当前会话与运行状态'),
    CommandSpec('model', '查看当前模型'),
    CommandSpec('pwd', '查看当前工作区'),
    CommandSpec('sessions', '打开会话选择器', aliases=('transcripts',)),
    CommandSpec('resume', '恢复会话', '<序号|ID前缀|标题>'),
    CommandSpec('export', '导出 transcript'),
    CommandSpec('clear', '清空当前对话视图'),
    CommandSpec('allow', '本会话允许某个工具', '<工具>'),
    CommandSpec('deny', '本会话拒绝某个工具', '<工具>'),
    CommandSpec('ask', '恢复某个工具的逐次审批', '<工具>'),
    CommandSpec('hotkeys', '显示快捷键'),
    CommandSpec('quit', '退出', aliases=('exit', 'q')),
    CommandSpec('diff', '查看未提交改动', ported=False),
    CommandSpec('review', '只读审阅未提交改动', ported=False),
    CommandSpec('cache', '工具缓存统计/回收', ported=False),
    CommandSpec('sandbox', '查看/切换执行档位', ported=False),
    CommandSpec('checkpoint', '创建快照', '[标签]', ported=False),
    CommandSpec('checkpoints', '列出快照', ported=False),
    CommandSpec('rewind', '回溯快照', ported=False),
    CommandSpec('undo', '撤销最近一回合', ported=False),
    CommandSpec('fork', '分叉会话', ported=False),
    CommandSpec('archive', '归档旧会话', ported=False),
    CommandSpec('delete', '删除已归档会话', ported=False),
    CommandSpec('watch', '跟踪后台任务', ported=False),
    CommandSpec('expand', '展开被折叠的工具输出', ported=False),
    CommandSpec('approvals', '查看待审批操作', ported=False),
    CommandSpec('revoke', '撤销同类预授权', ported=False),
    CommandSpec('workspaces', '列出工作区', ported=False),
    CommandSpec('workspace', '维护工作区', ported=False),
    CommandSpec('cd', '切换工作区', ported=False),
    CommandSpec('new', '新建会话', ported=False),
    CommandSpec('compact', '压缩上下文', ported=False),
    CommandSpec('find', '跨会话搜索', ported=False),
    CommandSpec('rename', '重命名会话', ported=False),
    CommandSpec('more', '加载更早的历史', ported=False),
)

_BY_NAME = {name.lower(): spec for spec in COMMANDS
            for name in (spec.name, *spec.aliases)}


def find(name: str) -> CommandSpec | None:
    return _BY_NAME.get((name or '').lstrip('/').lower())


def matches(query: str) -> list[CommandSpec]:
    """按模糊分数排序；查询为空时返回全部（限 8 条）。"""
    query = (query or '').strip()
    if not query:
        return [spec for spec in COMMANDS if spec.ported][:8]
    return fuzzy_filter(COMMANDS, query,
                        lambda spec: f'{spec.name} {spec.description}')[:8]


def did_you_mean(name: str) -> str | None:
    candidates = fuzzy_filter(COMMANDS, name, lambda spec: spec.name)
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
    return chr(10).join(lines)


HOTKEYS = (
    ('Enter', '发送消息'),
    ('Ctrl+J', '换行'),
    ('Tab', '采用补全'),
    ('Alt+↑ / Alt+↓', '切换补全候选 / 无候选时 Alt+↑ 取回排队消息'),
    ('Esc', '关闭补全候选；回合中取消'),
    ('Ctrl+O', '展开最近工具块'),
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
