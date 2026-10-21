"""Slash-command metadata shared by REPL dispatch and help output."""

from dataclasses import dataclass
from difflib import get_close_matches

from exec_policy import APPROVAL_POLICIES, SANDBOX_MODES


@dataclass(frozen=True)
class Command:
    name: str
    aliases: tuple[str, ...]
    arguments: str
    description: str
    group: str

    @property
    def usage(self) -> str:
        return f'{self.name} {self.arguments}'.rstrip()


COMMANDS = (
    Command('/help', ('/?',), '[命令]', '显示帮助；如 /help rewind', '常用'),
    Command('/new', ('/n',), '[--ws <序号|名称|路径>|会话ID]',
            '选择工作区开始新会话；--ws 直接指定，Enter 用当前目录', '会话'),
    Command('/resume', ('/r',), '[last|会话ID或前缀]', '选择并恢复会话及其工作目录；last 直达最近会话', '会话'),
    Command('/sessions', ('/s',), '', '打开会话管理；按工作区分组、预览和切换', '会话'),
    Command('/archive', (), '[天数|all|empty|restore]', '归档旧会话；默认 30 天，可查看全部或恢复归档', '会话'),
    Command('/delete', (), '', '永久删除已归档会话及关联快照（不可恢复）', '会话'),
    Command('/status', (), '', '查看当前会话与运行状态', '常用'),
    Command('/watch', (), '[bg_id|--list] [--timeout 秒]',
            '跟踪最新的后台任务；--list 查看全部，无需记住编号', '常用'),
    Command('/diff', (), '', '查看当前工作区未提交改动的逐行 diff（只读）', '常用'),
    Command('/cache', (), '[gc [天数] [--apply]]',
            '查看工具缓存占用；gc 预览或清理过期且未被会话引用的文件', '常用'),
    Command('/review', (), '', '用只读子代理审阅未提交改动并给出分级问题', '常用'),
    Command('/compact', (), '', '压缩当前会话上下文并保留完整记录', '常用'),
    Command('/undo', (), '', '撤销最近一次已完成回合的文件和会话改动', '常用'),
    Command('/model', (), '', '查看当前模型配置', '常用'),
    Command('/approvals', ('/perms',), '[序号] y|n', '查看待审批操作；带序号时按序号决议', '常用'),
    Command('/revoke', (), '', '撤销当前会话已允许的同类操作', '常用'),
    Command('/sandbox', (), '[档位|approvals <档位>]',
            '查看或切换执行档位（read-only|workspace-write|danger-full-access）与审批档位',
            '常用'),
    Command('/fork', (), '[事件序号]', '从当前会话分叉出新会话（原会话保留）', '会话'),
    Command('/find', ('/search',), '<关键词>', '跨会话搜索历史内容', '会话'),
    Command('/rename', (), '<新主题>', '设置当前会话主题', '会话'),
    Command('/pwd', ('/p', '/w'), '', '查看当前工作区', '工作区'),
    Command('/cd', (), '[路径]', '切换当前会话工作区；换项目建议 /new', '工作区'),
    Command('/workspaces', (), '', '列出已登记工作区', '工作区'),
    Command('/workspace', (), '[add <路径> [--name 名称]|remove <序号|名称|路径>]',
            '登记、命名或移除工作区', '工作区'),
    Command('/checkpoints', ('/cps',), '', '列出当前会话的快照', '快照'),
    Command('/checkpoint', ('/cp',), '[标签]', '创建手动快照', '快照'),
    Command('/rewind', (), '[ID|last|head] [--force] [--session] [--yes]',
            '预览并确认回溯；--session 仅恢复会话，--force 允许覆盖冲突', '快照'),
    Command('/checkpoint-gc', (), '', '清理旧快照', '快照'),
    Command('/quit', ('/exit',), '', '退出；会话自动保存', '常用'),
)

_BY_NAME = {name: command for command in COMMANDS
            for name in (command.name, *command.aliases)}


def parse_command(line: str) -> tuple[str, str]:
    """Preserve argument text (notably Windows paths and checkpoint labels)."""
    parts = line.strip().split(maxsplit=1)
    name = parts[0].lower() if parts else '/help'
    if name == '/':
        name = '/help'
    command = _BY_NAME.get(name)
    return (command.name if command else name, parts[1] if len(parts) > 1 else '')


def command_error(name: str, argument: str) -> str | None:
    command = _BY_NAME.get(name)
    if command is None:
        matches = get_close_matches(name, _BY_NAME, n=1, cutoff=0.6)
        hint = f' 你是否想输入 {matches[0]}？' if matches else ''
        return f'未知命令：{name}。{hint} 输入 / 查看可用命令。'
    if not command.arguments and argument:
        return f'{name} 不接受参数。用法：{command.usage}'
    if name == '/rewind':
        parts = argument.split()
        unknown = [p for p in parts if p.startswith('--')
                   and p not in ('--force', '--session', '--yes')]
        if unknown or len([p for p in parts if not p.startswith('--')]) > 1:
            return f'参数无效。用法：{command.usage}'
    if name == '/archive' and argument:
        if (argument not in ('all', 'empty', 'restore')
                and not (argument.isascii() and argument.isdigit()
                         and 1 <= int(argument) <= 36500)):
            return '用法：/archive [天数|all|empty|restore]，天数需为 1～36500。'
    if name == '/approvals' and argument:
        tokens = argument.split()
        if (len(tokens) != 2 or not tokens[0].isdigit()
                or tokens[1] not in ('y', 'n')):
            return '用法：/approvals [序号] y|n，序号见 /approvals 列表。'
    if name == '/sandbox' and argument:
        # 与 harness.apply_sandbox_tier 接受的形状一致：[档位] 或 approvals [档位]
        parts = [part.casefold() for part in argument.split()]
        if parts[0] == 'approvals':
            if len(parts) != 2 or parts[1] not in APPROVAL_POLICIES:
                return f'用法：/sandbox approvals [{"|".join(APPROVAL_POLICIES)}]'
        elif len(parts) != 1 or parts[0] not in SANDBOX_MODES:
            return f'用法：/sandbox [{"|".join(SANDBOX_MODES)}]'
    if name == '/fork' and argument and not argument.strip().isdigit():
        return '用法：/fork [事件序号]，缺省复制全部历史。'
    if name == '/find' and not argument.strip():
        return '用法：/find <关键词>。'
    if name == '/rename' and not argument.strip():
        return '用法：/rename <新主题>。'
    if name == '/workspaces' and argument.strip():
        return '用法：/workspaces（不带参数）。'
    if name == '/workspace':
        tokens = argument.split(maxsplit=1)
        action = tokens[0].lower() if tokens else 'list'
        if action not in ('list', 'add', 'remove'):
            return ('用法：/workspaces；/workspace add <路径> [--name 名称]；'
                    '/workspace remove <序号|名称|路径>')
        if action in ('add', 'remove') and (
                len(tokens) < 2 or not tokens[1].strip()):
            if action == 'add':
                return '用法：/workspace add <路径> [--name 名称]'
            return '用法：/workspace remove <序号|名称|路径>'
    return None


def format_command_help(argument: str = '') -> str:
    if argument:
        name, _ = parse_command('/' + argument.lstrip('/'))
        command = _BY_NAME.get(name)
        if command is None:
            return command_error(name, '')
        aliases = ', '.join(command.aliases) or '无'
        return f'  {command.usage}\n  {command.description}\n  别名：{aliases}'
    lines = ['  直接输入消息开始对话；/help <命令> 查看用法。']
    for group in ('常用', '会话', '工作区', '快照'):
        lines.append(f'\n  {group}')
        for command in COMMANDS:
            if command.group == group:
                description = command.description.split('；', 1)[0]
                lines.append(f'    {command.name:<16} {description}')
    return '\n'.join(lines)
