"""端到端验收：真实 agent_loop 里，工具面会随 tool_search 变化。

需要 `HARNESS_TOOL_DISCOVERY=1`（默认关闭，原因见 src/tool_discovery.py）。

验收点（来自 docs/TOOL_DISCOVERY_PROPOSAL.md 第 3 节）：
1. 第一轮请求里没有 schedule_cron（被延迟）；
2. 模型调 tool_search 之后，第二轮请求里就有 schedule_cron；
3. 第三轮仍在（粘性）；
4. 全程 tool_registry 不变（能力没被裁）。
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# 必须相对 `__file__`：用相对路径 `'src'` / `'.'` 只在 cwd 恰好是仓库根时成立，
# 换个目录跑就 ModuleNotFoundError（entrypoint.py 踩过同一个坑）。
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / 'src'))
sys.path.insert(0, str(_ROOT))

os.environ.setdefault('HARNESS_HOME', tempfile.mkdtemp(prefix='acceptance-'))

import harness  # noqa: E402
import tools  # noqa: E402
from session_store import SessionEventStore  # noqa: E402


class Call:
    def __init__(self, call_id, name, arguments='{}'):
        self.id = call_id
        self.type = 'function'
        self.function = SimpleNamespace(name=name, arguments=arguments)


class Message:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self):
        out = {'role': 'assistant', 'content': self.content}
        if self.tool_calls:
            out['tool_calls'] = [
                {'id': c.id, 'type': 'function',
                 'function': {'name': c.function.name,
                              'arguments': c.function.arguments}}
                for c in self.tool_calls]
        return out


def response(content=None, tool_calls=None, finish='stop'):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=Message(content, tool_calls),
                                 finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10,
                              total_tokens=110, prompt_tokens_details=None,
                              prompt_cache_hit_tokens=None,
                              cache_creation_input_tokens=None,
                              completion_tokens_details=None))


class Scripted:
    def __init__(self, items):
        self.items = list(items)
        self.tool_names = []

    def create(self, **kwargs):
        self.tool_names.append({t['function']['name'] for t in (kwargs.get('tools') or [])})
        return self.items.pop(0)


def main():
    store = SessionEventStore(harness.SESSIONS_DIR, session_id='acceptance')
    scripted = Scripted([
        response(tool_calls=[Call('c1', 'tool_search', '{"query": "每天提醒我"}')],
                 finish='tool_calls'),
        response(content='done'),
    ])
    harness.client.chat.completions.create = scripted.create

    # 关掉副通道，保持脚本响应队列对齐
    for name, value in (('load_memories', lambda messages: ''),
                        ('extract_memories', lambda *a, **k: None),
                        ('consolidate_memories', lambda *a, **k: None),
                        ('generate_session_title', lambda *a, **k: 'acceptance'),
                        ('_post_turn_housekeeping', lambda *a, **k: None),
                        ('wait_for_mcp_tools', lambda timeout=10.0: True),
                        ('streaming_enabled', lambda: False)):
        setattr(harness, name, value)

    registry_before = set(tools.tool_registry)
    harness.agent_loop([{'role': 'user', 'content': '每天提醒我喝水'}],
                       session_store=store, housekeeping=False)

    import tool_discovery
    if not tool_discovery.enabled():
        print(f'工具发现当前关闭（{tool_discovery.ENV} 未设）。')
        print('这是默认值：延迟加载会打掉 prompt 缓存前缀，实测净亏（见 PERFORMANCE_PROFILE）。')
        print('要看机制行为，请带 HARNESS_TOOL_DISCOVERY=1 重跑。')
        return 0

    ok = True
    first, second = scripted.tool_names[0], scripted.tool_names[1]
    print(f'第 1 轮工具数：{len(first)}')
    print(f'  含 schedule_cron：{"schedule_cron" in first}')
    print(f'  含 tool_search  ：{"tool_search" in first}')
    print(f'第 2 轮工具数：{len(second)}')
    print(f'  含 schedule_cron：{"schedule_cron" in second}')

    ok &= 'schedule_cron' not in first
    ok &= 'tool_search' in first
    ok &= 'schedule_cron' in second
    ok &= 'remember' not in second           # 只激活了 cron 组
    ok &= set(tools.tool_registry) == registry_before   # 能力没被裁

    # 粘性：再跑一轮不该丢
    scripted.items = [response(content='again')]
    harness.agent_loop([{'role': 'user', 'content': '继续'}],
                       session_store=store, housekeeping=False)
    third = scripted.tool_names[2]
    print(f'第 3 轮工具数：{len(third)}')
    print(f'  含 schedule_cron：{"schedule_cron" in third}（粘性）')
    ok &= 'schedule_cron' in third

    print()
    print('验收结果:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
