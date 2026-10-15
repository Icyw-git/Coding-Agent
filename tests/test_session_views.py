import asyncio
import json

import pytest

from cli_input import SessionPicker, fit_cells, text_width
from feishu_gateway import FeishuGateway
from feishu_sessions import list_card
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.data_structures import Size
from session_store import SessionEventStore
from session_views import group_sessions, session_status


def rows():
    return [dict(id='old', title='修复终端会话管理', workspace='D:/project-a', preview='修复选择器',
                 last_assistant='已修复选择器的键盘处理。', updated=10),
            dict(id='current', title='当前任务', workspace='D:/project-a', preview='继续检查', updated=20),
            dict(id='empty', title='新会话', workspace='D:/project-b', preview='尚无任务', updated=30)]


class TerminalOutput(DummyOutput):
    def __init__(self, columns):
        self.columns = columns

    def get_size(self):
        return Size(rows=35, columns=self.columns)


@pytest.mark.parametrize('keys, expected', [('\t\r', 'current'), ('\x1b[Z\r', 'empty'),
                                            ('\x1b[6~\r', 'old'), ('\x1b', None)])
def test_picker_keyboard_filters_paging_and_cancel(keys, expected):
    async def exercise():
        with create_pipe_input() as pipe:
            picker = SessionPicker(rows(), current_id='current', input=pipe, output=TerminalOutput(150))
            task = asyncio.create_task(picker.application.run_async())
            pipe.send_text(keys)
            assert await asyncio.wait_for(task, timeout=3) == expected
    asyncio.run(exercise())


@pytest.mark.parametrize('width, has_details', [(150, True), (70, False)])
def test_actual_terminal_render_adapts_to_width(width, has_details):
    async def exercise():
        with create_pipe_input() as pipe:
            picker = SessionPicker(rows(), current_id='current', input=pipe, output=TerminalOutput(width))
            task = asyncio.create_task(picker.application.run_async())
            try:
                for _ in range(100):
                    screen = picker.application.renderer._last_screen
                    if screen is not None:
                        break
                    await asyncio.sleep(0.01)
                assert screen is not None
                rendered = '\n'.join(''.join(screen.data_buffer[y][x].char for x in range(width))
                                     for y in range(35))
                assert '会话管理' in rendered and 'project-b' in rendered
                assert ('会话详情' in rendered) is has_details
                assert 'internal-id' not in rendered
                pipe.send_bytes(b'\x03')
                assert await asyncio.wait_for(task, timeout=3) is None
            finally:
                if not task.done():
                    task.cancel()
    asyncio.run(exercise())


def test_grouping_details_and_cjk_columns(tmp_path):
    assert [group for group, _ in group_sessions(rows())] == ['D:/project-b', 'D:/project-a']
    assert [session_status(row, 'current') for row in rows()] == ['历史', '当前', '空会话']
    assert text_width(fit_cells('中文会话主题' * 10, 18, pad=True)) == 18
    assert fit_cells('中文会话主题' * 10, 18).endswith('…')
    store = SessionEventStore(tmp_path)
    store.append_message({'role': 'user', 'content': '修复界面\n保持可读'})
    store.append_message({'role': 'assistant', 'content': '这是最近的回复'})
    store.append_message({'role': 'tool', 'content': 'hidden tool data'})
    summary = store.session_summary()
    assert summary['last_user'] == '修复界面\n保持可读'
    assert summary['last_assistant'] == '这是最近的回复'


class CardTransport:
    def __init__(self, fail=False):
        self.cards, self.texts = [], []
        self.fail = fail

    def send_session_card(self, chat, card):
        if self.fail:
            raise RuntimeError('card unavailable')
        self.cards.append((chat, card))

    def send_text(self, chat, text):
        self.texts.append((chat, text))


class API:
    def __init__(self):
        self.rows = [dict(id=f'session-{index}', title=f'任务 {index}', preview=f'用户任务 {index}',
                          workspace='D:/project', updated=index, last_assistant=f'回复 {index}')
                     for index in range(12)]

    def list_sessions(self, limit=20):
        return self.rows[:limit]

    def get_workspace(self, session_id):
        if not any(row['id'] == session_id for row in self.rows):
            raise FileNotFoundError(session_id)
        return 'D:/project'


def buttons(card):
    return [button['value'] for element in card['elements'] for button in element.get('actions', [])]


def test_feishu_card_pages_details_and_switch_on_dispatcher(tmp_path):
    api, transport = API(), CardTransport()
    gw = FeishuGateway(api, transport=transport, bindings_path=tmp_path / 'bindings.json',
                       allowed_users=['owner'])
    gw.start_dispatcher(workers=1)
    try:
        gw.on_message('chat', '/sessions', sender_id='owner')
        card = transport.cards[-1][1]
        assert '任务 11' in json.dumps(card, ensure_ascii=False)
        action = next(action for action in buttons(card) if action['action'] == 'page')
        assert gw.handle_card_action('owner', action, 'message', chat_id='chat')[1]
        gw._events.join()
        card = transport.cards[-1][1]
        assert '第 2/3 页' in json.dumps(card, ensure_ascii=False)
        action = next(action for action in buttons(card) if action['action'] == 'details')
        selected = gw._session_choices['chat'][action['index']]
        assert gw.handle_card_action('owner', action, 'message', chat_id='chat')[1]
        gw._events.join()
        detail = transport.cards[-1][1]
        assert selected['last_assistant'] in json.dumps(detail, ensure_ascii=False)
        assert any(element.get('text', {}).get('tag') == 'lark_md' for element in detail['elements'])
        switch = next(action for action in buttons(detail) if action['action'] == 'switch')
        assert gw.handle_card_action('owner', switch, 'message', chat_id='chat')[1]
        gw._events.join()
        assert gw.bindings['chat'] == selected['id']
    finally:
        gw.stop()


def test_feishu_rejects_forwarded_expired_and_unauthorized_buttons(tmp_path, monkeypatch):
    transport = CardTransport()
    gw = FeishuGateway(API(), transport=transport, bindings_path=tmp_path / 'bindings.json',
                       allowed_users=['owner'])
    gw.on_message('chat', '/sessions', sender_id='owner')
    action = buttons(transport.cards[-1][1])[0]
    assert not gw.handle_card_action('stranger', action, 'message', chat_id='chat')[1]
    assert not gw.handle_card_action('owner', action, 'message', chat_id='other')[1]
    assert not gw.handle_card_action('owner', dict(action, index=-1), 'message', chat_id='chat')[1]
    gw.on_message('chat', '/sessions', sender_id='owner')
    assert not gw.handle_card_action('owner', action, 'message', chat_id='chat')[1]
    action = buttons(transport.cards[-1][1])[0]
    created = gw._session_views['chat'][2]
    monkeypatch.setattr('feishu_gateway.time.monotonic', lambda: created + 601)
    assert not gw.handle_card_action('owner', action, 'message', chat_id='chat')[1]
    assert not gw.bindings and gw._events.empty()


def test_feishu_deleted_session_and_text_fallback_keep_binding(tmp_path):
    api, transport = API(), CardTransport(fail=True)
    gw = FeishuGateway(api, transport=transport, bindings_path=tmp_path / 'bindings.json')
    gw.on_message('chat', '/sessions')
    assert '/resume' in transport.texts[-1][1]
    gw.on_message('chat', '/sessions 2')
    assert '6.' in transport.texts[-1][1]
    token = gw._session_views['chat'][0]
    api.rows.clear()
    gw._handle_session_action('chat', {'session_view': token, 'action': 'switch', 'index': 0}, None)
    assert not gw.bindings
    assert '无法打开' in transport.texts[-1][1]


def test_session_card_stays_small_for_chinese_content():
    data = [dict(id=str(index), title='中文标题' * 1000, workspace='目录' * 1000,
                 updated=1, preview='内容') for index in range(5)]
    assert len(json.dumps(list_card(data, 'token'), ensure_ascii=False).encode('utf-8')) < 24000
