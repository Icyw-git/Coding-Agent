import asyncio
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPclient:
    def __init__(self, name: str, command: str, args: list[str] = None):
        self.name = name
        self.command = command
        self.args = args or []
        self.session: ClientSession = None
        self._tools: list[dict] = []
        self._exit_stack = None
        self._timeout: float = 120   # MCP server 挂起时最多等 120s 抛错，避免永久阻塞

    async def connect(self, timeout: float = 120):
        self._timeout = timeout
        server_params = StdioServerParameters(
            command=self.command,
            args=self.args
        )

        self._exit_stack = AsyncExitStack()
        try:
            async def _setup():
                read, write = await self._exit_stack.enter_async_context(
                    stdio_client(server_params))
                self.session = await self._exit_stack.enter_async_context(
                    ClientSession(read, write))
                await self.session.initialize()
                return await self.session.list_tools()

            tools_result = await asyncio.wait_for(_setup(), timeout)
        except Exception:
            await self.close()   # 中途失败也清理子进程，避免泄漏
            raise
        self._tools = []
        for tool in tools_result.tools:
            self._tools.append({
                'name': tool.name,
                'description': tool.description,
                'inputSchema': tool.input_schema,
            })
        return self._tools

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

    async def close(self):
        stack, self._exit_stack = self._exit_stack, None   # 幂等：重复调用不再 aclose
        if stack:
            await stack.aclose()
