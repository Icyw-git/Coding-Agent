import mcp_bridge


def test_shutdown_mcp_closes_registered_clients(monkeypatch):
    class FakeLoop:
        def __init__(self):
            self.stopped = False

        def call_soon_threadsafe(self, callback):
            callback()

        def stop(self):
            self.stopped = True

    class FakeClient:
        name = 'fake'
        _timeout = 1

        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    client = FakeClient()
    loop = FakeLoop()
    monkeypatch.setattr(mcp_bridge, '_shutdown', False)
    monkeypatch.setattr(mcp_bridge, '_clients', [client])
    monkeypatch.setattr(mcp_bridge, '_loop', loop)
    closed = []

    def run_async(coroutine, timeout):
        closed.append(coroutine)
        try:
            coroutine.send(None)
        except StopIteration:
            pass

    monkeypatch.setattr(mcp_bridge, '_run_async', run_async)

    mcp_bridge.shutdown_mcp()

    assert client.closed
    assert loop.stopped
    assert closed


def test_register_mcp_tools_skips_after_shutdown(monkeypatch):
    monkeypatch.setattr(mcp_bridge, '_shutdown', True)
    assert mcp_bridge.register_mcp_tools({}) == []
