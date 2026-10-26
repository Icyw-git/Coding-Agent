"""Gateway stream ownership and command toggling regressions."""

import json
import threading
from unittest.mock import Mock

import httpx

from gateway_sim import GatewayClient, repl


def test_send_forwards_screenshots_and_downloads_bytes():
    png = b'\x89PNG\r\n\x1a\npng'
    sha1 = 'a' * 40
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith('/messages'):
            return httpx.Response(200, json={
                'session_id': 's1', 'content': 'done',
                'screenshots': [{'name': f'{sha1}.png', 'sha1': sha1}],
            })
        return httpx.Response(200, content=png,
                              headers={'content-type': 'image/png'})

    client = GatewayClient('http://127.0.0.1:9', 't',
                           transport=httpx.MockTransport(handler))
    shots = []
    assert client.send('hi', session_id='s1', on_screenshots=shots.extend) == 'done'
    assert [shot['sha1'] for shot in shots] == [sha1]
    assert client.download_screenshot('s1', f'{sha1}.png') == png
    assert calls == [('POST', '/v1/sessions/s1/messages'),
                     ('GET', f'/v1/sessions/s1/screenshots/{sha1}.png')]


def test_send_stream_forwards_done_screenshots():
    sha1 = 'b' * 40
    body = (
        'event: delta\ndata: {"text": "he"}\n\n'
        'event: done\ndata: ' + json.dumps({
            'content': 'hello',
            'screenshots': [{'name': f'{sha1}.png', 'sha1': sha1}],
            'duration_ms': 1200, 'duration': '1.2s',
        }) + '\n\n'
    )

    def handler(request):
        return httpx.Response(200, content=body.encode(),
                              headers={'content-type': 'text/event-stream'})

    client = GatewayClient('http://127.0.0.1:9', 't',
                           transport=httpx.MockTransport(handler))
    shots = []
    assert client.send_stream('hi', session_id='s1',
                              on_screenshots=shots.extend) == 'hello'
    assert [shot['sha1'] for shot in shots] == [sha1]
    assert client.last_duration_ms == 1200
    assert client.last_duration == '1.2s'


def test_send_skips_empty_screenshot_callback():
    def handler(request):
        return httpx.Response(200, json={'session_id': 's1', 'content': 'ok'})

    client = GatewayClient('http://127.0.0.1:9', 't',
                           transport=httpx.MockTransport(handler))
    seen = []
    client.send('hi', session_id='s1', on_screenshots=seen.extend)
    assert seen == []


def test_restarting_stream_closes_old_reader(monkeypatch):
    responses = []
    http = Mock()

    class Response:
        def __init__(self):
            self.reading = threading.Event()
            self.closed = threading.Event()
            responses.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def raise_for_status(self):
            pass

        def close(self):
            self.closed.set()

        def iter_lines(self):
            self.reading.set()
            assert self.closed.wait(3), 'stream was not closed'
            yield ': ping'

    http.stream.side_effect = lambda *a, **kw: Response()
    monkeypatch.setattr('gateway_sim.httpx.Client', lambda **kw: http)
    client = GatewayClient('http://unused', 't')
    client.session_id = 'first'
    try:
        assert client.start_stream()
        first = client._stream_thread
        for _ in range(100):
            if responses and responses[0].reading.wait(0.01):
                break
        assert responses[0].reading.is_set()
        client.session_id = 'second'
        assert client.start_stream()
        assert not first.is_alive()
        assert responses[0].closed.is_set()
    finally:
        client.close()
    assert not client.streaming
    http.close.assert_called_once()


def test_stream_command_toggles_off(monkeypatch):
    client = Mock(streaming=True)
    commands = iter(['/stream', '/quit'])
    monkeypatch.setattr('builtins.input', lambda _: next(commands))
    assert repl(client) == 0
    client.start_stream.assert_not_called()
    assert client.stop_stream.called
