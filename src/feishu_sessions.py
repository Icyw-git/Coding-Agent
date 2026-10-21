"""Small, mobile-friendly session cards; all actions use server-side snapshots."""

from session_views import display_text, relative_time, session_details, session_status

PAGE_SIZE = 5


def _text(text):
    return {'tag': 'div', 'text': {'tag': 'plain_text', 'content': display_text(text)}}


def _button(label, token, action, index, primary=False):
    return {'tag': 'button', 'text': {'tag': 'plain_text', 'content': label},
            'type': 'primary' if primary else 'default',
            'value': {'session_view': token, 'action': action, 'index': index}}


def list_card(rows, token, current_id=None, page=0):
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    elements = []
    previous_group = None
    for index in range(page * PAGE_SIZE, min(len(rows), (page + 1) * PAGE_SIZE)):
        row = rows[index]
        workspace = row.get('workspace') or '工作目录未记录'
        if workspace != previous_group:
            elements.append(_text(f'工作区 · {workspace[:200]}'))
            previous_group = workspace
        elements.append(_text(f'{index + 1}. {row["title"][:80]}\n'
                              f'{session_status(row, current_id)} · 最近活跃 {relative_time(row.get("updated"))}'))
        elements.append({'tag': 'action', 'actions': [
            _button('查看详情', token, 'details', index),
            _button('切换到此会话', token, 'switch', index, True),
        ]})
    navigation = []
    if page:
        navigation.append(_button('上一页', token, 'page', page - 1))
    if page + 1 < pages:
        navigation.append(_button('下一页', token, 'page', page + 1))
    if navigation:
        elements.append({'tag': 'action', 'actions': navigation})
    elements.append(_text(f'第 {page + 1}/{pages} 页 · 共 {len(rows)} 个会话\n'
                          '/resume 关键词 搜索 · /resume 序号 切换\n旧卡片失效后请重新 /sessions。'))
    return {'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text', 'content': '会话管理 · 按工作区分组'}},
            'elements': elements}


def workspace_card(rows, token):
    """新建会话的工作区选择卡：一行一个目录，按钮回传服务端快照序号。"""
    elements = []
    for index, row in enumerate(rows):
        marks = []
        if row.get('current'):
            marks.append('当前')
        if row.get('saved'):
            marks.append('已登记')
        if not row.get('exists', True):
            marks.append('目录不存在')
        elif not row.get('allowed', True):
            marks.append('未授权')
        details = []
        if row.get('sessions'):
            details.append(f"{row['sessions']} 个会话")
        if row.get('last_active'):
            details.append(relative_time(row['last_active']))
        details.extend(marks)
        meta = f"\n{' · '.join(details)}" if details else ''
        elements.append(_text(f"{index + 1}. {row['name']}\n{row['path']}{meta}"))
        elements.append({'tag': 'action', 'actions': [{
            'tag': 'button',
            'text': {'tag': 'plain_text',
                     'content': '使用当前目录' if row.get('current') else '选择此目录'},
            'type': 'primary' if row.get('current') else 'default',
            'value': {'workspace_view': token, 'index': index},
        }]})
    elements.append(_text('旧卡片失效后请重新 /new；也可以 /new <路径> 直接指定目录。'))
    return {'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text', 'content': '新建会话 · 选择工作区'}},
            'elements': elements}


def detail_card(row, token, index, current_id=None, reply_markdown=None):
    elements = []
    for label, value in session_details(row, current_id):
        if label == '最近回复' and reply_markdown is not None:
            elements.extend([_text(label), {'tag': 'div', 'text': {
                'tag': 'lark_md', 'content': reply_markdown}}])
        else:
            elements.append(_text(f'{label}\n{str(value)[:2000]}'))
    elements.append({'tag': 'action', 'actions': [
        _button('返回列表', token, 'page', index // PAGE_SIZE),
        _button('切换到此会话', token, 'switch', index, True),
        _button('分叉新会话', token, 'fork', index),
    ]})
    return {'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text', 'content': '会话详情'}},
            'elements': elements}
