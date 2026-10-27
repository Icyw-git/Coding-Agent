"""逐会话的记忆读写开关。

为什么需要
----------
``HARNESS_LOAD_MEMORIES`` / ``HARNESS_MEMORY_RECALL`` 都是**进程级**环境变量。
没有「这次聊天别记我」或「这次别读我的记忆」的能力。Codex 的 ``/memories`` 是
逐会话控制，且**不改全局设置**：

    Chat-level choices let you decide whether the current chat can use existing
    memories and whether Codex can use the chat to generate future memories.
    Chat-level choices **don't change your global memory settings**.

两种状态各管一头
----------------
- ``use``：能否把已有记忆注入本会话（读）
- ``contribute``：本会话结束后能否被抽取成记忆（写）

分开的理由：调试记忆功能时常需要"只读"（用已有记忆但不污染它）或"只写"
（一次性引导会话，让它学习但不受旧记忆影响）。Codex 明确说了这对非对称组合
有用，我们照做。

状态存在哪
----------
ContextVar + 会话事件流双写。事件流是持久源（重启后仍生效），ContextVar 是
热路径（每轮召回都要读，不能每次都解析 jsonl）。

**不做全局开关**：这一档的语义就是"这次"，改全局是另一件事（那已经有环境变量）。
"""
from __future__ import annotations

import os
from contextvars import ContextVar

#: 会话级状态：``(use, contribute)``。None 表示跟随全局环境变量。
_state: ContextVar[tuple | None] = ContextVar('memory_scope', default=None)

#: 事件类型名。恢复时按「最后一次出现」取值，和 session_title 同一套语义。
EVENT = 'memory_scope'

MODES = ('on', 'off', 'readonly', 'writeonly')


def _globals() -> tuple[bool, bool]:
    """全局默认值（进程级环境变量）。

    ``HARNESS_MEMORY_RECALL=off`` 的语义是「不读记忆」，但它只关了读、没关写——
    直接映射会得到 ``writeonly``（能学不读），这显然不是用户想表达的。它历史上
    就是全局的「关掉记忆召回」，所以这里把它归一成 ``off``：读写都停。
    需要真正的只写请显式用 ``/memories writeonly``。
    """
    load = os.getenv('HARNESS_LOAD_MEMORIES', '1').strip().lower() not in ('0', 'off', 'false', 'no')
    recall = os.getenv('HARNESS_MEMORY_RECALL', 'local').strip().lower()
    contribute = os.getenv('HARNESS_MEMORY_CONTRIBUTE', '1').strip().lower() not in ('0', 'off', 'false', 'no')
    if not load or recall == 'off':
        return (False, False)
    return (True, contribute)


def reset() -> None:
    _state.set(None)


def set_mode(mode: str) -> tuple[bool, bool]:
    """设置本会话的记忆模式，返回 ``(use, contribute)``。

    on       : 读 + 写（默认）
    off      : 都不做
    readonly : 读已有，但不产生新记忆
    writeonly: 不被旧记忆影响，但本会话会被抽取
    """
    key = str(mode or '').strip().lower()
    if key not in MODES:
        raise ValueError(f'mode must be one of {", ".join(MODES)}; got {mode!r}')
    pair = {
        'on': (True, True),
        'off': (False, False),
        'readonly': (True, False),
        'writeonly': (False, True),
    }[key]
    _state.set(pair)
    return pair


def restore(mode: str) -> None:
    """从会话事件流恢复；未知值忽略（旧会话没有这个事件）。"""
    key = str(mode or '').strip().lower()
    if key in MODES:
        set_mode(key)


def restore_from_session(session_store) -> None:
    """会话打开时从事件流恢复模式（ContextVar 不跨进程）。

    与 ``tool_todos.restore`` 同一套语义：取最后一次出现的值。失败不动 상태——
    恢复不了就跟随全局默认，比把用户静默切成「不记」安全。
    """
    reset()
    if session_store is None:
        return
    try:
        events = session_store.read_events()
    except Exception:
        return
    last = next((event for event in reversed(events)
                 if event.get('type') == EVENT), None)
    if last is not None:
        restore(last.get('mode'))


def mode() -> str:
    """当前有效模式（会话级优先，否则推导自全局）。"""
    current = _state.get()
    if current is None:
        use, contribute = _globals()
        if use and contribute:
            return 'on'
        if not use and not contribute:
            return 'off'
        return 'readonly' if use else 'writeonly'
    use, contribute = current
    if use and contribute:
        return 'on'
    if not use and not contribute:
        return 'off'
    return 'readonly' if use else 'writeonly'


def use_enabled() -> bool:
    """本会话是否应注入已有记忆。"""
    current = _state.get()
    return current[0] if current is not None else _globals()[0]


def contribute_enabled() -> bool:
    """本会话结束后是否应被抽取成记忆。"""
    current = _state.get()
    return current[1] if current is not None else _globals()[1]


def describe() -> str:
    """一行给人看的说明（`/status` 与命令回显用）。"""
    labels = {
        'on': '读+写（默认）',
        'off': '不读不写',
        'readonly': '只读（不产生新记忆）',
        'writeonly': '只写（不受旧记忆影响）',
    }
    return labels.get(mode(), mode())
