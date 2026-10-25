"""帮助卡片（schema 2.0）：常用入口做成按钮，命令清单折叠起来。

`/help` 以前是一大段纯文本，手机上一屏看不完，也点不了。这里改成：
- 顶部一句“日常不用记命令”+ 三句可照抄的例句；
- 一排常用入口按钮（控制台 / 会话 / 定时任务 / 待审批 / 后台任务）；
- 命令清单放进折叠面板，需要排障时再展开。

按钮只回传 `{"panel": ...}` / `{"task_action": "refresh"}`，仍由网关统一分发，
不引入新的执行路径。
"""
from __future__ import annotations


def _plain(content: str) -> dict:
    return {'tag': 'plain_text', 'content': str(content)}


def _markdown(content: str) -> dict:
    return {'tag': 'markdown', 'content': str(content)}


def _button(label: str, value: dict, *, kind: str = 'default') -> dict:
    return {'tag': 'button',
            'text': _plain(label),
            'type': kind,
            'behaviors': [{'type': 'callback', 'value': value}]}


EXAMPLES = (
    '帮我看看这个项目最近改了什么',
    '每天早上 9 点提醒我喝水',
    '我们上次聊到哪了',
)


def help_card(commands: list[str] | str) -> dict:
    """常用入口 + 折叠命令清单。commands 直接接受 `_help_text` 的多行文本。"""
    lines = ([commands] if isinstance(commands, str)
             else list(commands))
    body = '\n'.join(line for line in lines if line).strip()
    example_text = '\n'.join(f'· {item}' for item in EXAMPLES)
    return {
        'schema': '2.0',
        'header': {'title': _plain('使用说明'), 'template': 'blue'},
        'body': {'elements': [
            _markdown('**日常不用记命令**，直接说话就行：\n' + example_text),
            {'tag': 'column_set', 'flex_mode': 'none', 'columns': [
                {'tag': 'column', 'width': 'weighted', 'weight': 1,
                 'elements': [_button('⚙️ 控制台', {'panel': 'menu'},
                                      kind='primary')]},
                {'tag': 'column', 'width': 'weighted', 'weight': 1,
                 'elements': [_button('💬 会话列表', {'panel': 'sessions'})]},
            ]},
            {'tag': 'column_set', 'flex_mode': 'none', 'columns': [
                {'tag': 'column', 'width': 'weighted', 'weight': 1,
                 'elements': [_button('⏰ 定时任务', {'panel': 'crons'})]},
                {'tag': 'column', 'width': 'weighted', 'weight': 1,
                 'elements': [_button('✅ 待审批', {'panel': 'perms'})]},
            ]},
            # schema 2.0 不再支持 tag=action（飞书报 200861），按钮一律放进 column_set。
            {'tag': 'column_set', 'flex_mode': 'none', 'columns': [
                {'tag': 'column', 'width': 'weighted', 'weight': 1,
                 'elements': [_button('📋 后台任务', {'task_action': 'refresh'})]},
                {'tag': 'column', 'width': 'weighted', 'weight': 1,
                 'elements': [_button('⏹ 停止当前回合', {'panel': 'stop'},
                                      kind='danger')]},
            ]},
            {'tag': 'collapsible_panel',
             'expanded': False,
             'background_color': 'grey-50',
             'border': {'color': 'grey', 'corner_radius': '8px'},
             'padding': '8px 8px 8px 8px',
             'header': {
                 'title': _plain('📖 命令清单（高级用法 / 排障）'),
                 'vertical_align': 'center',
                 'padding': '8px 8px 8px 8px',
                 'icon': {'tag': 'standard_icon',
                          'token': 'down-small-ccm_outlined',
                          'color': 'grey', 'size': '16px 16px'},
                 'icon_position': 'right',
                 'icon_expanded_angle': -180,
             },
             'elements': [_markdown(body or '（无）')]},
        ]},
    }
