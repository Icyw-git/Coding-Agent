"""本地 mock MCP server（stdio）：用于验证 harness 的 MCP 接入流程。

提供两个工具：
  echo  —— 文本回显
  add   —— 两个整数相加

运行：python mock_mcp_server.py  （被 .cache/mcp_servers.json 里的 command 拉起）
"""
import asyncio
from mcp import types
from mcp.server import Server, InitializationOptions
from mcp.server.stdio import stdio_server

TOOL_DEFS = [
    types.Tool(
        name="echo",
        description="Echo back the given text",
        inputSchema={"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]},
    ),
    types.Tool(
        name="add",
        description="Add two integers and return the sum",
        inputSchema={"type": "object",
                     "properties": {
                         "a": {"type": "integer"},
                         "b": {"type": "integer"},
                     },
                     "required": ["a", "b"]},
    ),
]


async def _list_tools(ctx, params):
    return types.ListToolsResult(tools=TOOL_DEFS)


async def _call_tool(ctx, params):
    name, args = params.name, params.arguments or {}
    if name == "echo":
        text = f"echo:{args.get('text', '')}"
    elif name == "add":
        text = f"sum:{args.get('a', 0) + args.get('b', 0)}"
    else:
        text = f"unknown tool: {name}"
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)])


server = Server("mock", on_list_tools=_list_tools, on_call_tool=_call_tool)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(
            server_name="mock", server_version="0.1.0",
            capabilities=server.get_capabilities()))


if __name__ == "__main__":
    asyncio.run(main())
