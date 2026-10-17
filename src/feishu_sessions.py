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
