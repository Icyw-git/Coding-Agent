"""MCP 接入桥：后台事件循环线程 + 动态注册 MCP 工具进 tool_registry。

harness 的工具分发是同步的（tool_registry[name](**args)），而 MCP SDK 是 asyncio。
本模块维护一个常驻事件循环线程，用 run_coroutine_threadsafe 桥接：connect 与 call_tool
都投递到该循环执行，同步等待结果。接入后 MCP 工具对模型/子代理/teammate 无差别可见。

配置（环境变量或配置文件，二选一，都没有则不接入）：
  MCP_SERVERS       JSON 数组内联，如 [{"name":"fs","command":"npx","args":["-y","..."]}]
  MCP_SERVERS_FILE  json 文件路径，内容同上（如 .cache/mcp_servers.json）
"""
import asyncio
import json
import os
import threading
from typing import List

from MCPclient import MCPclient

# 后台事件循环线程：MCP 是 asyncio，harness 是同步，通过 run_coroutine_threadsafe 桥接
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()


def _load_server_configs() -> List[dict]:
    """从 MCP_SERVERS（JSON 内联）或 MCP_SERVERS_FILE（json 文件）读取 server 配置。"""
    raw = os.getenv('MCP_SERVERS', '')
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []
    path = os.getenv('MCP_SERVERS_FILE', '')
    if path and os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _openai_schema(tool: dict) -> dict:
    """MCP 工具的 inputSchema 就是 JSON Schema，直接映射成 OpenAI 工具格式。"""
    params = tool.get('inputSchema') or {}
    if 'type' not in params:
        params = {**params, 'type': 'object'}
    return {
        'type': 'function',
        'function': {
            'name': tool['name'],
            'description': tool.get('description', ''),
            'parameters': params,
        },
    }


def _run_async(coro, timeout: float = 120):
    """把异步协程投递到后台事件循环，同步等待结果（超时抛错由调用方兜底）。"""
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=timeout)


def _make_sync_wrapper(client: MCPclient, tool_name: str):
    """为单个 MCP 工具生成同步 handler，与 tool_registry 的 (**args) 签名一致。"""
    def run(**kwargs):
        fut = asyncio.run_coroutine_threadsafe(
            client.call_tool(tool_name, kwargs), _loop)
        try:
            return fut.result(timeout=client._timeout)
        except Exception as e:
            return f'MCP error: {e}'
    run.__name__ = tool_name
    return run


def register_mcp_tools(registry: dict) -> List[dict]:
    """连接配置的所有 MCP server，把工具动态注册进 registry（tool_registry）。
    返回新增的 OpenAI 工具 schema 列表，供 harness 追加进 TOOLS；无配置返回 []。"""
    new_schemas = []
    for cfg in _load_server_configs():
        client = MCPclient(cfg['name'], cfg['command'], cfg.get('args'))
        try:
            tools = _run_async(client.connect())
        except Exception as e:
            print(f"  \033[31m[mcp] {cfg['name']} connect failed: {e}\033[0m")
            continue
        for tool in tools:
            name = tool['name']
            if name in registry:
                print(f"  \033[33m[mcp] skip duplicate tool {name}\033[0m")
                continue
            registry[name] = _make_sync_wrapper(client, name)
            new_schemas.append(_openai_schema(tool))
        print(f"  \033[32m[mcp] {cfg['name']}: {len(tools)} tool(s)\033[0m")
    return new_schemas
