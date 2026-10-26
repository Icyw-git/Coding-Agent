"""网关发送失败时的本机通知降级（P2c）。

只测降级策略：不连飞书、不弹真通知（notifier 是记录调用参数的假函数），
也不启动轮询线程。
"""

import pytest

import feishu_gateway
from feishu_gateway import FeishuGateway


class Alerts:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, title, body='', *, level='info'):
        self.calls.append({'title': title, 'body': body, 'level': level})
        return {'sent': True}


class Transport:
    def __init__(self, *, fail=False, card_fail=False):
        self.fail = fail
        self.card_fail = card_fail
        self.sent: list[tuple[str, str]] = []
        self.cards: list[tuple[str, str]] = []

    def send_markdown(self, chat_id, text):
        if self.card_fail:
            raise RuntimeError('card down')
        self.cards.append((chat_id, text))

    def send_text(self, chat_id, text):
        if self.fail:
            raise RuntimeError('feishu down')
        self.sent.append((chat_id, text))


class TextOnlyTransport:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    def send_text(self, chat_id, text):
        if self.fail:
            raise RuntimeError('feishu down')
        self.sent.append((chat_id, text))


class FakeApi:
    def __init__(self, *, heartbeat=(), results=()):
        self.heartbeat = list(heartbeat)
        self.results = list(results)
        self.acked: list[str] = []
        self.cron_acked: list[str] = []

    def pending_heartbeat(self):
        return self.heartbeat

    def ack_heartbeat(self, occurrence_id):
        self.acked.append(occurrence_id)
        return True

    def pending_cron_results(self):
        return self.results

    def ack_cron_result(self, item_id):
        self.cron_acked.append(item_id)
        return True


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # 用户机器上可能真的设了开关，测试只认自己显式设置的值
    monkeypatch.delenv(feishu_gateway.LOCAL_ALERT_ENV, raising=False)


def make_gateway(tmp_path, *, transport=None, notifier=None, api=None):
    return FeishuGateway(
        api if api is not None else FakeApi(),
        transport=transport if transport is not None else TextOnlyTransport(fail=True),
        bindings_path=tmp_path / 'bindings.json',
        notifier=notifier,
    )


def test_reply_text_failure_alerts_and_raises(tmp_path):
    alerts = Alerts()
    gw = make_gateway(tmp_path, notifier=alerts)

    with pytest.raises(RuntimeError, match='feishu down'):
        gw._reply('chat-1', '普通回复')

    assert len(alerts.calls) == 1
    call = alerts.calls[0]
    assert call['title'] == '飞书回复发送失败'
    assert call['level'] == 'error'
    assert 'feishu down' in call['body']


def test_reply_alert_dedupes_same_key(tmp_path):
    alerts = Alerts()
    gw = make_gateway(tmp_path, notifier=alerts)

    for _ in range(3):
        with pytest.raises(RuntimeError):
            gw._reply('chat-1', '普通回复')

    assert len(alerts.calls) == 1


def test_reply_card_falls_back_to_text_without_alert(tmp_path):
    """卡片失败但文本成功：这是正常回退，不该报本地告警。"""
    alerts = Alerts()
    transport = Transport(card_fail=True)
    gw = make_gateway(tmp_path, transport=transport, notifier=alerts)

    gw._reply('chat-1', '渲染**加粗**')

    assert transport.sent == [('chat-1', '渲染**加粗**')]
    assert alerts.calls == []


def test_no_notifier_keeps_old_behavior(tmp_path):
    gw = make_gateway(tmp_path)

    with pytest.raises(RuntimeError, match='feishu down'):
        gw._reply('chat-1', '普通回复')


def test_env_opt_out_disables_alerts(tmp_path, monkeypatch):
    monkeypatch.setenv(feishu_gateway.LOCAL_ALERT_ENV, '0')
    alerts = Alerts()
    gw = make_gateway(tmp_path, notifier=alerts)

    with pytest.raises(RuntimeError):
        gw._reply('chat-1', '普通回复')

    assert alerts.calls == []


def test_watch_notify_failure_alerts_and_raises(tmp_path):
    alerts = Alerts()
    gw = make_gateway(tmp_path, notifier=alerts)
    gw.bindings = {'chat-1': 'sess-1'}

    with pytest.raises(RuntimeError, match='feishu down'):
        gw._watch_notify('sess-1', 'completed', {})

    assert len(alerts.calls) == 1
    assert alerts.calls[0]['title'] == '飞书回合通知失败'
    assert '回合完成' in alerts.calls[0]['body']


def test_watch_notify_success_sends_text_and_no_alert(tmp_path):
    alerts = Alerts()
    transport = TextOnlyTransport()
    gw = make_gateway(tmp_path, transport=transport, notifier=alerts)
    gw.bindings = {'chat-1': 'sess-1'}

    gw._watch_notify('sess-1', 'approval_timeout',
                     {'timeout_s': 30, 'tool': 'bash', 'reason': 'rm -rf build'})

    assert transport.sent and '审批超时' in transport.sent[0][1]
    assert alerts.calls == []


def test_heartbeat_failure_alerts_and_keeps_pending(tmp_path):
    alerts = Alerts()
    api = FakeApi(heartbeat=[{'occurrence_id': 'occ-1', 'text': '每日摘要'}])
    gw = make_gateway(tmp_path, notifier=alerts, api=api)
    gw.bindings = {'chat-1': 'sess-1'}

    assert gw.poll_heartbeat() == 0
    assert api.acked == []
    assert len(alerts.calls) == 1
    assert alerts.calls[0]['title'] == '飞书心跳推送失败'


def test_heartbeat_success_acks_without_alert(tmp_path):
    alerts = Alerts()
    api = FakeApi(heartbeat=[{'occurrence_id': 'occ-1', 'text': '每日摘要'}])
    gw = make_gateway(tmp_path, transport=TextOnlyTransport(), notifier=alerts,
                      api=api)
    gw.bindings = {'chat-1': 'sess-1'}

    assert gw.poll_heartbeat() == 1
    assert api.acked == ['occ-1']
    assert alerts.calls == []


def test_cron_result_failure_alerts_and_keeps_pending(tmp_path):
    alerts = Alerts()
    api = FakeApi(results=[{'id': 'r1', 'chat_id': 'chat-1', 'text': '任务结果'}])
    gw = make_gateway(tmp_path, notifier=alerts, api=api)
    gw.bindings = {'chat-1': 'sess-1'}

    assert gw.poll_cron_results() == 0
    assert api.cron_acked == []
    assert len(alerts.calls) == 1
    assert alerts.calls[0]['title'] == '飞书定时结果推送失败'


def test_heartbeat_and_cron_use_distinct_keys(tmp_path):
    """两类推送同时失败时各弹一次：去重按事件键，不是全局节流。"""
    alerts = Alerts()
    api = FakeApi(heartbeat=[{'occurrence_id': 'occ-1', 'text': '摘要'}],
                  results=[{'id': 'r1', 'chat_id': 'chat-1', 'text': '结果'}])
    gw = make_gateway(tmp_path, notifier=alerts, api=api)
    gw.bindings = {'chat-1': 'sess-1'}

    gw.poll_heartbeat()
    gw.poll_cron_results()

    titles = [call['title'] for call in alerts.calls]
    assert titles == ['飞书心跳推送失败', '飞书定时结果推送失败']


def test_notifier_exception_does_not_mask_send_error(tmp_path):
    def broken(title, body='', *, level='info'):
        raise OSError('no toast service')

    gw = make_gateway(tmp_path, notifier=broken)

    with pytest.raises(RuntimeError, match='feishu down'):
        gw._reply('chat-1', '普通回复')


def test_alert_body_is_truncated(tmp_path):
    alerts = Alerts()
    gw = make_gateway(tmp_path, notifier=alerts)

    assert gw._local_alert('key', '标题', 'y' * 2000) is True
    assert len(alerts.calls[0]['body']) == feishu_gateway.LOCAL_ALERT_BODY
