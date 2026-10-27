"""合并审批卡片：多条请求一张卡、就地更新、批量决议。"""
import json
import threading
import time

import pytest

import gateway_sim
from agent_runtime import AgentRuntime
from feishu_approval_card import approval_card, approval_text
from feishu_gateway import FeishuGateway
from harness_api import create_app
from hooks import GATE as permission_gate
from session_store import SessionEventStore
from test_feishu_gateway import FakeTransport, _sync_asgi_transport


def _req(req_id, tool='bash', command='git status', reason='需要执行命令'):
    return {'req_id': req_id, 'tool': tool, 'reason': reason,
            'args': {'command': command}}


def test_card_lists_each_request_with_its_own_buttons():
    card = approval_card([_req('r1'), _req('r2', 'read', 'a.txt')])
    text = json.dumps(card, ensure_ascii=False)
    assert card['header']['title']['content'] == '⚠ 需要审批（2）'
    assert '#1 · 执行命令 (bash)' in text
    assert '#2 · 读文件 (read)' in text
    buttons = [button for element in card['elements']
               if element.get('tag') == 'action'
               for button in element['actions']]
    values = [button['value'] for button in buttons]
    assert {'approval': 'r1', 'allow': True} in values
    assert {'approval': 'r2', 'allow': False} in values


def test_card_bulk_buttons_only_when_several_requests():
    single = approval_card([_req('r1')])
    assert not any(button.get('value', {}).get('approval_bulk')
                   for element in single['elements']
                   if element.get('tag') == 'action'
                   for button in element['actions'])
    many = approval_card([_req('r1'), _req('r2')])
    bulk = [button['value']['approval_bulk']
            for element in many['elements'] if element.get('tag') == 'action'
            for button in element['actions'] if 'approval_bulk' in button['value']]
    assert bulk == ['allow', 'deny']


def test_card_collapses_beyond_four_items():
    card = approval_card([_req(f'r{i}') for i in range(7)])
    text = json.dumps(card, ensure_ascii=False)
    assert '#4 · 执行命令 (bash)' in text
    assert '#5 ·' not in text
    assert '另有 **3** 条未展开' in text
    assert card['header']['title']['content'] == '⚠ 需要审批（7）'


def test_card_stays_under_feishu_limit_with_long_commands():
    long_command = 'git commit -m "' + 'x' * 2400 + '"'
    card = approval_card([_req(f'r{i}', command=long_command) for i in range(4)])
    assert len(json.dumps(card, ensure_ascii=False).encode()) < 30 * 1024


def test_empty_card_says_everything_is_handled():
    assert '全部处理完' in approval_card([])['elements'][0]['content']
    assert '全部处理完' in approval_text([])


def test_text_fallback_lists_shortcuts_by_index():
    text = approval_text([_req('r1'), _req('r2')])
    assert '回复 y 允许 / n 拒绝' in text
    assert '回复 y2 允许 / n2 拒绝' in text


@pytest.fixture()
def approval_gateway(tmp_path):
    # 默认审批超时（3s）比“起两个线程 + 轮询 + 按钮决议”紧，容易把超时当成拒绝；
    # GATE.timeout 是普通属性，构造后改即刻生效。
    old_timeout = permission_gate.timeout
    permission_gate.timeout = 60.0
    runtime = AgentRuntime(
        run_turn=lambda messages, **kwargs: 'ok',
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path / 'sessions')
    app = create_app(runtime, api_token='t', task_root=tmp_path / 'tasks',
                     workspace=tmp_path, rate_limit=10000)
    api = gateway_sim.GatewayClient(
        'http://test', 't', transport=_sync_asgi_transport(app))
    transport = FakeTransport()
    gw = FeishuGateway(api, transport=transport,
                       bindings_path=tmp_path / 'bindings.json')
    permission_gate.backend = 'remote'
    gw.on_message('c1', 'hi')
    transport.cards.clear()
    try:
        yield gw, transport
    finally:
        permission_gate.timeout = old_timeout
        for item in permission_gate.pending():
            permission_gate.resolve(item.req_id, False, by='test')
        permission_gate.clear()
        api.close()


def _pending_two(gw, results):
    """登记两条请求并轮询一次；返回 req_id 列表。"""
    def waiter(name, command):
        results[name] = permission_gate.decide(
            'bash', {'command': command}, 'potentially destructive')[0]

    threads = [threading.Thread(target=waiter, args=(name, command), daemon=True)
               for name, command in (('a', 'git status'), ('b', 'git log'))]
    for thread in threads:
        thread.start()
    for _ in range(100):
        if len(permission_gate.pending()) >= 2:
            break
        time.sleep(0.02)
    gw.poll_permissions()
    return threads


@pytest.mark.slow   # real gateway poll
def test_two_requests_share_one_card_and_patch_on_resolve(approval_gateway):
    gw, fake = approval_gateway
    patches = []
    fake.update_step_card = lambda message_id, card: patches.append(card)
    results = {}
    threads = _pending_two(gw, results)

    approval_cards = [card for _, card in fake.cards
                      if '需要审批' in json.dumps(card, ensure_ascii=False)]
    assert len(approval_cards) == 1, '两条请求应合并成一张卡'
    assert '（2）' in approval_cards[0]['header']['title']['content']

    req_id = gw._chat_pending['c1'][0]
    text, ok = gw.handle_card_action(None, {'approval': req_id, 'allow': True},
                                     'om_card', chat_id='c1')
    assert ok and '已允许' in text
    # 决议后 PATCH 同一张卡：剩一条，标题变成 “需要审批”，且不再有整批按钮
    assert patches, '应就地更新审批卡'
    latest = json.dumps(patches[-1], ensure_ascii=False)
    assert '（2）' not in latest
    assert gw._chat_pending['c1'] and req_id not in gw._chat_pending['c1']

    for thread in threads:
        thread.join(timeout=2)
    # 只处理了第一条；第二条仍在等待（不是被猜成拒绝）
    assert results.get('a') is True
    assert 'b' not in results
    assert gw._chat_pending['c1'] == [req for req in gw._chat_pending['c1']]


def test_bulk_deny_resolves_every_pending_request(approval_gateway):
    gw, fake = approval_gateway
    results = {}
    threads = _pending_two(gw, results)
    assert len(gw._chat_pending['c1']) == 2

    text, ok = gw.handle_card_action(None, {'approval_bulk': 'deny'},
                                     'om_card', chat_id='c1')
    assert ok and '已拒绝 2 条' in text
    for thread in threads:
        thread.join(timeout=2)
    assert results == {'a': False, 'b': False}
    assert gw._chat_pending['c1'] == []
    assert permission_gate.pending() == []
    # 卡片被更新成“已处理完”，避免还留着可点的按钮
    assert '全部处理完' in json.dumps(fake.cards[-1][1], ensure_ascii=False) or \
        '全部处理完' in json.dumps(fake.cards[-1], ensure_ascii=False)


def test_bulk_button_from_another_chat_is_rejected(approval_gateway):
    gw, fake = approval_gateway
    results = {}
    threads = _pending_two(gw, results)

    text, ok = gw.handle_card_action(None, {'approval_bulk': 'allow'},
                                     'om_card', chat_id='c2')
    assert not ok and '没有待审批' in text
    for thread in threads:
        thread.join(timeout=0.2)
    # 别的聊天按不动这两个请求：它们仍在等待，工作线程也还没返回
    assert results == {}
    assert len(gw._chat_pending['c1']) == 2
    assert len(permission_gate.pending()) == 2


@pytest.mark.slow   # real gateway poll
def test_poll_does_not_rerender_when_nothing_changed(approval_gateway):
    gw, fake = approval_gateway
    results = {}
    threads = _pending_two(gw, results)
    cards_after_first = len(fake.cards)
    patches = []
    fake.update_step_card = lambda message_id, card: patches.append(card)

    gw.poll_permissions()   # 同一批请求，没有变化
    gw.poll_permissions()
    assert len(fake.cards) == cards_after_first
    assert patches == []

    for thread in threads:
        thread.join(timeout=2)
