"""请求前缀指纹（HARNESS_DEBUG_CACHE）：定位破坏缓存的中段改写。

供应商的 KV 缓存按前缀命中，只有「纯追加」的请求能完整复用。history_snipped /
tool_result_budget 这类改写是已知代价；本模块只提供观测，不改变请求内容，
默认关闭，排查缓存命中率时用 `HARNESS_DEBUG_CACHE=1` 打开。
"""
import hashlib
import os

ENV = 'HARNESS_DEBUG_CACHE'


def enabled() -> bool:
    return (os.getenv(ENV) or '').strip().casefold() in ('1', 'true', 'yes', 'on')


def message_fingerprint(message: dict) -> str:
    """单条消息的稳定指纹：只取影响请求内容与顺序的字段。"""
    payload = {key: message.get(key)
               for key in ('role', 'content', 'tool_call_id', 'name')}
    payload['tool_calls'] = [
        {'id': call.get('id'),
         'name': (call.get('function') or {}).get('name'),
         'arguments': (call.get('function') or {}).get('arguments')}
        for call in (message.get('tool_calls') or [])]
    return hashlib.sha1(repr(payload).encode('utf-8', 'replace')).hexdigest()[:12]


def fingerprints(messages) -> list[str]:
    return [message_fingerprint(message) for message in messages or []]


def first_divergence(previous: list[str], current: list[str]) -> int:
    """第一条不同的消息下标；纯追加（含空旧列表）返回旧列表长度。"""
    for index, (before, after) in enumerate(zip(previous, current, strict=False)):
        if before != after:
            return index
    return min(len(previous), len(current))
