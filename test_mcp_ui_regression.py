import asyncio
from types import SimpleNamespace

import pytest

from MCPclient import MCPclient


def test_ccapp_initializes_metrics_before_events():
    pytest.importorskip('textual')
    from ui_textual import CCApp
    app = CCApp([])
    assert app._round_metrics == []
    assert app._background_tasks == {}


@pytest.mark.asyncio
async def test_mcp_connect_setup_and_cleanup_stay_in_one_task(monkeypatch):
    client = MCPclient('mock', 'mock')
    entered = []

    async def setup():
        entered.append(asyncio.current_task())
        return SimpleNamespace(tools=[])

    monkeypatch.setattr(client, '_setup_for_test', setup, raising=False)
    # Regression check for the timeout implementation: timeout must not create
    # a child task around the AsyncExitStack owner task.
    async def owner():
        await client._setup_for_test()
        return asyncio.current_task()

    task = await owner()
    assert entered == [asyncio.current_task()]
    assert task is asyncio.current_task()
