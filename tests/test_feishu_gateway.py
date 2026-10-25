"""FeishuGateway 核心测试：fake transport + 真实 FastAPI app（ASGITransport）。

不依赖 lark-oapi 与网络——只测 on_message 编排 / 命令分发 / 审批联动。
"""

import json
import os
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import feishu_gateway
import gateway_sim
from feishu_gateway import FeishuGateway, format_permission_prompt
from harness_api import create_app
from hooks import GATE as permission_gate


class FakeTransport:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.cards: list[tuple[str, dict]] = []

    def send_text(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    def send_session_card(self, chat_id: str, card: dict) -> str:
        self.cards.append((chat_id, card))
        return f'om-card-{len(self.cards)}'

    def last(self) -> str:
        return self.sent[-1][1] if self.sent else ''

    def last_card(self) -> str:
        return (json.dumps(self.cards[-1][1], ensure_ascii=False)
                if self.cards else '')

    def all_text(self) -> str:
        return '\n'.join(text for _, text in self.sent)


@pytest.fixture()
def api_app(tmp_path):
    from agent_runtime import AgentRuntime
    from session_store import SessionEventStore

    runtime = AgentRuntime(
        run_turn=lambda messages, **kwargs: 'agent-reply-ok',
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path / 'sessions',
    )
    return create_app(runtime, api_token='t',
                      task_root=tmp_path / 'tasks', workspace=tmp_path,
                      rate_limit=10000)


def _sync_asgi_transport(app):
    """同步桥：httpx.MockTransport → TestClient → ASGI app（不发网络请求）。

    注：此环境的 starlette TestClient 基于 httpx2（环境同时装有 httpx 0.28 与
    httpx2 2.x），必须把 httpx2.Response 转回 httpx.Response 再交给 MockTransport。
    """
    test_client = TestClient(app)

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ('content-length', 'host')}
        raw = test_client.request(
            request.method, request.url.path,
            params=dict(request.url.params),
            content=request.content,
            headers=headers,
        )
        return httpx.Response(raw.status_code, headers=dict(raw.headers),
                              content=raw.content)

    return httpx.MockTransport(handler)


@pytest.fixture()
def gateway(api_app, tmp_path, monkeypatch):
    monkeypatch.setattr(permission_gate, 'backend', 'remote')
    api = gateway_sim.GatewayClient(
        'http://test', 't', transport=_sync_asgi_transport(api_app))
    fake = FakeTransport()
    gw = FeishuGateway(api, transport=fake,
                       bindings_path=tmp_path / 'bindings.json')
    yield gw, fake
    api.close()
    monkeypatch.setattr(permission_gate, 'backend', 'console')


def test_first_message_creates_session_and_binds(gateway, tmp_path):
    gw, fake = gateway
    gw.on_message('chat-1', '你好，帮我看看项目')
    assert 'agent-reply-ok' in fake.last()
    assert gw.bindings['chat-1']

    # 绑定持久化
    reloaded = FeishuGateway(gw.api, transport=fake,
                             bindings_path=tmp_path / 'bindings.json')
    assert reloaded.bindings['chat-1'] == gw.bindings['chat-1']


def test_new_lists_workspaces_and_selects_by_number(gateway, tmp_path):
    """飞书 /new：先列候选、按序号选；带路径直接指定，不再要求用户记目录。"""
    gw, fake = gateway
    alpha = tmp_path / 'alpha'
    beta = tmp_path / 'beta'
    alpha.mkdir()
    beta.mkdir()

    gw.on_message('c1', '/cd "alpha"')
    bound = gw.bindings['c1']

    gw.on_message('c1', '/new')              # 只列候选，不创建会话
    card = _last_card_text(fake)
    assert '选择工作区' in card and 'alpha' in card and '1.' in card
    assert gw.bindings['c1'] == bound

    gw.on_message('c1', f'/new "{beta}"')    # 显式路径
    assert gw.api.get_workspace(gw.bindings['c1']) == str(beta)

    gw.on_message('c1', '/new')              # 当前 beta + 最近 alpha
    card = _last_card_text(fake)
    assert 'beta' in card and 'alpha' in card
    previous = gw.bindings['c1']
    gw.on_message('c1', '/new 2')            # 序号 2 = alpha
    assert gw.bindings['c1'] != previous
    assert gw.api.get_workspace(gw.bindings['c1']) == str(alpha)


def test_commands_run_status_logs_stop(gateway):
    gw, fake = gateway
    gw.on_message('c1', '/run echo feishu-task')
    assert '任务已创建' in fake.last()

    task_id = gw.api.list_tasks()[0]['id']
    for _ in range(100):
        if gw.api.get_task(task_id)['status'] in ('succeeded', 'failed'):
            break
        time.sleep(0.05)

    # /status <id> 与 /logs 现在都是卡片（就地可刷新/停止），不再回纯文本
    gw.on_message('c1', f'/status {task_id}')
    assert 'succeeded' in fake.last_card()
    gw.on_message('c1', f'/logs {task_id}')
    assert 'feishu-task' in fake.last_card()


def test_unknown_command_reports_error(gateway):
    gw, fake = gateway
    gw.on_message('c1', '/nope')
    assert '未知命令' in fake.last()


def test_help_and_unknown_include_command_list(gateway):
    """/help 列出全部命令；未知命令回复自动附带命令清单。"""
    gw, fake = gateway
    gw.on_message('c1', '/help')
    assert '可用命令' in fake.last()
    assert '日常不用记命令' in fake.last()
    assert '/use' in fake.last() and '/perms' in fake.last()
    assert '/model' in fake.last() and '/approvals' in fake.last()

    gw.on_message('c1', '/nope')
    assert '未知命令: /nope' in fake.last()
    assert '可用命令' in fake.last()


def test_fullwidth_input_normalized_for_commands(gateway):
    """中文输入法的全角斜杠/问号归一为半角后再路由命令。"""
    gw, fake = gateway
    gw.on_message('c1', '／use zzzz')   # 全角斜杠 → 命令而非 agent 消息
    assert any('未找到' in text for _, text in fake.sent)

    gw.on_message('c1', '/？')          # 全角问号 → help
    assert '可用命令' in fake.last()


def test_messages_use_explicit_chat_session_without_shared_client_state(tmp_path):
    class API:
        def __init__(self):
            self.calls = []

        def create_session(self):
            raise AssertionError('bindings are pre-created')

        def send(self, content, session_id=None):
            self.calls.append((content, session_id))
            return session_id

    api = API()
    fake = FakeTransport()
    gw = FeishuGateway(api, transport=fake, bindings_path=tmp_path / 'bindings.json')
    gw.bindings.update({'a': 'session-a', 'b': 'session-b'})
    threads = [threading.Thread(target=gw.on_message, args=(chat, 'hello'))
               for chat in ('a', 'b')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(api.calls, key=lambda row: row[1]) == [
        ('hello', 'session-a'), ('hello', 'session-b')]


def test_approval_dispatch_bypasses_chat_lock(tmp_path):
    class API:
        def pending_permissions(self): return []

    gw = FeishuGateway(API(), bindings_path=tmp_path / 'bindings.json')
    called = threading.Event()
    gw.on_message = lambda chat_id, text, sender_id=None, chat_type=None,         message_id=None: called.set()
    lock = gw._chat_lock('c1')
    lock.acquire()
    try:
        gw.start_dispatcher(workers=1)
        gw.dispatch('c1', '/approve req y')
        assert called.wait(1), 'approval was blocked by the chat lock'
    finally:
        lock.release()
        gw.stop()


def _drain_stale_permissions():
    """清理全局 gate 中上次测试遗留的 pending 请求（测试隔离）。"""
    for stale in permission_gate.pending():
        try:
            permission_gate.resolve(stale.req_id, allow=False, by='test-cleanup')
        except RuntimeError:
            pass


def test_approval_prompt_shows_the_whole_command():
    """命令完整性归展示层：网关再切一刀（旧版 [:160]）会把重定向目标弄没。"""
    command = 'echo ' + 'x' * 900 + ' >> ~/.bashrc\x1b[31m'
    text = format_permission_prompt({'req_id': 'p1', 'tool': 'bash',
                                     'reason': 'destructive',
                                     'args': {'command': command}})
    assert text.startswith('⚠ 需要审批')
    assert '~/.bashrc' in text
    assert text.count('```') == 2          # 命令在代码块里，按原样成段
    assert '\x1b' not in text and '\\x1b' in text


def test_permission_approval_via_chat(gateway):
    gw, fake = gateway
    _drain_stale_permissions()
    gw.on_message('c1', 'hi')   # 先建立聊天→会话绑定
    results = {}

    def waiter():
        results['allowed'] = permission_gate.decide(
            'bash', {'command': 'del x'}, 'destructive')[0]

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    for _ in range(50):
        if permission_gate.pending():
            break
        time.sleep(0.02)
    req_id = permission_gate.pending()[0].req_id

    # 轮询器推送合并审批卡（卡片正文里能看清工具与命令）
    gw.poll_permissions()
    assert 'bash' in fake.last_card()
    assert format_permission_prompt(
        {'req_id': req_id, 'tool': 'bash', 'reason': 'destructive',
         'args': {'command': 'del x'}}).startswith('⚠ 需要审批')

    # 聊天里回复审批
    gw.on_message('c1', f'/approve {req_id} y')
    thread.join(timeout=2)
    assert results['allowed'] is True
    assert '已允许' in fake.last()
    assert req_id not in fake.all_text()

    # 同一请求不会重复推送
    calls_before = len(fake.sent)
    gw.poll_permissions()
    assert len(fake.sent) == calls_before


def test_permission_shortcut_y_n_without_id(gateway):
    """y/n 快捷审批：无需输入 req_id，按 FIFO 决议本聊天最早的请求。"""
    gw, fake = gateway
    _drain_stale_permissions()
    gw.on_message('c1', 'hi')
    results = {}

    def waiter():
        results['allowed'] = permission_gate.decide(
            'bash', {'command': 'del x'}, 'destructive')[0]

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    for _ in range(50):
        if permission_gate.pending():
            break
        time.sleep(0.02)
    gw.poll_permissions()   # 推送提醒并登记到该聊天的 FIFO

    gw.on_message('c1', 'y')   # 不带 req_id
    thread.join(timeout=2)
    assert results['allowed'] is True
    assert '已允许' in fake.last()

    # 没有待审批时，y 当作普通消息发给 agent
    fake.sent.clear()
    gw.on_message('c1', 'y')
    assert any('agent-reply-ok' in text for _, text in fake.sent)


def test_indexed_y_n_resolves_by_position(gateway):
    """y2/n2 决议本聊天 FIFO 里第 2 条；不带序号决议第 1 条。"""
    gw, fake = gateway
    _drain_stale_permissions()
    gw.on_message('c1', 'hi')
    outcomes = []

    def waiter():
        outcomes.append(permission_gate.decide(
            'bash', {'command': 'del x'}, 'destructive')[0])

    threads = [threading.Thread(target=waiter, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for _ in range(100):
        if len(permission_gate.pending()) >= 2:
            break
        time.sleep(0.02)
    gw.poll_permissions()   # 两条都推送提醒并登记 FIFO

    gw.on_message('c1', 'n2')   # 拒绝第 2 条
    for thread in threads:
        thread.join(timeout=2)
    assert outcomes == [False]                   # 第 2 条被拒，第 1 条仍阻塞
    assert len(permission_gate.pending()) == 1

    gw.on_message('c1', 'y')    # 不带序号 → 决议第 1 条
    for thread in threads:
        thread.join(timeout=2)
    for _ in range(50):
        if not permission_gate.pending():
            break
        time.sleep(0.02)
    assert sorted(outcomes) == [False, True]
    assert permission_gate.pending() == []


def test_approval_push_uses_one_merged_card(gateway):
    """审批推送走合并卡片，不是一条请求一张卡，也不回退成文本。"""
    gw, fake = gateway
    _drain_stale_permissions()
    gw.on_message('c1', 'hi')
    baseline_sent = len(fake.sent)      # on_message('hi') 已产生的文本回复
    baseline_cards = len(fake.cards)

    results = {}

    def waiter():
        results['allowed'] = permission_gate.decide(
            'bash', {'command': 'del x'}, 'destructive')[0]

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    for _ in range(50):
        if permission_gate.pending():
            break
        time.sleep(0.02)
    req_id = permission_gate.pending()[0].req_id

    gw.poll_permissions()
    approval_cards = [card for chat, card in fake.cards[baseline_cards:]
                      if '需要审批' in json.dumps(card, ensure_ascii=False)]
    assert len(approval_cards) == 1
    text = json.dumps(approval_cards[0], ensure_ascii=False)
    assert req_id in text and '允许' in text and '拒绝' in text
    assert len(fake.sent) == baseline_sent   # 没有走文本回退

    gw.on_message('c1', 'y')        # 文本快捷键仍然生效
    thread.join(timeout=2)
    assert results['allowed'] is True


def test_card_button_resolves_permission(gateway):
    """按钮回调决议权限、清 FIFO、无按钮卡片时 toast 反馈结果。"""
    gw, fake = gateway
    _drain_stale_permissions()
    gw.on_message('c1', 'hi')
    results = {}

    def waiter():
        results['allowed'] = permission_gate.decide(
            'bash', {'command': 'del x'}, 'destructive')[0]

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    for _ in range(50):
        if permission_gate.pending():
            break
        time.sleep(0.02)
    req_id = permission_gate.pending()[0].req_id
    gw.poll_permissions()

    text, ok = gw.handle_card_action(
        'ou_me', {'approval': req_id, 'allow': True}, 'om_1', chat_id='c1')
    assert ok and '已允许' in text
    thread.join(timeout=2)
    assert results['allowed'] is True

    # FIFO 已清理：y 恢复为普通消息
    fake.sent.clear()
    gw.on_message('c1', 'y')
    assert any('agent-reply-ok' in text for _, text in fake.sent)


def test_card_button_respects_allowlist(api_app, tmp_path, monkeypatch):
    """白名单外的用户点按钮无效，权限请求保持 pending。"""
    monkeypatch.setattr(permission_gate, 'backend', 'remote')
    api = gateway_sim.GatewayClient(
        'http://test', 't', transport=_sync_asgi_transport(api_app))
    fake = FakeTransport()
    gw = feishu_gateway.FeishuGateway(api, transport=fake,
                                      bindings_path=tmp_path / 'bindings.json',
                                      allowed_users={'ou_me'})
    try:
        _drain_stale_permissions()
        results = {}

        def waiter():
            results['allowed'] = permission_gate.decide(
                'bash', {'command': 'del x'}, 'destructive')[0]

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        for _ in range(50):
            if permission_gate.pending():
                break
            time.sleep(0.02)
        req_id = permission_gate.pending()[0].req_id
        gw.bindings['c1'] = 'session-allowlist'
        gw.poll_permissions()   # 推送并登记 FIFO，按钮才有归属

        text, ok = gw.handle_card_action(
            'ou_stranger', {'approval': req_id, 'allow': True}, 'om_1',
            chat_id='c1')
        assert not ok and '没有审批权限' in text
        assert results == {}    # waiter 仍阻塞，未决议

        text, ok = gw.handle_card_action(
            'ou_me', {'approval': req_id, 'allow': False}, 'om_1',
            chat_id='c1')
        assert ok
        thread.join(timeout=2)
        assert results['allowed'] is False
    finally:
        api.close()
        monkeypatch.setattr(permission_gate, 'backend', 'console')


def test_group_messages_are_ignored_by_default(gateway):
    """个人助手只收单聊：群聊/话题群不建会话、不回复；p2p 正常。"""
    gw, fake = gateway
    gw.on_message('g1', 'hello', sender_id='ou_me', chat_type='group')
    gw.on_message('g1', 'hello', sender_id='ou_me', chat_type='topic')
    assert gw.bindings == {}
    assert fake.sent == []

    gw.on_message('g1', 'hello', sender_id='ou_me', chat_type='p2p')
    assert 'agent-reply-ok' in fake.last()
    assert gw.bindings['g1']


def test_dispatch_forwards_chat_type(tmp_path):
    """分发队列要把来源带过去；否则群消息在 worker 里丢掉 chat_type 就漏检。"""
    gw = FeishuGateway(object(), bindings_path=tmp_path / 'bindings.json')
    seen = []
    gw.on_message = lambda chat_id, text, sender_id=None, chat_type=None, \
        message_id=None: seen.append((chat_id, text, chat_type))
    gw.start_dispatcher(workers=1)
    try:
        gw.dispatch('g1', 'hello', sender_id='ou_me', chat_type='group')
        gw._events.join()
    finally:
        gw.stop()
    assert seen == [('g1', 'hello', 'group')]


def test_approvals_route_only_to_the_bound_session_chat(gateway):
    """审批只推给绑定其登记会话的聊天；/perms 也不能列出别人的请求。"""
    gw, fake = gateway
    _drain_stale_permissions()
    sid_a = gw.api.create_session()
    sid_b = gw.api.create_session()
    gw.bindings.update({'c1': sid_a, 'c2': sid_b})
    fake.sent.clear()

    req = permission_gate.request('bash', {'command': 'del x'}, 'destructive',
                                  session_id=sid_a)
    try:
        gw.poll_permissions()
        approval_chats = [chat for chat, card in fake.cards
                          if '需要审批' in json.dumps(card, ensure_ascii=False)]
        assert approval_chats == ['c1']
        assert req.req_id in gw._chat_pending['c1']
        assert req.req_id not in gw._chat_pending.get('c2', [])

        # /perms 同样按会话过滤：c2 看不到 c1 的请求，也不会登记进它的 FIFO
        fake.sent.clear()
        gw.on_message('c2', '/perms')
        assert fake.last() == '(无待审批请求)'
        assert req.req_id not in gw._chat_pending.get('c2', [])
    finally:
        permission_gate.resolve(req.req_id, False, by='test')


def test_approval_cannot_be_resolved_from_another_chat(gateway):
    """卡片按钮和 /approve <id> 都要求请求已推给本聊天。"""
    gw, fake = gateway
    _drain_stale_permissions()
    sid_a = gw.api.create_session()
    sid_b = gw.api.create_session()
    gw.bindings.update({'c1': sid_a, 'c2': sid_b})
    req = permission_gate.request('bash', {'command': 'del x'}, 'destructive',
                                  session_id=sid_a)
    try:
        gw.poll_permissions()   # 只推给 c1

        text, ok = gw.handle_card_action(
            'ou_me', {'approval': req.req_id, 'allow': True}, 'om_1',
            chat_id='c2')
        assert not ok and '不属于当前聊天' in text
        assert req.status == 'pending'

        fake.sent.clear()
        gw.on_message('c2', f'/approve {req.req_id} y')
        assert '不属于当前聊天' in fake.last()
        assert req.status == 'pending'

        text, ok = gw.handle_card_action(
            'ou_me', {'approval': req.req_id, 'allow': True}, 'om_2',
            chat_id='c1')
        assert ok and req.status == 'approved'
    finally:
        if req.status == 'pending':
            permission_gate.resolve(req.req_id, False, by='test')


def test_main_refuses_to_start_without_allowlist(monkeypatch, capsys):
    """没配置白名单就拒绝启动，不把“谁都能用真 shell”当默认。"""
    monkeypatch.setenv('FEISHU_APP_ID', 'cli_x')
    monkeypatch.setenv('FEISHU_APP_SECRET', 'secret')
    monkeypatch.setenv('HARNESS_API_TOKEN', 't')
    monkeypatch.setenv('FEISHU_ALLOWED_USERS', '')
    monkeypatch.setenv('FEISHU_ALLOW_ANY_USER', '')
    assert feishu_gateway.main() == 1
    assert 'FEISHU_ALLOWED_USERS is required' in capsys.readouterr().out


def test_main_allows_explicit_any_user_escape_hatch(monkeypatch, tmp_path):
    """本地测试可显式放开；启动后仍照常构造网关。"""
    # 单实例锁改用临时路径：否则本机真有一个网关在跑时，这条用例会被拒绝。
    monkeypatch.setattr(feishu_gateway, 'INSTANCE_LOCK_PATH',
                        tmp_path / 'gateway.lock')
    monkeypatch.setenv('FEISHU_APP_ID', 'cli_x')
    monkeypatch.setenv('FEISHU_APP_SECRET', 'secret')
    monkeypatch.setenv('HARNESS_API_TOKEN', 't')
    monkeypatch.setenv('FEISHU_ALLOWED_USERS', '')
    monkeypatch.setenv('FEISHU_ALLOW_ANY_USER', '1')
    seen = {}

    class FakeApi:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            seen['closed'] = True

    class FakeGateway:
        def __init__(self, api, transport=None, allowed_users=None):
            seen['allowed'] = allowed_users

        def run(self, app_id, app_secret):
            seen['ran'] = True

        def stop(self):
            seen['stopped'] = True

    monkeypatch.setattr(feishu_gateway, 'GatewayClient', FakeApi)
    monkeypatch.setattr(feishu_gateway, 'LarkTransport', lambda *a, **k: None)
    monkeypatch.setattr(feishu_gateway, 'FeishuGateway', FakeGateway)
    assert feishu_gateway.main() == 0
    assert seen == {'allowed': None, 'ran': True, 'stopped': True,
                    'closed': True}


def test_lark_transport_sends_text(monkeypatch):
    """LarkTransport：成功发送不抛错；失败时带错误码抛 RuntimeError。"""
    import lark_oapi as lark

    from feishu_gateway import LarkTransport

    sent = {}

    class FakeResponse:
        def __init__(self, ok):
            self._ok = ok
        def success(self):
            return self._ok
        code = 99991672
        msg = 'permission denied'

    class FakeMessageApi:
        def create(self, request):
            sent['request'] = request
            return FakeResponse(sent.pop('fail', False) is False)

    class FakeClient:
        def __init__(self):
            self.im = SimpleNamespace(v1=SimpleNamespace(
                message=FakeMessageApi()))

    fake_client = FakeClient()
    monkeypatch.setattr(
        lark.Client, 'builder',
        lambda: SimpleNamespace(
            app_id=lambda aid: SimpleNamespace(
                app_secret=lambda sec: SimpleNamespace(build=lambda: fake_client))))

    transport = LarkTransport('cli_x', 'secret')
    transport.send_text('oc_chat', '你好')   # 成功路径：不抛错

    sent['fail'] = True
    with pytest.raises(RuntimeError, match='feishu send failed'):
        transport.send_text('oc_chat', '再试')


def test_markdown_to_card_md_conversions():
    """lark_md 转换：标题→加粗、水平线→空行、无序列表→•；代码块原样。"""
    from feishu_gateway import markdown_to_card_md
    md = ('# 标题\n## 二级\n- 甲\n* 乙\n---\n'
          '| 状态 | 结果 |\n|---|---|\n| ok | 完成 |\n'
          '```py\n# 注释\n- 保留\n```\n正文')
    out = markdown_to_card_md(md)
    lines = out.splitlines()
    assert lines[0] == '**标题**'
    assert lines[1] == '**二级**'
    assert lines[2] == '• 甲'
    assert lines[3] == '• 乙'
    assert lines[4] == ''          # 水平线
    assert '状态: ok；结果: 完成' in out
    assert '# 注释' in out          # 代码块内不转换
    assert '- 保留' in out
    assert lines[-1] == '正文'
    # 截断保护发生在 send_markdown 层
    from feishu_gateway import CARD_MD_LIMIT
    huge = markdown_to_card_md('x' * (CARD_MD_LIMIT + 500))
    content = huge
    if len(content) > CARD_MD_LIMIT:
        content = content[:CARD_MD_LIMIT] + '\n…[内容过长已截断]'
    assert len(content) <= CARD_MD_LIMIT + 20
    assert content.endswith('…[内容过长已截断]')


def test_markdown_card_uses_lark_md_element():
    """飞书旧版交互卡片必须把 Markdown 放在 div.text/lark_md 中。"""
    import json

    from feishu_gateway import LarkTransport

    card = json.loads(LarkTransport._markdown_card('**加粗**\n- 项目'))
    element = card['elements'][0]
    assert element['tag'] == 'div'
    assert element['text']['tag'] == 'lark_md'
    assert element['text']['content'] == '**加粗**\n• 项目'


def test_reply_prefers_card_and_falls_back_to_text():
    """_reply：有 send_markdown 的 transport 优先卡片；卡片抛错回退 text。"""
    api = gateway_sim.GatewayClient(
        'http://test', 't', transport=_sync_asgi_transport(api_app))
    fake = FakeTransport()
    gw = feishu_gateway.FeishuGateway(api, transport=fake)

    class CardTransport(FakeTransport):
        def send_markdown(self, chat_id, text):
            self.cards.append((chat_id, text))

        def send_text(self, chat_id, text):
            self.texts.append((chat_id, text))

    ok = CardTransport()
    ok.cards = []
    ok.texts = []
    ok.send_markdown('c', 'x')  # 预热属性存在
    ok.cards.clear()
    ok.texts.clear()
    gw.transport = ok
    gw._reply('c1', '渲染**加粗**')
    assert ok.cards and not ok.texts

    class BrokenCardTransport(CardTransport):
        def send_markdown(self, chat_id, text):
            raise RuntimeError('card failed')

    bad = BrokenCardTransport()
    bad.cards = []
    bad.texts = []
    gw.transport = bad
    gw._reply('c1', '回退文本')
    assert not bad.cards and bad.texts


def test_stream_reply_uses_one_markdown_card_and_finalizes(monkeypatch):
    """流式回复创建并更新同一张卡片，最终结果只保留一条消息。"""
    import perf

    monkeypatch.setenv('HARNESS_PERF', '1')
    perf.record('previous_turn', 1)
    class StreamApi:
        def send_stream(self, content, session_id=None, on_delta=None, on_progress=None):
            on_delta('**第一段**')
            on_delta('\n- 第二段')
            return '**第一段**\n- 第二段'

    class CardTransport:
        def __init__(self):
            self.created = []
            self.updated = []
            self.texts = []

        def send_markdown(self, chat_id, text):
            self.created.append((chat_id, text))
            return 'msg-1'

        def update_markdown(self, message_id, text):
            self.updated.append((message_id, text))

        def send_text(self, chat_id, text):
            self.texts.append((chat_id, text))

    transport = CardTransport()
    gateway = FeishuGateway(StreamApi(), transport=transport)
    gateway._send_agent_reply('chat-1', 'session-1', 'hello')
    assert transport.created == [('chat-1', '**第一段**')]
    # 建卡即首帧；最终回复与最后一张部分更新相同 → 不再重复 PATCH
    assert transport.updated == [
        ('msg-1', '**第一段**\n- 第二段'),
    ]
    assert transport.texts == []
    assert 'previous_turn' not in perf.snapshot()
    assert perf.snapshot()['gw.first_card'][0] == 1
    assert 'gw.final_update' not in perf.snapshot()   # 内容相同不重复更新


def test_stream_card_update_failure_does_not_fail_generation():
    """卡片更新失败时仍完成生成，并补发纯文本结果。"""
    class StreamApi:
        def send_stream(self, content, session_id=None, on_delta=None, on_progress=None):
            on_delta('partial')
            return 'final answer'

    class BrokenTransport:
        def __init__(self):
            self.texts = []

        def send_markdown(self, chat_id, text):
            return 'msg-1'

        def update_markdown(self, message_id, text):
            raise RuntimeError('temporary failure')

        def send_text(self, chat_id, text):
            self.texts.append((chat_id, text))

    transport = BrokenTransport()
    gateway = FeishuGateway(StreamApi(), transport=transport)
    gateway._send_agent_reply('chat-1', 'session-1', 'hello')
    assert transport.texts == [('chat-1', 'final answer')]


def test_use_command_switches_binding(api_app, tmp_path):
    """/use <前缀>：唯一命中→重绑；歧义→候选列表；未命中→报错。"""
    api = gateway_sim.GatewayClient(
        'http://test', 't', transport=_sync_asgi_transport(api_app))
    fake = FakeTransport()
    gw = feishu_gateway.FeishuGateway(api, transport=fake,
                                      bindings_path=tmp_path / 'bindings.json')
    sid_a = api.create_session('dup-1')
    sid_b = api.create_session('dup-2')
    sid_c = api.create_session()          # 自动 ID，构造唯一前缀用
    fake.sent.clear()

    # 空参数 → 用法提示
    gw.on_message('c1', '/use')
    assert any('用法' in text for _, text in fake.sent)

    # 未命中
    gw.on_message('c1', '/use zzzz')
    assert any('未找到' in text for _, text in fake.sent)

    # 歧义：dup- 前缀同时命中两个
    gw.on_message('c1', '/use dup-')
    assert any('歧义' in text for _, text in fake.sent)

    # 唯一前缀命中 → 重绑到 sid_c
    prefix = next(sid_c[:k] for k in range(1, len(sid_c) + 1)
                  if not sid_a.startswith(sid_c[:k])
                  and not sid_b.startswith(sid_c[:k]))
    gw.on_message('c1', f'/use {prefix}')
    assert any('已切换到会话' in text for _, text in fake.sent)
    assert gw.bindings['c1'] == sid_c
    api.close()


def test_user_allowlist_blocks_strangers(api_app, tmp_path, monkeypatch):
    """白名单：名单外用户的消息被静默忽略（不建会话、不执行）。"""
    monkeypatch.setattr(permission_gate, 'backend', 'remote')
    api = gateway_sim.GatewayClient(
        'http://test', 't', transport=_sync_asgi_transport(api_app))
    fake = FakeTransport()
    gw = FeishuGateway(api, transport=fake,
                       bindings_path=tmp_path / 'bindings.json',
                       allowed_users={'ou_me'})
    try:
        gw.on_message('c1', 'hello', sender_id='ou_stranger')
        assert gw.bindings == {}          # 没有为陌生人建会话
        # 不再静默：回一条带 open_id 的提示，方便直接配置白名单
        assert 'FEISHU_ALLOWED_USERS=ou_stranger' in fake.last()

        gw.on_message('c1', 'hello', sender_id='ou_me')
        assert 'agent-reply-ok' in fake.last()

        # 陌生人无法通过 y/n 决议别人的审批（也没有他的 FIFO）
        fake.sent.clear()
        gw.on_message('c1', 'y', sender_id='ou_stranger')
        assert all('FEISHU_ALLOWED_USERS' in text for _, text in fake.sent)
    finally:
        api.close()


def test_cd_switches_task_directory(gateway, tmp_path):
    """/cd 创建或切换会话工作区，/run 自动携带。"""
    gw, fake = gateway
    workdir = tmp_path / 'projA'
    workdir.mkdir()

    gw.on_message('c1', f'/cd {workdir}')
    assert str(workdir) in fake.last()
    assert gw.api.get_workspace(gw.bindings['c1']) == str(workdir)

    gw.on_message('c1', '/run echo from-proj-a')
    assert '任务已创建' in fake.last()
    task_id = gw.api.list_tasks()[0]['id']
    for _ in range(100):
        if gw.api.get_task(task_id)['status'] in ('succeeded', 'failed'):
            break
        time.sleep(0.05)
    assert gw.api.get_task(task_id)['cwd'] == str(workdir)
    assert 'from-proj-a' in gw.api.task_logs(task_id)


def test_api_permission_endpoints_flow(api_app):
    """HTTP 层契约：pending 列表 → resolve 200 → 重复 resolve 409 → 未知 404。"""
    from fastapi.testclient import TestClient

    monkey_backend = permission_gate.backend
    permission_gate.backend = 'remote'
    _drain_stale_permissions()
    try:
        client = TestClient(api_app)
        headers = {'Authorization': 'Bearer t'}
        results = {}

        def waiter():
            results['allowed'] = permission_gate.decide(
                'bash', {'command': 'x'}, 'destructive')[0]

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        for _ in range(50):
            if permission_gate.pending():
                break
            time.sleep(0.02)
        req_id = permission_gate.pending()[0].req_id

        listing = client.get('/v1/permissions', headers=headers)
        assert listing.status_code == 200
        assert listing.json()['pending'][0]['req_id'] == req_id

        resolved = client.post(f'/v1/permissions/{req_id}/resolve',
                               json={'allow': True}, headers=headers)
        assert resolved.status_code == 200
        assert resolved.json()['request']['status'] == 'approved'
        thread.join(timeout=2)
        assert results['allowed'] is True

        assert client.post(f'/v1/permissions/{req_id}/resolve',
                           json={'allow': False}, headers=headers).status_code == 409
        assert client.post('/v1/permissions/perm_missing/resolve',
                           json={'allow': True}, headers=headers).status_code == 404
    finally:
        permission_gate.backend = monkey_backend


# ---------- /sandbox：档位读写（转发到服务端 /v1/policy） ----------

def test_gateway_client_policy_round_trip(api_app):
    """网关客户端 ↔ HTTP 端点的真实链路（飞书 /sandbox 走的就是这两个方法）。"""
    api = gateway_sim.GatewayClient('http://test', 't',
                                    transport=_sync_asgi_transport(api_app))
    try:
        assert api.get_policy() == {'sandbox_mode': 'workspace-write',
                                    'approval_policy': 'on-request'}
        assert api.set_policy(sandbox_mode='read-only')['sandbox_mode'] == 'read-only'
        assert api.get_policy()['sandbox_mode'] == 'read-only'
    finally:
        api.close()


def test_sandbox_command_reports_tiers_and_errors(gateway, monkeypatch):
    gw, fake = gateway

    class PolicyAPI:
        def __init__(self):
            self.policy = {'sandbox_mode': 'workspace-write',
                           'approval_policy': 'on-request'}
            self.calls = []

        def get_policy(self):
            self.calls.append(('get',))
            return dict(self.policy)

        def set_policy(self, **kwargs):
            self.calls.append(('set', kwargs))
            self.policy.update(kwargs)
            return dict(self.policy)

    api = PolicyAPI()
    monkeypatch.setattr(gw, 'api', api)

    gw.on_message('c1', '/sandbox')
    assert 'workspace-write' in fake.last() and 'OS 级沙箱' in fake.last()
    gw.on_message('c1', '/sandbox read-only')
    assert 'read-only' in fake.last()
    gw.on_message('c1', '/sandbox approvals never')
    assert 'never' in fake.last()
    assert api.calls == [('get',), ('set', {'sandbox_mode': 'read-only'}),
                         ('set', {'approval_policy': 'never'})]

    # 形状不对：本地拦下，不发请求
    gw.on_message('c1', '/sandbox approvals')
    assert '用法' in fake.last()
    assert len(api.calls) == 3

    class BrokenAPI(PolicyAPI):
        def set_policy(self, **kwargs):
            raise RuntimeError("unknown HARNESS_SANDBOX_MODE 'nope'; "
                               'choose one of read-only, workspace-write, danger-full-access')

    monkeypatch.setattr(gw, 'api', BrokenAPI())
    gw.on_message('c1', '/sandbox nope')
    assert '[sandbox] unknown' in fake.last(), '服务端的 400 说明要转给用户'


class _WorkspaceCardTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.cards = []

    def send_workspace_card(self, chat_id, card):
        self.cards.append((chat_id, card))


def _workspace_values(card):
    return [element['actions'][0]['value'] for element in card['elements']
            if element.get('tag') == 'action']


def test_new_lists_the_default_workspace_before_any_session(gateway, tmp_path):
    gw, _ = gateway
    gw.transport = _WorkspaceCardTransport()

    gw.on_message('c1', '/new')

    assert not gw.bindings, '/new without a choice must not create a session'
    assert gw.transport.cards
    card = gw.transport.cards[-1][1]
    assert '选择工作区' in card['header']['title']['content']
    texts = [element['text']['content'] for element in card['elements']
             if element.get('tag') == 'div']
    assert any(str(tmp_path) in text for text in texts)
    values = _workspace_values(card)
    assert len(values) == 1
    assert values[0]['index'] == 0 and values[0]['workspace_view']


def test_workspace_card_button_creates_a_session_in_that_directory(gateway, tmp_path):
    gw, _ = gateway
    gw.transport = _WorkspaceCardTransport()
    gw.on_message('c1', '/new')
    value = _workspace_values(gw.transport.cards[-1][1])[0]

    gw._handle_workspace_action('c1', value, 'ou_me')

    assert gw.api.get_workspace(gw.bindings['c1']) == str(tmp_path.resolve())
    assert '已创建新会话' in gw.transport.last()
    assert 'c1' not in gw._workspace_views      # 用掉的快照立即失效


def test_expired_workspace_view_rejects_numbers_and_buttons(gateway, monkeypatch):
    gw, fake = gateway
    gw.on_message('c1', '/new')
    token = gw._workspace_views['c1'][0]
    expires = (gw._workspace_views['c1'][2]
               + feishu_gateway.WORKSPACE_VIEW_TTL + 1)
    monkeypatch.setattr(feishu_gateway.time, 'monotonic', lambda: expires)

    gw.on_message('c1', '/new 1')
    assert '已失效' in fake.last()
    assert not gw.bindings

    text, ok = gw.handle_card_action(
        'ou_me', {'workspace_view': token, 'index': 0}, 'om_1', chat_id='c1')
    assert not ok and '已失效' in text


def test_workspace_card_rejects_bad_tokens_and_indexes(gateway):
    gw, _ = gateway
    gw.on_message('c1', '/new')
    token = gw._workspace_views['c1'][0]

    text, ok = gw.handle_card_action(
        'ou_me', {'workspace_view': 'stale', 'index': 0}, 'om_1', chat_id='c1')
    assert not ok and '已失效' in text
    text, ok = gw.handle_card_action(
        'ou_me', {'workspace_view': token, 'index': 99}, 'om_1', chat_id='c1')
    assert not ok and '无效' in text
    assert not gw.bindings


def test_workspace_card_refuses_an_unauthorized_entry(gateway, tmp_path):
    import workspaces

    gw, _ = gateway
    workspaces.add_workspace(tmp_path.parent, name='Parent',
                             file=tmp_path / 'workspaces.json')
    gw.transport = _WorkspaceCardTransport()
    gw.on_message('c1', '/new')
    values = _workspace_values(gw.transport.cards[-1][1])
    assert len(values) == 2

    gw._handle_workspace_action('c1', values[1], 'ou_me')

    assert '未授权' in gw.transport.last()
    assert not gw.bindings


def test_new_accepts_ws_name_relative_path_and_id(gateway, tmp_path):
    gw, _ = gateway
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'sub').mkdir()

    gw.on_message('c1', f'/workspace add "{project}" --name proj')
    gw.on_message('c1', '/new ws:proj')
    assert gw.api.get_workspace(gw.bindings['c1']) == str(project.resolve())

    gw.on_message('c1', '/new id:named-session')
    assert gw.bindings['c1'] == 'named-session'
    assert gw.api.get_workspace('named-session') == str(project.resolve())

    gw.on_message('c1', '/new ./sub')
    assert gw.api.get_workspace(gw.bindings['c1']) == str((project / 'sub').resolve())


def test_feishu_workspace_commands_add_list_and_remove(gateway, tmp_path):
    gw, fake = gateway
    project = tmp_path / 'project'
    project.mkdir()

    gw.on_message('c1', f'/workspace add "{project}" --name proj')
    assert '已登记' in fake.last()
    gw.on_message('c1', '/workspaces')
    assert 'proj' in fake.last() and str(project.resolve()) in fake.last()
    gw.on_message('c1', '/workspace remove proj')
    assert '已移除' in fake.last()
    gw.on_message('c1', '/workspaces')
    assert '尚未登记' in fake.last()


def test_switching_workspace_invalidates_the_pending_selection(gateway):
    gw, _ = gateway
    gw.on_message('c1', '/new')
    assert 'c1' in gw._workspace_views

    gw.on_message('c1', '/cd .')

    assert 'c1' not in gw._workspace_views

    gw.on_message('c1', '/new')
    assert 'c1' in gw._workspace_views
    gw.on_message('c1', '/quit')
    assert 'c1' not in gw._workspace_views


def test_workspaces_lists_all_registered_entries_beyond_the_picker_limit(
        gateway, tmp_path):
    gw, fake = gateway
    for index in range(8):
        directory = tmp_path / f'ws{index}'
        directory.mkdir()
        gw.api.add_workspace(str(directory), f'ws{index}')

    gw.on_message('c1', '/workspaces')
    text = fake.last()
    for index in range(8):
        assert f'ws{index}' in text

    gw.on_message('c1', '/workspace remove ws0')
    assert '已移除' in fake.last()


def test_watcher_pushes_turn_terminal_from_other_sources(gateway, tmp_path):
    """TUI/API 跑完的回合 → watcher 通过 SSE 增量发现并主动发卡。"""
    import time

    from session_store import SessionEventStore

    gw, fake = gateway
    gw.on_message('chat-watch', '帮我看看')
    session_id = gw.bindings['chat-watch']

    store = SessionEventStore.open(
        tmp_path / 'sessions' / f'session_{session_id}.jsonl')
    store.append('turn_started', turn_id='t-watch', source='tui')
    store.append('turn_terminal', turn_id='t-watch', status='completed',
                 source='tui', ts=time.time())

    result = gw.watcher.poll_once(session_id)
    assert result['notified'] == 1
    text = fake.last()
    assert text == '✅ 回合完成', '完成通知不应带会话/turn/来源'

    # 同一个终态再拉一次（SSE 重放）不会重复推送
    assert gw.watcher.poll_once(session_id)['notified'] == 0
    assert fake.all_text().count('回合完成') == 1


def test_watcher_skips_feishu_turns_and_reports_failures(gateway, tmp_path):
    import time

    from session_store import SessionEventStore

    gw, fake = gateway
    gw.on_message('chat-skip', 'hi')
    session_id = gw.bindings['chat-skip']
    store = SessionEventStore.open(
        tmp_path / 'sessions' / f'session_{session_id}.jsonl')

    store.append('turn_started', turn_id='t-feishu', source='feishu')
    store.append('turn_terminal', turn_id='t-feishu', status='completed',
                 source='feishu', ts=time.time())
    assert gw.watcher.poll_once(session_id)['notified'] == 0

    store.append('turn_started', turn_id='t-fail', source='cron')
    store.append('turn_terminal', turn_id='t-fail', status='failed',
                 source='cron', error_type='APIStatusError', ts=time.time())
    assert gw.watcher.poll_once(session_id)['notified'] == 1
    assert '回合失败' in fake.last() and 'APIStatusError' in fake.last()
    assert 'turn' not in fake.last() and '来源' not in fake.last()


def test_watcher_pushes_approval_timeout_distinctly(gateway, tmp_path):
    """审批超时通知要和「用户拒绝」「回合失败」区分开，且只推一次。"""
    from session_store import SessionEventStore

    gw, fake = gateway
    gw.on_message('chat-approval-timeout', 'hi')
    session_id = gw.bindings['chat-approval-timeout']
    store = SessionEventStore.open(
        tmp_path / 'sessions' / f'session_{session_id}.jsonl')
    store.append('approval_timeout', request_id='perm_timeout', tool='bash',
                 reason='destructive command', timeout_s=300.0)

    assert gw.watcher.poll_once(session_id)['notified'] == 1
    text = fake.last()
    assert '审批超时' in text and 'bash' in text and '无人应答' in text
    assert '回合失败' not in text

    assert gw.watcher.poll_once(session_id)['notified'] == 0


def test_heartbeat_summary_is_delivered_and_acked(gateway, tmp_path, monkeypatch):
    """网关领走 outbox 摘要 → 推给绑定聊天 → ack；重复轮询不再推。"""
    import heartbeat

    monkeypatch.setattr(heartbeat, 'OUTBOX_PATH', tmp_path / 'outbox.json')
    gw, fake = gateway
    gw.on_message('chat-heartbeat', 'hi')
    heartbeat.queue_summary('heartbeat:2026-10-25T09:00', '🫀 每日摘要 · 测试')

    assert gw.poll_heartbeat() == 1
    assert '每日摘要' in fake.last()
    assert heartbeat.pending() == []
    assert gw.poll_heartbeat() == 0


def test_heartbeat_stays_pending_without_a_bound_chat(gateway, tmp_path, monkeypatch):
    import heartbeat

    monkeypatch.setattr(heartbeat, 'OUTBOX_PATH', tmp_path / 'outbox.json')
    gw, _fake = gateway
    heartbeat.queue_summary('heartbeat:2026-10-25T09:00', '🫀 每日摘要 · 无人接收')

    assert gw.poll_heartbeat() == 0
    assert [item['occurrence_id'] for item in heartbeat.pending()] \
        == ['heartbeat:2026-10-25T09:00']


def test_cron_commands_list_add_and_delete(gateway, tmp_path, monkeypatch):
    import cron_scheduler

    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    gw, fake = gateway

    gw.on_message('chat-cron', '/cron')
    assert '没有定时任务' in fake.last_card()

    gw.on_message('chat-cron', '/cron add "0 9 * * *" 每日汇报')
    created = fake.last()
    assert '已创建 cron_' in created and '每日汇报' in created
    job_id = created.split('已创建 ', 1)[1].split(':', 1)[0]

    gw.on_message('chat-cron', '/cron list')
    assert job_id in fake.last_card() and '每日汇报' in fake.last_card()

    gw.on_message('chat-cron', '/cron add "* * *" 缺少字段')
    assert 'Expected 5 fields' in fake.last()

    gw.on_message('chat-cron', f'/cron delete {job_id}')
    assert '已删除' in fake.last()
    gw.on_message('chat-cron', '/cron list')
    assert '没有定时任务' in fake.last_card()


def test_cron_command_usage_and_unknown_job(gateway):
    gw, fake = gateway
    gw.on_message('chat-cron2', '/cron add')
    assert '用法' in fake.last()
    gw.on_message('chat-cron2', '/cron delete missing-id')
    assert 'not found' in fake.last() or '404' in fake.last()


def test_cron_add_is_read_only_unless_writes_flag(gateway, tmp_path, monkeypatch):
    import cron_scheduler

    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    gw, fake = gateway

    gw.on_message('chat-cron-w', '/cron add "0 9 * * *" 只读汇报')
    assert '只读' in fake.last()

    gw.on_message('chat-cron-w', '/cron add --writes "0 10 * * *" 修改文件')
    assert '可写' in fake.last() and '修改文件' in fake.last()

    gw.on_message('chat-cron-w', '/cron list')
    listing = fake.last_card()
    assert 'recurring · 只读 · 推送本聊天' in listing
    assert 'recurring · 可写（副作用仍审批） · 推送本聊天' in listing
    assert '允许写操作' in listing and '收回写权限' in listing

    # 只有开关没有表达式：退回用法，不创建任务
    gw.on_message('chat-cron-w', '/cron add --writes')
    assert '用法' in fake.last()


def test_gateway_pushes_cron_results_only_to_bound_chats(gateway, tmp_path,
                                                        monkeypatch):
    import cron_delivery

    monkeypatch.setattr(cron_delivery, 'OUTBOX_PATH', tmp_path / 'outbox.json')
    gw, fake = gateway

    gw.on_message('chat-result', 'hi')   # 绑定会话
    fake.sent.clear()
    cron_delivery.queue('chat-result', '⏰ 定时结果', job_id='j1',
                        occurrence='o1')
    cron_delivery.queue('chat-unknown', '不该发出', job_id='j2',
                        occurrence='o2')

    assert gw.poll_cron_results() == 1
    assert ('chat-result', '⏰ 定时结果') in fake.sent
    assert all('不该发出' not in text for _, text in fake.sent)
    # 未绑定的目标保留 pending，等绑定恢复再补发
    assert [item['chat_id'] for item in cron_delivery.pending()] == ['chat-unknown']


def _last_card_text(fake) -> str:
    """把最近一张卡片拍平成字符串，便于断言内容（卡片是结构化 JSON）。"""
    import json

    return json.dumps(fake.cards[-1][1], ensure_ascii=False) if fake.cards else ''


def test_startup_announces_control_panel_to_bound_chats(gateway):
    gw, fake = gateway
    gw.on_message('chat-boot-1', 'hi')
    gw.on_message('chat-boot-2', 'hi')
    fake.cards.clear()
    assert gw.announce_startup() == 2
    assert len(fake.cards) == 2
    assert all(card['header']['title']['content'] == 'Harness 控制台'
               for _, card in fake.cards)


def test_panel_menu_action_opens_control_panel(gateway):
    gw, fake = gateway
    gw.on_message('chat-menu', 'hi')
    fake.cards.clear()
    toast, ok = gw.handle_card_action('ou_me', {'panel': 'menu'}, 'om_m',
                                      chat_id='chat-menu')
    assert ok
    gw._handle_panel_action('chat-menu', 'menu')
    assert fake.cards and fake.cards[-1][1]['header']['title']['content'] == 'Harness 控制台'


def _panel_actions(card: dict) -> list[str]:
    actions = []
    for element in card.get('elements', []):
        for button in element.get('actions', []) or []:
            value = button.get('value') or {}
            if value.get('panel'):
                actions.append(value['panel'])
    return actions


def test_first_contact_sends_control_panel_without_commands(gateway, tmp_path):
    gw, fake = gateway
    gw.on_message('chat-panel', '你好')
    assert gw.bindings['chat-panel']
    assert fake.cards, '首次私聊应发控制台卡片'
    card = fake.cards[-1][1]
    assert card['header']['title']['content'] == 'Harness 控制台'
    assert {'new', 'sessions', 'stop', 'status', 'help'} <= set(_panel_actions(card))
    # 卡片里不应该要求用户输入斜杠命令
    text = '\n'.join(item.get('text', {}).get('content', '')
                     for item in card['elements'] if item.get('tag') == 'div')
    assert '/new' not in text


def test_unauthorized_user_gets_their_open_id(gateway):
    gw, fake = gateway
    gw.allowed_users = {'ou_boss'}
    gw.on_message('chat-stranger', 'hi', sender_id='ou_stranger')
    assert 'chat-stranger' not in gw.bindings
    assert 'FEISHU_ALLOWED_USERS=ou_stranger' in fake.last()


def test_panel_buttons_drive_new_status_and_stop(gateway):
    gw, fake = gateway
    gw.on_message('chat-panel2', 'hi')
    gw.start_dispatcher(workers=1)
    try:
        toast, ok = gw.handle_card_action('ou_me', {'panel': 'new'}, 'om_1',
                                          chat_id='chat-panel2')
        assert ok and '打开' in toast
        for _ in range(100):
            time.sleep(0.01)
            if any(item.get('tag') == 'action' for item in
                   (fake.cards[-1][1].get('elements') if fake.cards else [])):
                break

        toast, ok = gw.handle_card_action('ou_me', {'panel': 'status'}, 'om_2',
                                          chat_id='chat-panel2')
        assert ok
        for _ in range(100):
            time.sleep(0.01)
            if '会话 ' in fake.last():
                break
        assert gw.bindings['chat-panel2'][:8] in fake.last()

        toast, ok = gw.handle_card_action('ou_me', {'panel': 'stop'}, 'om_3',
                                          chat_id='chat-panel2')
        assert ok
        for _ in range(100):
            time.sleep(0.01)
            if '回合' in fake.last():
                break
        assert '没有正在执行的回合' in fake.last() or '取消' in fake.last()
    finally:
        gw.stop()


def test_panel_help_falls_back_to_text_without_card_support(tmp_path):
    class TextOnly:
        def __init__(self):
            self.sent = []

        def send_text(self, chat_id, text):
            self.sent.append((chat_id, text))

    class API:
        def create_session(self):
            return 's1'

        def get_workspace(self, session_id):
            return str(tmp_path)

        def send(self, text, session_id=None):
            return 'ok'

    gw = FeishuGateway(API(), transport=TextOnly(),
                       bindings_path=tmp_path / 'bindings.json')
    gw.on_message('c', 'hi')
    assert any('直接发消息即可' in text for _, text in gw.transport.sent)


def test_check_setup_reports_missing_and_accepts_allow_any(monkeypatch):
    from feishu_gateway import check_setup

    monkeypatch.delenv('FEISHU_APP_ID', raising=False)
    monkeypatch.delenv('FEISHU_APP_SECRET', raising=False)
    monkeypatch.delenv('HARNESS_API_TOKEN', raising=False)
    monkeypatch.delenv('FEISHU_ALLOWED_USERS', raising=False)
    monkeypatch.delenv('FEISHU_ALLOW_ANY_USER', raising=False)
    problems = check_setup()
    assert len(problems) == 4
    env = {'FEISHU_APP_ID': 'a', 'FEISHU_APP_SECRET': 'b',
           'HARNESS_API_TOKEN': 'c', 'FEISHU_ALLOWED_USERS': 'ou_x'}
    assert check_setup(env) == []
    env.pop('FEISHU_ALLOWED_USERS')
    env['FEISHU_ALLOW_ANY_USER'] = '1'
    assert check_setup(env) == []


def test_check_mode_exits_without_connecting(monkeypatch, capsys):
    import dotenv

    import feishu_gateway

    monkeypatch.setattr(dotenv, 'load_dotenv', lambda *a, **k: None)
    for key in ('FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'HARNESS_API_TOKEN',
                'FEISHU_ALLOWED_USERS', 'FEISHU_ALLOW_ANY_USER'):
        monkeypatch.delenv(key, raising=False)
    assert feishu_gateway.main(['--check']) == 1
    out = capsys.readouterr().out
    assert 'FEISHU_APP_ID' in out and '飞书接入步骤' in out


def test_stale_binding_is_unbound_and_warns_once(gateway):
    gw, fake = gateway
    gw.bindings['chat-stale'] = 'missing-session'
    gw._watch_missing('missing-session')
    assert 'chat-stale' not in gw.bindings
    assert '已解除绑定' in fake.last()


def test_send_to_stale_session_rebinds_and_retries(api_app, tmp_path, monkeypatch):
    """老绑定指向已删除会话时，发消息应自动新建会话并继续，而不是报 404。"""
    monkeypatch.setattr(permission_gate, 'backend', 'remote')
    calls = {'n': 0}

    class NotFound(Exception):
        class response:
            status_code = 404

    class API(gateway_sim.GatewayClient):
        def send_stream(self, content, session_id=None, on_delta=None,
                        on_progress=None):
            calls['n'] += 1
            if calls['n'] == 1:
                raise NotFound()
            if on_delta:
                on_delta('ok')
            return 'ok'

    api = API('http://test', 't', transport=_sync_asgi_transport(api_app))
    fake = FakeTransport()
    gw = FeishuGateway(api, transport=fake,
                       bindings_path=tmp_path / 'bindings.json')
    try:
        old = gw.api.create_session()
        gw.bindings['chat-retry'] = old
        # 把会话文件删掉，模拟“绑定指向已删除会话”
        (tmp_path / 'sessions' / f'session_{old}.jsonl').unlink()
        gw.on_message('chat-retry', '继续')
        assert gw.bindings['chat-retry'] != old
        assert '已新建会话' in fake.all_text()
        assert 'ok' in fake.all_text()
    finally:
        api.close()
        monkeypatch.setattr(permission_gate, 'backend', 'console')


def test_missing_session_error_detection():
    from feishu_gateway import _is_missing_session

    class Response:
        status_code = 404

    class HTTPError(Exception):
        response = Response()

    assert _is_missing_session(FileNotFoundError('x'))
    assert _is_missing_session(HTTPError())
    assert not _is_missing_session(RuntimeError('boom'))


class AttachmentTransport(FakeTransport):
    """带附件下载能力的传输层替身。"""

    def __init__(self, image=b'', file=b''):
        super().__init__()
        self.image = image
        self.file = file
        self.downloaded = []

    def download_message_resource(self, message_id, key, kind):
        self.downloaded.append((message_id, key, kind))
        return self.image if kind == 'image' else self.file


class AttachmentAPI:
    def __init__(self, workspace):
        self.workspace = workspace
        self.sent = []

    def create_session(self):
        return 'att-session'

    def get_workspace(self, session_id):
        return str(self.workspace)

    def send(self, text, session_id=None):
        self.sent.append((session_id, text))
        return 'ok'

    def send_stream(self, text, session_id=None, on_delta=None, on_progress=None):
        self.sent.append((session_id, text))
        if on_delta:
            on_delta('ok')
        return 'ok'


def test_image_attachment_saved_to_workspace_with_view_image_hint(tmp_path):
    png = bytes([0x89]) + b'PNG' + b'\x00' * 20
    transport = AttachmentTransport(image=png)
    api = AttachmentAPI(tmp_path)
    gw = FeishuGateway(api, transport=transport,
                       bindings_path=tmp_path / 'bindings.json')
    gw._handle_attachment('chat-img', {'kind': 'image', 'key': 'img_1'},
                          sender_id='ou_me', chat_type='p2p', message_id='om_1')

    inbox = tmp_path / '.cache' / 'feishu-inbox'
    files = list(inbox.glob('*.png'))
    assert len(files) == 1
    assert files[0].read_bytes() == png
    hint = api.sent[-1][1]
    assert 'view_image' in hint and str(files[0]) in hint
    assert gw.bindings['chat-img'] == 'att-session'
    assert transport.downloaded == [('om_1', 'img_1', 'image')]


def test_document_attachment_hint_points_at_read_document(tmp_path):
    transport = AttachmentTransport(file=b'%PDF-1.4 fake')
    api = AttachmentAPI(tmp_path)
    gw = FeishuGateway(api, transport=transport,
                       bindings_path=tmp_path / 'bindings.json')
    gw._handle_attachment('chat-doc', {'kind': 'file', 'key': 'file_1',
                                       'name': '季度报告.pdf'},
                          sender_id='ou_me', chat_type='p2p', message_id='om_2')
    saved = list((tmp_path / '.cache' / 'feishu-inbox').glob('*.pdf'))
    assert saved and saved[0].name.endswith('季度报告.pdf')
    hint = api.sent[-1][1]
    assert 'read_document' in hint and '季度报告.pdf' in hint
    assert str(saved[0]) in hint


def test_oversized_attachment_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('FEISHU_MAX_ATTACHMENT_MB', '1')
    transport = AttachmentTransport(file=b'x' * (2 * 1024 * 1024))
    api = AttachmentAPI(tmp_path)
    gw = FeishuGateway(api, transport=transport,
                       bindings_path=tmp_path / 'bindings.json')
    gw._handle_attachment('chat-big', {'kind': 'file', 'key': 'f', 'name': 'big.bin'},
                          sender_id='ou_me', chat_type='p2p', message_id='om_3')
    assert '超过' in transport.last()
    assert not list((tmp_path / '.cache' / 'feishu-inbox').glob('*'))


def test_unsupported_message_type_gets_hint(tmp_path):
    fake = FakeTransport()
    gw = FeishuGateway(AttachmentAPI(tmp_path), transport=fake,
                       bindings_path=tmp_path / 'bindings.json')
    gw._unsupported_message('chat-audio', 'audio')
    assert '暂不支持 audio' in fake.last() and '图片、文件' in fake.last()


def test_lark_transport_downloads_via_message_resource_endpoint():
    """消息里的图片/文件必须走 messages/{id}/resources；用 images 接口会 234001。"""
    import io
    from types import SimpleNamespace

    from feishu_gateway import LarkTransport

    captured = {}

    class Resource:
        def get(self, request):
            captured['request'] = request
            return SimpleNamespace(success=lambda: True,
                                   file=io.BytesIO(b'image-bytes'))

    transport = LarkTransport.__new__(LarkTransport)
    transport._client = SimpleNamespace(im=SimpleNamespace(
        v1=SimpleNamespace(message_resource=Resource())))
    data = transport.download_message_resource('om_1', 'img_1', 'image')

    assert data == b'image-bytes'
    request = captured['request']
    assert request.message_id == 'om_1'
    assert request.file_key == 'img_1'
    assert request.type == 'image'


def test_parse_post_content_text_links_and_images():
    import json

    from feishu_gateway import parse_post_content

    raw = json.dumps({'title': '标题', 'content': [
        [{'tag': 'text', 'text': '看这张图 '},
         {'tag': 'img', 'image_key': 'img_1'},
         {'tag': 'a', 'text': '文档', 'href': 'https://example.com'}],
    ]})
    text, images = parse_post_content(raw)
    assert '标题' in text and '看这张图' in text
    assert '文档 (https://example.com)' in text
    assert images == ['img_1']

    wrapped = json.dumps({'zh_cn': {'title': '', 'content': [
        [{'tag': 'text', 'text': '你好'}]]}})
    assert parse_post_content(wrapped) == ('你好', [])
    assert parse_post_content('not json') == ('', [])


def test_post_message_with_images_saves_all_and_hints_view_image(tmp_path):
    png = bytes([0x89]) + b'PNG' + b'\x00' * 10
    transport = AttachmentTransport(image=png)
    api = AttachmentAPI(tmp_path)
    gw = FeishuGateway(api, transport=transport,
                       bindings_path=tmp_path / 'bindings.json')

    gw._handle_attachment('chat-post', {
        'kind': 'post', 'text': '这两张图对比一下',
        'images': ['img_1', 'img_2'],
    }, sender_id='ou_me', chat_type='p2p', message_id='om_post')

    files = sorted((tmp_path / '.cache' / 'feishu-inbox').glob('*.png'))
    assert len(files) == 2
    hint = api.sent[-1][1]
    assert '这两张图对比一下' in hint
    assert all(str(path) in hint for path in files)
    assert 'view_image' in hint
    assert [item[1] for item in transport.downloaded] == ['img_1', 'img_2']


def test_audio_and_media_messages_are_saved_with_explicit_capability_hint(tmp_path):
    transport = AttachmentTransport(file=b'fake-audio-or-video')
    api = AttachmentAPI(tmp_path)
    gw = FeishuGateway(api, transport=transport,
                       bindings_path=tmp_path / 'bindings.json')

    gw._handle_attachment('chat-audio', {
        'kind': 'audio', 'resource_kind': 'file', 'key': 'audio-1',
        'name': 'voice.opus'}, 'ou_me', 'p2p', 'om-audio')
    assert '音频已保存' in api.sent[-1][1]
    assert '不自动转写' in api.sent[-1][1]
    assert list((tmp_path / '.cache' / 'feishu-inbox').glob('*.opus'))

    gw._handle_attachment('chat-video', {
        'kind': 'media', 'resource_kind': 'file', 'key': 'video-1',
        'name': 'clip.mp4'}, 'ou_me', 'p2p', 'om-video')
    assert '视频/媒体文件' in api.sent[-1][1]
    assert '不自动做视频理解' in api.sent[-1][1]
    assert list((tmp_path / '.cache' / 'feishu-inbox').glob('*.mp4'))


def test_file_extension_is_not_restricted_by_gateway(tmp_path):
    transport = AttachmentTransport(file=b'PK\x03\x04not-really-a-doc')
    api = AttachmentAPI(tmp_path)
    gw = FeishuGateway(api, transport=transport,
                       bindings_path=tmp_path / 'bindings.json')
    for name in ('data.csv', 'archive.zip', 'slides.pptx', 'sheet.xlsx'):
        gw._handle_attachment('chat-files', {
            'kind': 'file', 'resource_kind': 'file', 'key': name,
            'name': name}, 'ou_me', 'p2p', 'om-' + name)
    files = list((tmp_path / '.cache' / 'feishu-inbox').iterdir())
    assert {path.suffix for path in files} >= {'.csv', '.zip', '.pptx', '.xlsx'}


def test_cron_card_buttons_authorize_and_revoke_side_effects(gateway, tmp_path,
                                                             monkeypatch):
    import cron_scheduler

    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    gw, fake = gateway
    gw.on_message('chat-cards', '/cron add "0 9 * * *" 每日汇报')
    job_id = fake.last().split('已创建 ', 1)[1].split(':', 1)[0]

    gw.on_message('chat-cards', '/cron list')
    card = fake.cards[-1][1]
    token = _card_token(card, 'writes', job_id)
    assert token
    assert _card_label(card, 'writes', job_id) == '允许写操作'

    ok, allowed = gw.handle_card_action('ou_me', {
        'cron_action': 'writes', 'job_id': job_id, 'cron_view': token},
        None, chat_id='chat-cards')
    assert ok and allowed
    gw._handle_cron_action('chat-cards', {
        'cron_action': 'writes', 'job_id': job_id, 'cron_view': token})
    assert cron_scheduler.scheduled_jobs[job_id].allow_side_effects is True
    assert '已允许写操作' in fake.last()

    gw.on_message('chat-cards', '/cron list')
    card = fake.cards[-1][1]
    token = _card_token(card, 'readonly', job_id)
    gw._handle_cron_action('chat-cards', {
        'cron_action': 'readonly', 'job_id': job_id, 'cron_view': token})
    assert cron_scheduler.scheduled_jobs[job_id].allow_side_effects is False
    assert '已收回写权限' in fake.last()


def _card_buttons(card):
    return [button
            for element in card.get('elements', [])
            if element.get('tag') == 'action'
            for button in element.get('actions', [])]


def _card_token(card, action, job_id):
    for button in _card_buttons(card):
        value = button.get('value') or {}
        if value.get('cron_action') == action and (
                job_id is None or value.get('job_id') == job_id):
            return value.get('cron_view')
    raise AssertionError(f'no {action} button for {job_id}')


def _card_label(card, action, job_id):
    for button in _card_buttons(card):
        value = button.get('value') or {}
        if value.get('cron_action') == action and value.get('job_id') == job_id:
            return button['text']['content']
    raise AssertionError(f'no {action} button for {job_id}')


def test_cron_card_delete_requires_confirmation(gateway, tmp_path, monkeypatch):
    import cron_scheduler

    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    gw, fake = gateway
    gw.on_message('chat-del', '/cron add "0 9 * * *" 每日汇报')
    job_id = fake.last().split('已创建 ', 1)[1].split(':', 1)[0]
    gw.on_message('chat-del', '/cron list')
    token = _card_token(fake.cards[-1][1], 'delete', job_id)

    # 第一次只发确认卡，任务仍在
    gw._handle_cron_action('chat-del', {
        'cron_action': 'delete', 'job_id': job_id, 'cron_view': token})
    assert job_id in cron_scheduler.scheduled_jobs
    assert '确认删除' in fake.last_card()

    confirm_token = _card_token(fake.cards[-1][1], 'delete_confirm', job_id)
    gw._handle_cron_action('chat-del', {
        'cron_action': 'delete_confirm', 'job_id': job_id,
        'cron_view': confirm_token})
    assert job_id not in cron_scheduler.scheduled_jobs
    assert '已删除' in fake.last()

    # 取消按钮不改状态
    gw.on_message('chat-del', '/cron add "0 8 * * *" 早报')
    other = fake.last().split('已创建 ', 1)[1].split(':', 1)[0]
    gw.on_message('chat-del', '/cron list')
    token = _card_token(fake.cards[-1][1], 'delete', other)
    gw._handle_cron_action('chat-del', {
        'cron_action': 'cancel', 'job_id': other, 'cron_view': token})
    assert other in cron_scheduler.scheduled_jobs


def test_cron_card_rejects_stale_token_and_foreign_chat(gateway, tmp_path,
                                                        monkeypatch):
    import cron_scheduler

    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    gw, fake = gateway
    gw.on_message('chat-a', '/cron add "0 9 * * *" 每日汇报')
    job_id = fake.last().split('已创建 ', 1)[1].split(':', 1)[0]
    gw.on_message('chat-a', '/cron list')
    token = _card_token(fake.cards[-1][1], 'writes', job_id)

    # 别的聊天拿到同样的 value 也不能授权
    text, ok = gw.handle_card_action('ou_me', {
        'cron_action': 'writes', 'job_id': job_id, 'cron_view': token},
        None, chat_id='chat-b')
    assert not ok and '过期' in text
    gw._handle_cron_action('chat-b', {
        'cron_action': 'writes', 'job_id': job_id, 'cron_view': token})
    assert cron_scheduler.scheduled_jobs[job_id].allow_side_effects is False

    # 重新渲染后旧 token 失效
    gw.on_message('chat-a', '/cron list')
    gw._handle_cron_action('chat-a', {
        'cron_action': 'writes', 'job_id': job_id, 'cron_view': token})
    assert cron_scheduler.scheduled_jobs[job_id].allow_side_effects is False


def test_gateway_refuses_a_second_instance(tmp_path, monkeypatch):
    lock = tmp_path / 'locks' / 'feishu-gateway.lock'
    monkeypatch.setattr(feishu_gateway, 'INSTANCE_LOCK_PATH', lock)
    monkeypatch.setenv('FEISHU_APP_ID', 'app')
    monkeypatch.setenv('FEISHU_APP_SECRET', 'secret')
    monkeypatch.setenv('HARNESS_API_TOKEN', 't')
    monkeypatch.setenv('FEISHU_ALLOWED_USERS', 'ou_me')
    monkeypatch.delenv('FEISHU_ALLOW_MULTI', raising=False)
    started = []
    monkeypatch.setattr(feishu_gateway, '_run_gateway',
                        lambda **kwargs: started.append(kwargs) or 0)

    # 单实例：第一次启动成功，锁在 main() 结束（进程级 run 返回）后才释放
    assert feishu_gateway.main([]) == 0
    assert len(started) == 1

    # 另一个「进程」持有同一把锁 → 第二个实例拒绝启动，且不建 transport
    with feishu_gateway.exclusive_file_lock(lock, timeout=0):
        assert feishu_gateway.main([]) == 2
        assert len(started) == 1
        # 显式并行（不同应用凭证）时跳过检查
        monkeypatch.setenv('FEISHU_ALLOW_MULTI', '1')
        assert feishu_gateway.main([]) == 0
        assert len(started) == 2


@pytest.mark.parametrize(('raw', 'expected'), [
    ('agent send failed (402): Insufficient Balance', '余额/额度不足'),
    ('HTTP 401 Unauthorized: invalid api key', '鉴权失败'),
    ('403 forbidden', '拒绝访问'),
    ('429 Too Many Requests', '请求过于频繁'),
    ('ReadTimeout: request timed out', '请求超时'),
    ('turn_busy: another turn is running', '上一轮还在执行'),
    ('something odd happened', ''),
])
def test_friendly_error_maps_common_failures(raw, expected):
    hint = feishu_gateway.friendly_error(RuntimeError(raw))
    if expected:
        assert expected in hint
    else:
        assert hint == ''


def test_step_card_running_shows_stop_and_completed_shows_console():
    import feishu_step_card

    running = feishu_step_card.step_card(status='running', note='运行中 · 00:03',
                                         actions='stop')
    labels = [button['text']['content']
              for element in running['body']['elements']
              if element.get('tag') == 'column_set'
              for column in element['columns']
              for button in column['elements']]
    assert any('停止' in label for label in labels)
    value = [column['elements'][0]['behaviors'][0]['value']
             for element in running['body']['elements']
             if element.get('tag') == 'column_set'
             for column in element['columns']][0]
    assert value == {'panel': 'stop'}

    done = feishu_step_card.step_card(answer='ok', status='completed',
                                      actions='menu')
    assert any(element.get('tag') == 'column_set'
               for element in done['body']['elements'])


def test_truncated_note_only_for_long_answers():
    import feishu_step_card

    assert feishu_step_card.truncated_note('short') == ''
    note = feishu_step_card.truncated_note(
        'x' * (feishu_step_card.MAX_ANSWER_CHARS + 1))
    assert '前' in note and '写入文件' in note


def test_failure_card_uses_friendly_hint_and_raw_detail(gateway):
    gw, fake = gateway

    def boom(*args, **kwargs):
        raise RuntimeError('agent send failed (402): Insufficient Balance')

    original = gw.api.send_stream
    gw.api.send_stream = boom
    try:
        gw.on_message('chat-402', 'hi')
    finally:
        gw.api.send_stream = original
    text = fake.all_text()
    assert '余额/额度不足' in text and 'Insufficient Balance' in text


def test_panel_status_shows_pending_and_cron_counts(gateway, tmp_path, monkeypatch):
    import cron_scheduler

    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    gw, fake = gateway
    gw.on_message('chat-status', 'hi')
    gw.on_message('chat-status', '/cron add "0 9 * * *" 每日汇报')
    gw._handle_panel_action('chat-status', 'status')
    text = fake.all_text()
    assert '待审批 0 条' in text
    assert '定时任务 1 个' in text
    assert '直接发消息即可' in text


def test_first_message_welcome_suggests_what_to_say(gateway):
    gw, fake = gateway
    gw.on_message('chat-welcome', 'hi')
    cards = [card for chat, card in fake.cards if chat == 'chat-welcome']
    intro = json.dumps(cards[0], ensure_ascii=False)
    assert '直接说话就行' in intro
    assert '每天早上 9 点提醒我喝水' in intro


def _cron_job(job_id, *, fired='', writes=False):
    return {'id': job_id, 'cron': '0 9 * * *', 'prompt': f'任务{job_id}',
            'recurring': True, 'allow_side_effects': writes,
            'deliver_to': 'oc_x', 'last_fired': fired}


def test_cron_card_sorts_by_last_fired_and_paginates():
    from feishu_cron_card import cron_card, sort_jobs

    jobs = [_cron_job('old', fired='2026-10-01 09:00'),
            _cron_job('never'),
            _cron_job('new', fired='2026-10-25 09:00'),
            _cron_job('mid', fired='2026-10-20 09:00')]
    assert [job['id'] for job in sort_jobs(jobs)] == ['new', 'mid', 'old', 'never']

    first = cron_card(jobs, chat_id='oc_x', token='t')
    text = json.dumps(first, ensure_ascii=False)
    assert '第 1/2 页' in text and '共 4 个任务' in text
    assert '任务new' in text and '“任务never”' not in text
    assert '下一页' in text and '上一页' not in text

    second = cron_card(jobs, chat_id='oc_x', token='t', page=1)
    text = json.dumps(second, ensure_ascii=False)
    assert '第 2/2 页' in text and '任务never' in text
    assert '上一页' in text and '下一页' not in text

    # 页码越界收敛到最后一页，不会出现空白页
    assert '第 2/2 页' in json.dumps(
        cron_card(jobs, chat_id='oc_x', token='t', page=9), ensure_ascii=False)


def test_cron_page_button_navigates(gateway, tmp_path, monkeypatch):
    import cron_scheduler

    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    gw, fake = gateway
    for hour in range(4):
        cron_scheduler.schedule_job(f'0 {hour} * * *', f'任务{hour}',
                                    deliver_to='chat-pages')
    gw.on_message('chat-pages', '/cron')
    token = _card_token(fake.cards[-1][1], 'page', None)
    assert token

    gw._handle_cron_action('chat-pages', {'cron_action': 'page', 'page': 1,
                                          'cron_view': token})
    assert '第 2/2 页' in fake.last_card()
    # 翻页会重渲染，token 随之更新；旧 token 不能再动
    fresh = _card_token(fake.cards[-1][1], 'page', None)
    assert fresh and fresh != token
    gw._handle_cron_action('chat-pages', {'cron_action': 'page', 'page': 0,
                                          'cron_view': token})
    assert '已过期' in fake.last()
    # 非法页码只提示，不改卡片
    gw._handle_cron_action('chat-pages', {'cron_action': 'page', 'page': 'x',
                                          'cron_view': fresh})
    assert '无效的页码' in fake.last()
    # 旧卡片被点不会连带作废新卡片：fresh token 仍然可用
    gw._handle_cron_action('chat-pages', {'cron_action': 'page', 'page': 0,
                                          'cron_view': fresh})
    assert '第 1/2 页' in fake.last_card()


def test_cron_page_patches_existing_card_instead_of_posting(gateway, tmp_path,
                                                            monkeypatch):
    import cron_scheduler

    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    gw, fake = gateway
    for hour in range(4):
        cron_scheduler.schedule_job(f'0 {hour} * * *', f'任务{hour}',
                                    deliver_to='chat-patch')
    patches = []

    def patch(message_id, card):
        patches.append((message_id, card))

    fake.update_step_card = patch
    gw.on_message('chat-patch', '/cron')
    sent_before = len(fake.cards)
    token = _card_token(fake.cards[-1][1], 'page', None)

    gw._handle_cron_action('chat-patch', {'cron_action': 'page', 'page': 1,
                                          'cron_view': token},
                           message_id='om_card_1')
    assert patches and patches[-1][0] == 'om_card_1'
    assert len(fake.cards) == sent_before   # 没有新消息，聊天不刷屏
    assert '第 2/2 页' in json.dumps(patches[-1][1], ensure_ascii=False)

    # 没有 message_id 时退回发送新卡
    token = _card_token(patches[-1][1], 'page', None)
    gw._handle_cron_action('chat-patch', {'cron_action': 'page', 'page': 0,
                                          'cron_view': token})
    assert len(fake.cards) == sent_before + 1


def test_control_card_shows_pending_and_cron_counts(gateway, tmp_path, monkeypatch):
    import cron_scheduler

    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    gw, fake = gateway
    gw.on_message('chat-badge', 'hi')
    gw.on_message('chat-badge', '/cron add "0 9 * * *" 每日汇报')
    gw._chat_pending['chat-badge'] = ['req-1', 'req-2']
    gw._send_panel('chat-badge')
    card = json.dumps(fake.cards[-1][1], ensure_ascii=False)
    assert '待审批 2 条' in card and '定时任务 1 个' in card
    assert '回 y / n' in card


def test_control_card_survives_cron_api_failure(gateway):
    gw, fake = gateway

    def boom():
        raise RuntimeError('cron api down')

    original = gw.api.list_crons
    gw.api.list_crons = boom
    try:
        gw.on_message('chat-badge2', 'hi')
    finally:
        gw.api.list_crons = original
    card = json.dumps([c for chat, c in fake.cards if chat == 'chat-badge2'][0],
                      ensure_ascii=False)
    assert '待审批 0 条' in card and '定时任务 0 个' in card


def test_bypass_proxy_for_feishu_keeps_other_traffic_proxied(monkeypatch):
    monkeypatch.delenv('NO_PROXY', raising=False)
    monkeypatch.delenv('no_proxy', raising=False)
    monkeypatch.delenv('FEISHU_USE_PROXY', raising=False)
    monkeypatch.setenv('NO_PROXY', 'internal.corp')

    feishu_gateway.bypass_proxy_for_feishu()

    hosts = os.environ['NO_PROXY'].split(',')
    assert 'internal.corp' in hosts          # 既有例外保留
    assert '.feishu.cn' in hosts and 'open.feishu.cn' in hosts
    # 幂等：重复调用不重复追加
    feishu_gateway.bypass_proxy_for_feishu()
    assert os.environ['NO_PROXY'].split(',').count('.feishu.cn') == 1


def test_bypass_proxy_for_feishu_can_be_disabled(monkeypatch):
    monkeypatch.delenv('NO_PROXY', raising=False)
    monkeypatch.delenv('no_proxy', raising=False)
    monkeypatch.setenv('FEISHU_USE_PROXY', '1')

    feishu_gateway.bypass_proxy_for_feishu()

    assert 'feishu.cn' not in (os.environ.get('NO_PROXY') or '')


def test_session_card_shows_workspace_counts_and_short_paths():
    from feishu_sessions import list_card

    rows = [{'id': 's1', 'title': '会话一', 'workspace': 'D:/harness-agent',
             'updated': time.time() - 60, 'preview': '预览'},
            {'id': 's2', 'title': '会话二', 'workspace': 'D:/harness-agent',
             'updated': time.time() - 120, 'preview': '预览'},
            {'id': 's3', 'title': '会话三',
             'workspace': 'D:/very/long/path/' + 'x' * 80,
             'updated': time.time() - 180, 'preview': '预览'}]
    card = list_card(rows, 'tok', current_id='s1')
    text = json.dumps(card, ensure_ascii=False)
    assert '当前工作区 · D:/harness-agent（2 个会话）' in text
    assert '只看当前工作区' in text
    assert 'x' * 80 not in text          # 长路径被收窄
    assert '…' in text

    filtered = list_card(rows[:2], 'tok', current_id='s1', filtered=True)
    assert '显示全部工作区' in json.dumps(filtered, ensure_ascii=False)
    assert '（已过滤当前工作区）' in json.dumps(filtered, ensure_ascii=False)


def test_session_filter_toggle_keeps_indexes_consistent(gateway, monkeypatch):
    gw, fake = gateway
    current = gw.api.create_session()
    other = gw.api.create_session()
    gw.bindings['chat-filter'] = current
    rows = [{'id': current, 'title': '当前工作区会话', 'workspace': 'D:/harness-agent',
             'updated': time.time(), 'preview': '预览'},
            {'id': other, 'title': '别的工作区会话', 'workspace': 'D:/tmp/other',
             'updated': time.time() - 60, 'preview': '预览'}]
    monkeypatch.setattr(gw.api, 'list_sessions', lambda limit=1000: list(rows))
    monkeypatch.setattr(gw.api, 'list_workspaces',
                        lambda current=None: {'workspaces': []})
    monkeypatch.setattr(gw.api, 'get_workspace', lambda sid: 'D:/harness-agent')

    gw._show_sessions('chat-filter', list(rows))
    assert '共 2 个会话' in fake.last_card()
    token = gw._session_views['chat-filter'][0]

    # 只看当前工作区：只剩 1 条，且序号 0 指向当前会话
    gw._handle_session_action('chat-filter', {'session_view': token,
                                              'action': 'scope', 'index': 0},
                              'ou_me')
    card = fake.last_card()
    assert '共 1 个会话' in card and '已过滤当前工作区' in card
    assert len(gw._session_views['chat-filter'][1]) == 1

    # 再点一次回到全部
    gw._handle_session_action('chat-filter', {'session_view': token,
                                              'action': 'scope', 'index': 0},
                              'ou_me')
    assert '共 2 个会话' in fake.last_card()
    assert len(gw._session_views['chat-filter'][1]) == 2


def test_session_scope_button_is_accepted_by_card_action(gateway, monkeypatch):
    gw, fake = gateway
    current = gw.api.create_session()
    gw.bindings['chat-scope'] = current
    rows = [{'id': current, 'title': '会话', 'workspace': 'D:/harness-agent',
             'updated': time.time(), 'preview': '预览'}]
    monkeypatch.setattr(gw.api, 'get_workspace', lambda sid: 'D:/harness-agent')
    gw._show_sessions('chat-scope', list(rows))
    token = gw._session_views['chat-scope'][0]

    text, ok = gw.handle_card_action('ou_me', {'session_view': token,
                                               'action': 'scope', 'index': 0},
                                     'om_card', chat_id='chat-scope')
    assert ok and '正在处理会话操作' in text


def test_status_and_logs_render_as_cards_with_buttons(gateway, monkeypatch):
    gw, fake = gateway

    def list_tasks():
        return [{'id': 'bg_1', 'status': 'running', 'command': 'npm test',
                 'duration_ms': 12_000}]

    monkeypatch.setattr(gw.api, 'list_tasks', list_tasks)
    monkeypatch.setattr(gw.api, 'get_task',
                        lambda task_id: {'id': task_id, 'status': 'running',
                                         'command': 'npm test',
                                         'duration_ms': 12_000})
    monkeypatch.setattr(gw.api, 'task_logs',
                        lambda task_id, lines=60: 'line1\nline2\nline3')
    lines = []
    monkeypatch.setattr(gw.api, 'stop_task',
                        lambda task_id: lines.append(task_id) or
                        {'id': task_id, 'status': 'stopped'})

    gw.on_message('chat-task', '/status')
    card = json.dumps(fake.cards[-1][1], ensure_ascii=False)
    assert '后台任务（1）' in card and 'bg_1' in card
    assert '看日志' in card and '停止' in card and 'npm test' in card

    gw.on_message('chat-task', '/logs bg_1 5')
    card = json.dumps(fake.cards[-1][1], ensure_ascii=False)
    assert '任务日志 · bg_1' in card and 'line1' in card

    # 卡片按钮：看日志就地更新同一张卡
    patches = []
    fake.update_step_card = lambda message_id, payload: patches.append(payload)
    gw._handle_task_action('chat-task', {'task_action': 'logs', 'task_id': 'bg_1'},
                           message_id='om_task')
    assert patches and 'line1' in json.dumps(patches[-1], ensure_ascii=False)

    gw._handle_task_action('chat-task', {'task_action': 'stop', 'task_id': 'bg_1'},
                           message_id='om_task')
    assert lines == ['bg_1']
    assert '→ stopped' in fake.last()


def test_status_card_without_tasks_explains_how_to_start(gateway, monkeypatch):
    gw, fake = gateway
    monkeypatch.setattr(gw.api, 'list_tasks', lambda: [])
    gw.on_message('chat-task-empty', '/status')
    card = json.dumps(fake.cards[-1][1], ensure_ascii=False)
    assert '没有后台任务' in card and '刷新' in card


def test_task_action_buttons_are_queued_and_whitelisted(gateway, monkeypatch):
    gw, fake = gateway
    monkeypatch.setattr(gw.api, 'list_tasks', lambda: [])
    text, ok = gw.handle_card_action('ou_me', {'task_action': 'refresh'},
                                     'om_task', chat_id='chat-task2')
    assert ok and '正在处理' in text

    # 白名单外的用户点任务卡片：拒绝
    blocked = FeishuGateway(gw.api, transport=fake,
                            bindings_path=gw.bindings_path,
                            allowed_users={'ou_other'})
    text, ok = blocked.handle_card_action('ou_me', {'task_action': 'refresh'},
                                          'om_task', chat_id='chat-task2')
    assert not ok and '没有会话操作权限' in text


def test_failure_card_offers_retry_and_replays_last_message(gateway):
    gw, fake = gateway

    class StepTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.step_cards = {}
            self.counter = 0

        def send_step_card(self, chat_id, card, reply_to=None):
            self.counter += 1
            message_id = f'om_step_{self.counter}'
            self.step_cards[message_id] = card
            return message_id

        def update_step_card(self, message_id, card):
            self.step_cards[message_id] = card

    transport = StepTransport()
    gw.transport = transport
    calls = []

    def boom(*args, **kwargs):
        raise RuntimeError('agent send failed (402): Insufficient Balance')

    original_stream = gw.api.send_stream
    gw.api.send_stream = boom
    try:
        gw.on_message('chat-retry', '帮我整理一下')
    finally:
        gw.api.send_stream = original_stream

    failure = list(transport.step_cards.values())[-1]
    buttons = [button
               for element in failure['body']['elements']
               if element.get('tag') == 'column_set'
               for column in element['columns']
               for button in column['elements']]
    assert buttons and buttons[-1]['behaviors'][0]['value'] == {'retry': True}
    assert '余额/额度不足' in json.dumps(failure, ensure_ascii=False)

    # 点重试：重跑上一条消息（这一次成功）
    def ok(text, session_id=None, on_delta=None, on_progress=None, **kwargs):
        calls.append(text)
        return '重试成功'

    gw.api.send_stream = ok
    try:
        text, accepted = gw.handle_card_action('ou_me', {'retry': True},
                                               'om_fail', chat_id='chat-retry')
        assert accepted and '正在重试' in text
        gw._retry_last('chat-retry')
    finally:
        gw.api.send_stream = original_stream
    assert calls == ['帮我整理一下']
    latest = list(transport.step_cards.values())[-1]
    assert '重试成功' in json.dumps(latest, ensure_ascii=False)


def test_retry_without_recent_message_explains(gateway):
    gw, fake = gateway
    gw._retry_last('chat-nothing')
    assert '没有可重试的消息' in fake.last()

    gw._last_user['chat-old'] = {'text': 'x', 'session_id': 's1',
                                 'at': time.monotonic() - gw.RETRY_TTL - 1}
    gw._retry_last('chat-old')
    assert '没有可重试的消息' in fake.last()


def test_help_is_a_card_with_entry_buttons(gateway):
    gw, fake = gateway
    cards_before = len(fake.cards)

    class HelpTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.help_cards = []

        def send_step_card(self, chat_id, card, reply_to=None):
            self.help_cards.append((chat_id, card))
            return 'om_help'

    help_transport = HelpTransport()
    gw.transport = help_transport
    gw.on_message('chat-help', '/help')
    assert help_transport.help_cards, '应发送帮助卡片'
    card = help_transport.help_cards[-1][1]
    assert card['schema'] == '2.0'
    text = json.dumps(card, ensure_ascii=False)
    assert '日常不用记命令' in text and '每天早上 9 点提醒我喝水' in text

    values = []
    for element in card['body']['elements']:
        if element.get('tag') == 'column_set':
            for column in element['columns']:
                for button in column['elements']:
                    values.append(button['behaviors'][0]['value'])
        if element.get('tag') == 'action':
            for button in element['actions']:
                values.append(button['behaviors'][0]['value'])
    assert {'panel': 'menu'} in values and {'panel': 'crons'} in values
    assert {'panel': 'perms'} in values
    assert {'task_action': 'refresh'} in values

    # 命令清单在折叠面板里，不占首屏
    panel = next(element for element in card['body']['elements']
                 if element.get('tag') == 'collapsible_panel')
    assert panel['expanded'] is False
    assert '/sessions' in json.dumps(panel, ensure_ascii=False)
    assert len(fake.cards) == cards_before   # 没有额外发 1.0 卡片


def test_help_falls_back_to_text_without_step_cards(gateway):
    gw, fake = gateway
    gw.on_message('chat-help-text', '/help')
    assert '可用命令' in fake.last()


def test_help_panel_button_opens_pending_approvals(gateway):
    gw, fake = gateway
    gw._chat_pending['chat-help-perms'] = []
    gw._handle_panel_action('chat-help-perms', 'perms')
    assert '(无待审批请求)' in fake.last()
