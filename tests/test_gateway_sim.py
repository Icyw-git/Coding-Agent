"""Gateway stream ownership and command toggling regressions."""

import threading
from unittest.mock import Mock

from gateway_sim import GatewayClient, repl


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
