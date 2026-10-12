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
        self._exit_stack = None # 异步上下文栈，用于清理子进程
        self._timeout: float = 120   # MCP server 挂起时最多等 120s 抛错，避免永久阻塞

    async def connect(self, timeout: float = 120):
        self._timeout = timeout
        server_params = StdioServerParameters(
            command=self.command,
            args=self.args
        )

        self._exit_stack = AsyncExitStack()
        try:
            # Enter and later close both context managers in this same task. Wrapping
            # the whole setup in wait_for creates a child task, which violates AnyIO's
            # cancel-scope ownership when the stack is closed during shutdown.
            read, write = await self._exit_stack.enter_async_context(
                stdio_client(server_params)) # 进接 MCP server
            self.session = await self._exit_stack.enter_async_context(
                ClientSession(read, write)) # 创建 MCP session
            await asyncio.wait_for(self.session.initialize(), timeout)
            tools_result = await asyncio.wait_for(self.session.list_tools(), timeout)
        except Exception:
            await self.close()   # 中途失败也清理子进程，避免泄漏
            raise
        self._tools = []
        for tool in tools_result.tools: # 遍历所有工具
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

    async def close(self): # 关闭 MCP session
        stack, self._exit_stack = self._exit_stack, None   # 幂等：重复调用不再 aclose 子进程
        if stack:
            await stack.aclose()
