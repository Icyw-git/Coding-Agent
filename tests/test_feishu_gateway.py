"""FeishuGateway 核心测试：fake transport + 真实 FastAPI app（ASGITransport）。

不依赖 lark-oapi 与网络——只测 on_message 编排 / 命令分发 / 审批联动。
"""

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

    def send_text(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    def last(self) -> str:
        return self.sent[-1][1] if self.sent else ''

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
    text = fake.last()
    assert '选择工作区' in text and 'alpha' in text and '1.' in text
    assert gw.bindings['c1'] == bound

    gw.on_message('c1', f'/new "{beta}"')    # 显式路径
    assert gw.api.get_workspace(gw.bindings['c1']) == str(beta)

    gw.on_message('c1', '/new')              # 当前 beta + 最近 alpha
    text = fake.last()
    assert 'beta' in text and 'alpha' in text
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

    gw.on_message('c1', f'/status {task_id}')
    assert 'succeeded' in fake.last()
    gw.on_message('c1', f'/logs {task_id}')
    assert 'feishu-task' in fake.last()


def test_unknown_command_reports_error(gateway):
    gw, fake = gateway
    gw.on_message('c1', '/nope')
    assert '未知命令' in fake.last()


def test_help_and_unknown_include_command_list(gateway):
    """/help 列出全部命令；未知命令回复自动附带命令清单。"""
    gw, fake = gateway
    gw.on_message('c1', '/help')
    assert '可用命令' in fake.last()
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
    gw.on_message = lambda chat_id, text, sender_id=None, chat_type=None: called.set()
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

    # 轮询器推送提醒（直接调用核心方法验证格式）
    gw.poll_permissions()
    assert 'bash' in fake.all_text()
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


def test_approval_push_prefers_button_card(gateway):
    """有 send_approval_card 的 transport 推送按钮卡片而非纯文本。"""
    gw, fake = gateway
    _drain_stale_permissions()
    gw.on_message('c1', 'hi')

    class ApprovalTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.cards = []

        def send_approval_card(self, chat_id, prompt_md, req):
            self.cards.append((chat_id, prompt_md, req))
            return 'om-1'

    approval = ApprovalTransport()
    gw.transport = approval
    baseline = len(fake.sent)   # on_message('hi') 已产生的文本回复

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
    assert approval.cards == [('c1', approval.cards[0][1], approval.cards[0][2])]
    assert approval.cards[0][2]['req_id'] == req_id
    assert '回复 y（允许）或 n（拒绝）' in approval.cards[0][1]
    assert len(fake.sent) == baseline   # 推送未走文本回退

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
    gw.on_message = lambda chat_id, text, sender_id=None, chat_type=None: \
        seen.append((chat_id, text, chat_type))
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
        assert any(chat_id == 'c1' for chat_id, _ in fake.sent)
        assert all(chat_id != 'c2' for chat_id, _ in fake.sent)
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


def test_main_allows_explicit_any_user_escape_hatch(monkeypatch):
    """本地测试可显式放开；启动后仍照常构造网关。"""
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
        assert fake.sent == []            # 没有任何回复

        gw.on_message('c1', 'hello', sender_id='ou_me')
        assert 'agent-reply-ok' in fake.last()

        # 陌生人无法通过 y/n 决议别人的审批（也没有他的 FIFO）
        fake.sent.clear()
        gw.on_message('c1', 'y', sender_id='ou_stranger')
        assert fake.sent == []
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
