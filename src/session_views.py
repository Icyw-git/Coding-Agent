"""Shared, read-only presentation data for terminal and chat session browsers."""

# 相对时间是唯一的活跃时间展示口径（cli_input 从这里再导入）
from turn_progress import relative_time  # noqa: F401


def display_text(value):
    return ''.join(char for char in str(value or '')
                   if char in '\n\t' or (char.isprintable() and char != '\x7f'))


def session_status(row, current_id=None):
    if row['id'] == current_id:
        return '当前'
    return '空会话' if row.get('preview') == '尚无任务' else '已保存'


def group_sessions(rows):
    groups = {}
    for row in sorted(rows, key=lambda row: (row.get('updated', 0), row['id']), reverse=True):
        groups.setdefault(row.get('workspace') or '工作目录未记录', []).append(row)
    return list(groups.items())


def session_details(row, current_id=None):
    return [
        ('主题', row.get('title') or '新会话'),
        ('状态', session_status(row, current_id)),
        ('工作区', row.get('workspace') or '工作目录未记录'),
        ('最近活跃', relative_time(row.get('updated'))),
        ('最近任务', row.get('last_user') or row.get('preview') or '尚无任务'),
        ('最近回复', row.get('last_assistant') or '尚无文字回复'),
    ]
