"""工具发现：把低频工具的 schema 延迟到"真的要用"时才发给模型。

为什么需要
----------
默认工具面 5,762 tokens 里，核心 10 个只占 1,415，其余 41 个占 4,347（75%）。而
单次会话里大多数工具一次都不会被调用——它们的 schema 每轮都在付钱。
数据与取舍见 docs/TOOL_DISCOVERY_PROPOSAL.md。

三条硬约束
----------
1. **只影响"模型看到什么"。** `tool_registry` 不动，策略层 / 审批 / 审计照旧看全量。
2. **搜到就粘住。** 会话（进程）内一旦激活，后续请求都带着它——不能让工具忽隐忽现。
3. **每个被延迟的工具都要有确定的发现路径**（组名 / 工具名 / 中文关键词），
   否则等于静默删功能。`tests/test_tool_discovery.py` 逐个验证。

刻意不做的事
------------
不按"上一轮对话"猜意图。同一句话在不同上下文里会得到不同工具面，行为不可复现，
调试时也说不清"它为什么没看见那个工具"。
"""
from __future__ import annotations

import os

#: 开关。**默认关闭**：延迟加载会让 tools 在会话中途变化，而 tools 位于 prompt
#: 缓存前缀的最前面——一变，整个前缀从 96% 命中掉到 0%，那一轮多付 ≈38k tokens。
#: 省下的是 3,083 tokens/轮（且因为在缓存里，实际只值 ≈419/轮），盈亏平衡 ≈91 轮，
#: 而实测平均会话只有 42 轮。数据见 docs/PERFORMANCE_PROFILE.md「工具表」一节。
#:
#: 保留开关给**没有 prompt 缓存**的部署（本地模型、自建推理），那里这个优化是真赚。
ENV = 'HARNESS_TOOL_DISCOVERY'


def enabled() -> bool:
    raw = (os.getenv(ENV) or '').strip().casefold()
    return raw in ('1', 'true', 'yes', 'on', 'enabled')


#: 每轮都发的核心工具：几乎所有工作都要用。
BASE_TOOLS = frozenset({
    'bash', 'read', 'write', 'edit', 'apply_patch',
    'grep', 'glob', 'ls', 'todo_write', 'todo_read',
})

#: 整组延迟的工具。键是组名，同时用作搜索关键词。
GROUPS: dict[str, tuple[str, ...]] = {
    'cron': ('schedule_cron', 'list_crons', 'cancel_cron'),
    'memory': ('remember', 'forget', 'recall'),
    'documents': ('read_document', 'render_document', 'view_image'),
    'tasks': ('task', 'list_tasks', 'get_task', 'claim_task', 'complete_task',
              'spawn_teammate', 'send_message', 'check_inbox',
              'request_plan', 'review_plan', 'request_shutdown'),
    'sessions': ('find_sessions', 'rename_session', 'revoke_permissions'),
    'screen': ('screen_capture', 'screen_ocr'),
    'worktree': ('create_worktree', 'remove_worktree', 'keep_worktree'),
    'clipboard': ('clipboard_read', 'clipboard_write'),
}

#: 除组名外还能命中的词（中英各半：模型的查询往往是中文）。
GROUP_KEYWORDS: dict[str, tuple[str, ...]] = {
    'cron': ('定时', '提醒', '周期', 'schedule', 'recurring', 'daily', 'reminder'),
    'memory': ('记忆', '偏好', '记住', 'memory', 'preference', 'remember'),
    'documents': ('文档', 'pdf', 'docx', 'xlsx', '图片', '文档页', 'document', 'image'),
    'tasks': ('任务', '子代理', '队友', '团队', 'task', 'subagent', 'teammate', 'team'),
    'sessions': ('会话', '历史', 'session', 'history', 'rename'),
    'screen': ('截屏', '屏幕', '识别文字', '截图', 'screenshot', 'ocr', 'screen'),
    'worktree': ('工作树', '分支', 'worktree', 'branch'),
    'clipboard': ('剪贴板', '复制粘贴', 'clipboard', 'paste', 'copy'),
}

#: 已激活的工具名（进程内粘性）。只在"发现"维度生效，不影响能否调用。
_activated: set[str] = set()

#: 激活状态变化时的回调（tools 用它重建 TOOLS）。
_listener = None


def set_listener(callback) -> None:
    """注册"激活集变了"的回调。tools 在导入期接上，用来重建自己的 TOOLS。"""
    global _listener
    _listener = callback


def _notify() -> None:
    if _listener is not None:
        _listener()


def deferred_tools() -> frozenset[str]:
    """所有被延迟的工具名。"""
    return frozenset(name for group in GROUPS.values() for name in group)


def searchable() -> bool:
    """`tool_search` 这个工具本身要不要发。

    关闭时也不发：没有可发现的东西，它只会占 schema。
    """
    return enabled()


def visible(name: str) -> bool:
    """这个工具现在该不该出现在请求的工具面里。

    核心工具、`tool_search` 本身、以及**不属于任何组**的工具（杂项）永远可见——
    杂项零散、模型猜不到组名，延迟它们等于让功能消失。
    """
    if not enabled():
        return True          # 关闭时一切照旧（完整能力面）
    if name in BASE_TOOLS or name == 'tool_search':
        return True
    if name not in deferred_tools():
        return True
    return name in _activated


def _matches(query: str, group: str, names: tuple[str, ...]) -> bool:
    text = str(query or '').casefold().strip()
    if not text:
        return False
    if group in text or text in group:
        return True
    if any(word.casefold() in text for word in GROUP_KEYWORDS.get(group, ())):
        return True
    return any(name.casefold() in text for name in names)


def search(query: str, schemas=None) -> list[dict]:
    """按查询激活工具，返回**完整 schema**（不只是名字）。

    只给名字的话，模型还得再猜一次参数——等于把一次往返变成两次。

    ``schemas`` 由调用方（tools）注入，避免这里 import tools 造成环。
    """
    if schemas is None:
        import tools as tools_module

        schemas = tools_module.capability_surface()
    by_name = {schema['function']['name']: schema for schema in schemas}
    found: list[dict] = []
    newly: list[str] = []
    for group, names in GROUPS.items():
        if not _matches(query, group, names):
            continue
        for name in names:
            schema = by_name.get(name)
            if schema is None or name in found_names(found):
                continue
            found.append(schema)
            if name not in _activated:
                newly.append(name)
    if newly:
        _activated.update(newly)
        _notify()
    return found


def found_names(schemas: list[dict]) -> set[str]:
    return {schema['function']['name'] for schema in schemas}


def active_extra() -> frozenset[str]:
    """已激活的额外工具（调试 / `/status` 用）。"""
    return frozenset(_activated)


def reset() -> None:
    """清空激活集（测试与 `/tool-reset` 用）。"""
    if _activated:
        _activated.clear()
        _notify()
