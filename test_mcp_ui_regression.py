import asyncio

from MCPclient import MCPclient


def test_mcp_session_entry_and_exit_stay_in_one_task(monkeypatch):
    """AnyIO 的 context 必须在同一个任务里进入/退出：session 跑在一条长生命周期任务里。

    旧实现用 wait_for 包住整个 session；新实现改用 wait_for 只包单次 RPC，
    并把 session 任务保存下来，close() 负责取消与等待。
    """
    async def scenario():
        client = MCPclient('mock', 'mock')
        tasks = []

        async def serve(ready):
            tasks.append(asyncio.current_task())
            ready.set_result([])
            await client._stop.wait()

        monkeypatch.setattr(client, '_serve_session', serve, raising=False)
        assert await client.connect() == []
        assert tasks == [client._session_task]
        await client.close()
        assert client._session_task is None

    asyncio.run(scenario())
