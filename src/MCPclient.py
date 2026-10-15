import asyncio
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPclient: #进程间通信客户端
    def __init__(self, name: str, command: str, args: list[str] = None):
        self.name = name
        self.command = command
        self.args = args or []
        self.session: ClientSession = None
        self._tools: list[dict] = []
        self._session_task = None
        self._stop = None
        self._timeout: float = 120   # MCP server 挂起时最多等 120s 抛错，避免永久阻塞

    async def connect(self, timeout: float = 120):
        self._timeout = timeout
        ready = asyncio.get_running_loop().create_future()
        self._stop = asyncio.Event()
        self._session_task = asyncio.create_task(self._serve_session(ready))
        try:
            return await asyncio.shield(ready)
        except BaseException:
            await self.close()
            raise

    async def _serve_session(self, ready):
        """Keep AnyIO context entry and exit in the same long-lived task."""
        server_params = StdioServerParameters(
            command=self.command,
            args=self.args
        )
        try:
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(server_params))
                self.session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(self.session.initialize(), self._timeout)
                tools_result = await asyncio.wait_for(self.session.list_tools(), self._timeout)
                self._tools = [
                    {'name': tool.name, 'description': tool.description,
                     'inputSchema': tool.input_schema}
                    for tool in tools_result.tools
                ]
                ready.set_result(self._tools)
                await self._stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                raise
        finally:
            self.session = None

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        if not self.session:
            return 'MCP error: not connected'
        try:
            result = await asyncio.wait_for(
                self.session.call_tool(tool_name, arguments), self._timeout)
            texts = [part.text for part in result.content if hasattr(part, 'text')]
            return '\n'.join(texts) if texts else str(result)
        except asyncio.TimeoutError:
            return 'MCP error: tool call timeout'
        except Exception as e:
            return f'MCP error: {e}'

    async def close(self): # 关闭 MCP session
        task = self._session_task
        if task is not None:
            self._stop.set()
            try:
                await task
            finally:
                self._session_task = None
