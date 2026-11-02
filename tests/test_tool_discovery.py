"""工具按需发现：把 41 个低频工具的 schema 从"每轮都发"改成"用时才发现"。

为什么
------
默认工具面 5,762 tokens 里，基础 10 个只占 1,415，其余 41 个占 4,347（75%）。
方案与取舍见 docs/TOOL_DISCOVERY_PROPOSAL.md：第一步只延迟 8 个**整组**，
杂项保持常发（零散、模型猜不到组名）。

三条必须成立的性质（任何一条不成立，这个机制就是在制造静默的功能缺失）：

1. 延迟的只是"发不发 schema"，**工具仍然可调用**（`tool_registry` 不动，策略层照旧）；
2. 搜到之后**粘住**，不会下一轮又消失；
3. 子代理等**不用发现机制**的入口，仍拿到完整的（能力过滤后）工具面。
"""
from __future__ import annotations

import pytest

import tool_discovery
import tools


@pytest.fixture(autouse=True)
def _discovery_on(monkeypatch):
    """这些用例测的是"开启时"的行为；关闭时的行为另有专门用例。

    默认关闭的原因见 src/tool_discovery.py 的 ENV 注释（prompt 缓存账）。
    """
    monkeypatch.setenv(tool_discovery.ENV, '1')
    tool_discovery.reset()
    import tools

    tools.rebuild_tools()
    yield
    tool_discovery.reset()
    tools.rebuild_tools()


def test_discovery_is_off_by_default(monkeypatch):
    """默认关闭：延迟加载会让 tools 中途变化，把缓存前缀打掉。

    实测（p50 prompt 44,178 tokens、命中率 96%）：省下的 3,083 tokens 因为在缓存里
    实际只值 ≈419/轮，而一次前缀失效多付 ≈38,170。盈亏平衡 91 轮，实测平均会话 42 轮。
    """
    import tools

    monkeypatch.delenv(tool_discovery.ENV, raising=False)
    tool_discovery.reset()
    tools.rebuild_tools()
    try:
        names = {s['function']['name'] for s in tools.TOOLS}
        assert 'schedule_cron' in names, '关闭时应当是完整能力面'
        assert 'tool_search' not in names, '关闭时 tool_search 没有可发现的东西，不该发'

        # 关闭时 == 完整能力面（只是不发 tool_search 本身：没有可发现的东西）
        expected = {s['function']['name'] for s in tools.capability_surface()} - {
            'tool_search'}
        assert names == expected
    finally:
        tools.rebuild_tools()


def _names(schemas) -> set[str]:
    return {schema['function']['name'] for schema in schemas}


def test_base_surface_keeps_core_and_drops_whole_groups():
    """基础面：核心 10 个 + 杂项 + tool_search；8 个整组不在里面。"""
    names = _names(tools.TOOLS)

    for core in ('bash', 'read', 'write', 'edit', 'apply_patch',
                 'grep', 'glob', 'ls', 'todo_write', 'todo_read', 'tool_search'):
        assert core in names, core
    # 杂项常发：零散工具，模型猜不到组名（提案第 3 节）
    for misc in ('web_fetch', 'delete_paths', 'move_paths', 'remind'):
        assert misc in names, misc
    # 8 个整组被延迟
    for deferred in ('schedule_cron', 'remember', 'read_document',
                     'create_worktree', 'screen_capture', 'clipboard_read',
                     'find_sessions', 'spawn_teammate'):
        assert deferred not in names, deferred


def test_searching_a_group_activates_it_and_it_stays():
    """搜到即激活，并且下一轮还在（会话内粘性）。"""
    before = _names(tools.TOOLS)
    assert 'schedule_cron' not in before

    hit = tool_discovery.search('定时任务 cron')
    assert 'schedule_cron' in _names(hit)

    after = _names(tools.TOOLS)
    assert 'schedule_cron' in after and 'list_crons' in after
    # 粘性：再查一次不会丢
    tool_discovery.search('无关的查询 xyz')
    assert 'schedule_cron' in _names(tools.TOOLS)


def test_search_returns_schemas_not_just_names():
    """只给名字，模型还得猜参数——返回的必须是完整 schema。"""
    hit = tool_discovery.search('memory 记忆')
    schema = next(s for s in hit if s['function']['name'] == 'remember')
    assert schema['function'].get('parameters', {}).get('properties')


def test_search_by_tool_name_and_by_group_name():
    assert 'remember' in _names(tool_discovery.search('remember'))
    tool_discovery.reset()
    assert 'remember' in _names(tool_discovery.search('memory'))
    tool_discovery.reset()
    assert 'remember' in _names(tool_discovery.search('记忆'))


def test_unknown_query_activates_nothing_and_says_so():
    hit = tool_discovery.search('完全不存在的工具 zzz')
    assert hit == []
    assert '反正不存在' not in _names(tools.TOOLS)


def test_deferred_tools_stay_callable():
    """关键不变量：不发 schema ≠ 不能用。

    策略层、审批、审计都看 `tool_registry`，发现机制只影响"模型看到什么"。
    """
    for name in ('schedule_cron', 'remember', 'read_document'):
        assert name in tools.tool_registry, name
        assert name not in _names(tools.TOOLS), name


def test_fingerprint_follows_the_active_surface():
    """指纹必须跟着活跃集变：否则缓存归因会说"工具表凭空变了"。"""
    import harness

    before = harness._tools_fingerprint()
    tool_discovery.search('cron')
    assert harness._tools_fingerprint() != before


def test_other_entry_points_get_the_full_capability_surface():
    """子代理不用发现机制：它们按需构造自己的工具面。"""
    surface = _names(tools.capability_surface())
    for name in ('schedule_cron', 'remember', 'spawn_teammate'):
        assert name in surface, name


def test_openai_surface_omits_discovery_only_tools_from_base():
    """没有 tool_search 时模型就够不到被延迟的工具——所以它必须常发。"""
    names = _names(tools.TOOLS)
    assert 'tool_search' in names


def test_discovery_saves_a_quarter_of_the_schema_payload():
    """验收指标：基础面至少比完整面小 2,500 字符（≈625 tokens）。"""
    import json

    full = len(json.dumps([s['function'] for s in tools.capability_surface()],
                          ensure_ascii=False))
    base = len(json.dumps([s['function'] for s in tools.TOOLS], ensure_ascii=False))
    assert full - base >= 2_500, f'只省了 {full - base} 字符'


def test_every_deferred_group_is_reachable():
    """每个被延迟的工具都必须能被至少一次搜索命中（否则就是真丢功能）。"""
    deferred = {name for group in tool_discovery.GROUPS.values() for name in group}
    for name in sorted(deferred):
        tool_discovery.reset()
        assert name in _names(tool_discovery.search(name)), name
