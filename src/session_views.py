"""Shared, read-only presentation data for terminal and chat session browsers."""

import time


def relative_time(timestamp, now=None):
    if not timestamp:
        return '未知时间'
    elapsed = max(0, int((time.time() if now is None else now) - timestamp))
    if elapsed < 60:
        return '刚刚'
    if elapsed < 3600:
        return f'{elapsed // 60} 分钟前'
    if elapsed < 86400:
        return f'{elapsed // 3600} 小时前'
    if elapsed < 604800:
        return f'{elapsed // 86400} 天前'
    return time.strftime('%Y-%m-%d', time.localtime(timestamp))


def absolute_time(timestamp):
    if not timestamp:
        return '未知时间'
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(timestamp))


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
        ('最近活跃', absolute_time(row.get('updated', 0))),
        ('最近任务', row.get('last_user') or row.get('preview') or '尚无任务'),
        ('最近回复', row.get('last_assistant') or '尚无文字回复'),
    ]
