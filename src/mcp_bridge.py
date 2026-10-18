"""MCP 接入桥：后台事件循环线程 + 动态注册 MCP 工具进 tool_registry。

harness 的工具分发是同步的（tool_registry[name](**args)），而 MCP SDK 是 asyncio。
本模块维护一个常驻事件循环线程，用 run_coroutine_threadsafe 桥接：connect 与 call_tool
都投递到该循环执行，同步等待结果。接入后 MCP 工具对模型/子代理/teammate 无差别可见，
但它们的副作用不在策略层的已知集合里：注册方（harness.initialize_mcp_tools）会把工具名
登记为第三方工具，调用默认需要审批（见 hooks.EXTERNAL_TOOLS / exec_policy.external_tools）。

配置（环境变量或配置文件，二选一，都没有则不接入）：
  MCP_SERVERS       JSON 数组内联，如 [{"name":"fs","command":"npx","args":["-y","..."]}]
  MCP_SERVERS_FILE  json 文件路径，内容同上（如 .cache/mcp_servers.json）
"""
import asyncio
import atexit
import json
import os
import threading
from typing import List

from MCPclient import MCPclient

# 后台事件循环线程：MCP 是 asyncio，harness 是同步，通过 run_coroutine_threadsafe 桥接
_loop = asyncio.new_event_loop()
_loop_thread = threading.Thread(target=_loop.run_forever, name="mcp-event-loop", daemon=True)
_loop_thread.start()
_clients: list[MCPclient] = []
_clients_lock = threading.Lock()
_shutdown = False


def _load_server_configs() -> List[dict]: # 加载 MCP server 配置
    """从 MCP_SERVERS（JSON 内联）或 MCP_SERVERS_FILE（json 文件）读取 server 配置。"""
    raw = os.getenv('MCP_SERVERS', '')
    if raw:
        try:
            configs = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"  \033[31m[mcp] invalid MCP_SERVERS JSON: {exc}\033[0m")
            return []
    else:
        path = os.getenv('MCP_SERVERS_FILE', '')
        if not path:
            return []
        try:
            with open(path, 'r', encoding='utf-8') as f:
                configs = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  \033[31m[mcp] cannot read MCP_SERVERS_FILE '{path}': {exc}\033[0m")
            return []

    if not isinstance(configs, list):
        print("  \033[31m[mcp] server configuration must be a JSON array\033[0m")
        return []

    valid = []
    for index, config in enumerate(configs):
        if not isinstance(config, dict):
            print(f"  \033[31m[mcp] skipping server config #{index + 1}: expected an object\033[0m")
            continue
        name, command, args = config.get('name'), config.get('command'), config.get('args', [])
        if not isinstance(name, str) or not name.strip():
            reason = "'name' must be a non-empty string"
        elif not isinstance(command, str) or not command.strip():
            reason = "'command' must be a non-empty string"
        elif not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            reason = "'args' must be an array of strings"
        else:
            valid.append({**config, 'name': name, 'command': command, 'args': args})
            continue
        print(f"  \033[31m[mcp] skipping server config #{index + 1}: {reason}\033[0m")
    return valid


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
    if _shutdown:
        return []
    new_schemas = []
    for cfg in _load_server_configs():
        client = MCPclient(cfg['name'], cfg['command'], cfg.get('args'))
        try:
            tools = _run_async(client.connect())
        except Exception as e:
            print(f"  \033[31m[mcp] {cfg['name']} connect failed: {e}\033[0m")
            continue
        with _clients_lock:
            _clients.append(client)
        for tool in tools:
            name = tool['name']
            if name in registry:
                print(f"  \033[33m[mcp] skip duplicate tool {name}\033[0m")
                continue
            registry[name] = _make_sync_wrapper(client, name)
            new_schemas.append(_openai_schema(tool))
        print(f"  \033[32m[mcp] {cfg['name']}: {len(tools)} tool(s)\033[0m")
    return new_schemas


def shutdown_mcp() -> None:
    """Close connected MCP sessions and stop the bridge event loop."""
    global _shutdown
    if _shutdown:
        return
    _shutdown = True
    with _clients_lock:
        clients = list(_clients)
        _clients.clear()
    for client in clients:
        try:
            _run_async(client.close(), timeout=client._timeout)
        except Exception as exc:
            print(f"  \033[33m[mcp] close failed for {client.name}: {exc}\033[0m")
    _loop.call_soon_threadsafe(_loop.stop)


atexit.register(shutdown_mcp)
