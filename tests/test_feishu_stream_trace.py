"""流式延迟诊断：FEISHU_STREAM_TRACE 开关与单行摘要。"""

import feishu_gateway


def _gw(monkeypatch, *, transport, api):
    monkeypatch.setattr(feishu_gateway, 'FeishuGateway', feishu_gateway.FeishuGateway)
    gw = feishu_gateway.FeishuGateway.__new__(feishu_gateway.FeishuGateway)
    gw.transport = transport
    gw.api = api
    gw.bindings = {}
    gw._bindings_lock = feishu_gateway.threading.Lock()
    gw._last_user = {}
    gw._last_card_patch = 0.0
    return gw


class Transport:
    def __init__(self):
        self.cards = []
        self.patches = []

    def send_step_card(self, chat_id, card, reply_to=None):
        self.cards.append((chat_id, card))
        return 'om_1'

    def update_step_card(self, message_id, card):
        self.patches.append((message_id, card))

    def send_text(self, chat_id, text):
        self.cards.append((chat_id, text))


class Api:
    def __init__(self):
        self.last_duration = '00:01'

    def send_stream(self, content, session_id=None, on_delta=None,
                    on_progress=None, origin_chat=None, on_screenshots=None):
        if on_progress:
            on_progress({'phase': 'thinking', 'elapsed': 1,
                         'elapsed_text': '00:01'})
        for chunk in ('你', '好'):
            if on_delta:
                on_delta(chunk)
        return '你好'


def test_stream_trace_is_off_by_default(monkeypatch, capsys):
    monkeypatch.delenv(feishu_gateway.STREAM_TRACE_ENV, raising=False)
    transport, api = Transport(), Api()
    gw = _gw(monkeypatch, transport=transport, api=api)

    gw._send_agent_reply('chat', 'sess', 'hi')

    assert fs_trace_lines(capsys) == []


def test_stream_trace_reports_counts_and_failure(monkeypatch, capsys):
    monkeypatch.setenv(feishu_gateway.STREAM_TRACE_ENV, '1')
    transport, api = Transport(), Api()
    gw = _gw(monkeypatch, transport=transport, api=api)

    gw._send_agent_reply('chat', 'sess', 'hi')

    output = capsys.readouterr().out
    line = next(item for item in output.splitlines()
                if item.startswith('[feishu][stream]'))
    assert 'deltas=2' in line
    assert 'progress=1' in line
    assert 'failed=False' in line
    assert 'first_delta=' in line


def test_stream_trace_is_printed_on_failure(monkeypatch, capsys):
    monkeypatch.setenv(feishu_gateway.STREAM_TRACE_ENV, '1')

    class Broken(Api):
        def send_stream(self, *args, **kwargs):
            raise RuntimeError('provider 500')

    gw = _gw(monkeypatch, transport=Transport(), api=Broken())

    gw._send_agent_reply('chat', 'sess', 'hi')

    output = capsys.readouterr().out
    line = next(item for item in output.splitlines()
                if item.startswith('[feishu][stream]'))
    assert 'error=provider 500' in line


def fs_trace_lines(capsys):
    return [item for item in capsys.readouterr().out.splitlines()
            if item.startswith('[feishu][stream]')]
