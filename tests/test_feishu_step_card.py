"""飞书 schema 2.0 过程卡与工具摘要的行为回归。"""
import json

from feishu_gateway import FeishuGateway
from feishu_step_card import step_card, tool_lines


class StepTransport:
    """记录 step card 的发送/更新（schema 2.0 路径）。"""

    def __init__(self):
        self.sent = []
        self.updated = []

    def send_step_card(self, chat_id, card, reply_to=None):
        self.sent.append((chat_id, card, reply_to))
        return 'om_card_1'

    def update_step_card(self, message_id, card):
        self.updated.append((message_id, card))


def _all_markdown(card: dict) -> str:
    """把卡片里所有 markdown/plain_text 内容拼起来，便于断言。"""

    def walk(node):
        parts = []
        if isinstance(node, dict):
            if node.get('tag') in ('markdown', 'plain_text') and 'content' in node:
                parts.append(str(node['content']))
            for value in node.values():
                parts.extend(walk(value))
        elif isinstance(node, list):
            for value in node:
                parts.extend(walk(value))
        return parts

    return '\n'.join(walk(card))


def test_tool_lines_summarise_counts_and_errors():
    lines = tool_lines({'bash': 3, 'read': 1},
                       [{'tool': 'glob', 'error': 'SEARCH_FAILED'}],
                       total_ms=1234)
    assert '`bash` × 3' in lines[0]
    assert '`read` × 1' in lines[1]
    assert '累计耗时 1.2s' in '\n'.join(lines)
    assert '⚠️ `glob` — SEARCH_FAILED' in lines


def test_step_card_has_process_header_and_collapsed_details():
    card = step_card(answer='答案 **bold**',
                     tool_counts={'bash': 2}, note='已完成 · 总用时 00:12')
    assert card['schema'] == '2.0'
    assert card['header']['title']['content'] == '⚙️ 任务过程'
    elements = card['body']['elements']
    assert elements[0] == {'tag': 'markdown', 'content': '答案 **bold**'}
    details = next(item for item in elements if item.get('tag') == 'collapsible_panel')
    assert details['expanded'] is False
    assert details['header']['title']['content'] == '📋 过程详情'
    inner = details['elements'][0]
    assert inner['tag'] == 'collapsible_panel'
    assert inner['header']['title']['content'] == '🛠️ 工具摘要（2）'
    assert '`bash` × 2' in _all_markdown(inner)
    assert elements[-1]['content'] == '_已完成 · 总用时 00:12_'


def test_running_card_keeps_tools_visible_without_details_wrapper():
    card = step_card(status='running', tool_counts={'bash': 1},
                     note='运行中 · 正在执行 bash · 00:03')
    panels = [item for item in card['body']['elements']
              if item.get('tag') == 'collapsible_panel']
    assert len(panels) == 1
    assert panels[0]['header']['title']['content'] == '🛠️ 工具摘要（1）'
    assert '正在执行 bash' in _all_markdown(card)


def test_gateway_streams_step_card_and_reports_tools(monkeypatch):
    class API:
        last_duration = None

        def send_stream(self, content, session_id=None, on_delta=None,
                        on_progress=None):
            if on_progress:
                on_progress({'phase': 'tool', 'tool': 'bash', 'elapsed': 3,
                             'elapsed_text': '00:03',
                             'tool_counts': {'bash': 1},
                             'tool_errors': [], 'tool_total_ms': 1200})
            if on_delta:
                on_delta('答案')
            if on_progress:
                on_progress({'phase': 'thinking', 'tool': None, 'elapsed': 5,
                             'elapsed_text': '00:05',
                             'tool_counts': {'bash': 1},
                             'tool_errors': [{'tool': 'glob',
                                              'error': 'SEARCH_FAILED'}],
                             'tool_total_ms': 1500})
            return '答案'

    transport = StepTransport()
    gw = FeishuGateway(API(), transport=transport)
    gw._send_agent_reply('oc_chat', 's1', '问题', reply_to='om_user_1')

    assert transport.sent, '应发送 schema 2.0 过程卡'
    chat_id, card, reply_to = transport.sent[0]
    assert chat_id == 'oc_chat' and reply_to == 'om_user_1'
    assert card['schema'] == '2.0' and card['header']['title']['content'] == '⚙️ 任务过程'

    assert transport.updated, '最终内容应更新同一张卡'
    final = transport.updated[-1][1]
    text = _all_markdown(final)
    assert '已完成 · 总用时' in text
    assert '工具摘要（1）' in text and '`bash` × 1' in text
    buttons = [element for element in final['body']['elements']
               if element.get('tag') == 'column_set']
    assert buttons, '完成后卡片应带常驻控制台入口'
    assert buttons[-1]['columns'][0]['elements'][0]['behaviors'][0]['value'] == {
        'panel': 'menu'}
    assert 'SEARCH_FAILED' in text, '工具失败要出现在过程详情里'


def test_step_card_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv('FEISHU_STEP_CARD', '0')
    calls = {}

    class API:
        last_duration = '00:01'

        def send_stream(self, content, session_id=None, on_delta=None,
                        on_progress=None):
            if on_delta:
                on_delta('答案')
            return '答案'

    class LegacyTransport:
        def send_markdown(self, chat_id, text, reply_to=None):
            calls['sent'] = (chat_id, text, reply_to)
            return 'om_1'

        def update_markdown(self, message_id, text):
            calls['updated'] = (message_id, text)

    gw = FeishuGateway(API(), transport=LegacyTransport())
    gw._send_agent_reply('oc_chat', 's1', '问题', reply_to='om_user_1')
    assert '答案' in calls['sent'][1]
    assert calls['sent'][2] == 'om_user_1'
    assert 'updated' in calls


def test_step_card_json_stays_serialisable():
    card = step_card(answer='x' * 30_000, tool_counts={'bash': 5},
                     note='已完成')
    json.dumps(card, ensure_ascii=False)   # 不应包含不可序列化对象
    assert len(_all_markdown(card)) < 40_000
