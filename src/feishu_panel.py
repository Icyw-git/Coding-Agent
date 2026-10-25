"""飞书控制台卡片：把常用操作做成按钮，日常使用不需要记斜杠命令。

与 `feishu_sessions` 的会话卡片同一套协议：按钮 `value` 回传服务端可识别的动作，
网关在分发线程里执行，回调线程只负责尽快返回 toast。
"""


def _text(content: str) -> dict:
    return {'tag': 'div',
            'text': {'tag': 'plain_text', 'content': content}}


def _button(label: str, action: str, *, primary: bool = False) -> dict:
    return {'tag': 'button',
            'text': {'tag': 'plain_text', 'content': label},
            'type': 'primary' if primary else 'default',
            'value': {'panel': action}}


def control_card(*, session_id: str = '', workspace: str = '',
                 model: str = '', intro: str = '', pending: int = 0,
                 crons: int = 0) -> dict:
    """控制台卡片：新会话 / 会话列表 / 停止 / 状态 / 定时任务 / 说明。

    pending / crons 只是数量（待审批按条数、定时任务按个数），不贴命令正文，
    这样一眼看到“有没有事要做”，点进对应卡片才看细节。
    """
    lines = []
    if intro:
        lines.append(intro)
    if session_id:
        lines.append(f'会话 {session_id[:8]}')
    if workspace:
        lines.append(f'工作区 {workspace}')
    if model:
        lines.append(f'模型 {model}')
    pending_note = f'待审批 {pending} 条'
    if pending:
        pending_note += '（回 y / n 或点卡片按钮）'
    lines.append(f'{pending_note} · 定时任务 {crons} 个')
    if not lines:
        lines.append('直接发消息即可开始；需要切换或停止时用下面的按钮。')
    return {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': 'Harness 控制台'}},
        'elements': [
            _text(chr(10).join(lines)),
            {'tag': 'action', 'actions': [
                _button('新会话', 'new', primary=True),
                _button('会话列表', 'sessions'),
            ]},
            {'tag': 'action', 'actions': [
                _button('停止当前回合', 'stop'),
                _button('当前状态', 'status'),
            ]},
            {'tag': 'action', 'actions': [
                _button('定时任务', 'crons'),
                _button('使用说明', 'help'),
            ]},
        ],
    }
