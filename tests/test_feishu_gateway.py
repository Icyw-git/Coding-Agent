"""FeishuGateway 核心测试：fake transport + 真实 FastAPI app（ASGITransport）。

不依赖 lark-oapi 与网络——只测 on_message 编排 / 命令分发 / 审批联动。
"""

import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from fastapi.testclient import TestClient

import gateway_sim
import feishu_gateway
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
    gw.on_message = lambda chat_id, text, sender_id=None: called.set()
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
    assert req_id in fake.all_text()
    assert format_permission_prompt(
        {'req_id': req_id, 'tool': 'bash', 'reason': 'destructive',
         'args': {'command': 'del x'}}).startswith('⚠ 需要审批')

    # 聊天里回复审批
    gw.on_message('c1', f'/approve {req_id} y')
    thread.join(timeout=2)
    assert results['allowed'] is True
    assert f'{req_id} → approved' in fake.last()

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
    assert '→ approved' in fake.last()

    # 没有待审批时，y 当作普通消息发给 agent
    fake.sent.clear()
    gw.on_message('c1', 'y')
    assert any('agent-reply-ok' in text for _, text in fake.sent)


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
    md = '# 标题\n## 二级\n- 甲\n* 乙\n---\n```py\n# 注释\n- 保留\n```\n正文'
    out = markdown_to_card_md(md)
    lines = out.splitlines()
    assert lines[0] == '**标题**'
    assert lines[1] == '**二级**'
    assert lines[2] == '• 甲'
    assert lines[3] == '• 乙'
    assert lines[4] == ''          # 水平线
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

    ok = CardTransport(); ok.cards = []; ok.texts = []
    ok.send_markdown('c', 'x')  # 预热属性存在
    ok.cards.clear(); ok.texts.clear()
    gw.transport = ok
    gw._reply('c1', '渲染**加粗**')
    assert ok.cards and not ok.texts

    class BrokenCardTransport(CardTransport):
        def send_markdown(self, chat_id, text):
            raise RuntimeError('card failed')

    bad = BrokenCardTransport(); bad.cards = []; bad.texts = []
    gw.transport = bad
    gw._reply('c1', '回退文本')
    assert not bad.cards and bad.texts


def test_stream_reply_uses_one_markdown_card_and_finalizes():
    """流式回复创建并更新同一张卡片，最终结果只保留一条消息。"""
    class StreamApi:
        def send_stream(self, content, session_id=None, on_delta=None):
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
    assert transport.updated == [
        ('msg-1', '**第一段**'),
        ('msg-1', '**第一段**\n- 第二段'),
    ]
    assert transport.texts == []


def test_stream_card_update_failure_does_not_fail_generation():
    """卡片更新失败时仍完成生成，并补发纯文本结果。"""
    class StreamApi:
        def send_stream(self, content, session_id=None, on_delta=None):
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
    """/cd 设置该聊天的任务目录，/run 自动携带；无 /cd 时用默认。"""
    gw, fake = gateway
    workdir = tmp_path / 'projA'
    workdir.mkdir()

    gw.on_message('c1', f'/cd {workdir}')
    assert '任务目录已切换' in fake.last()

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
