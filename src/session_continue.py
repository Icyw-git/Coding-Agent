"""跨渠道会话接续：你在哪里停下，就在哪里接上。

为什么需要
----------
现在飞书、TUI、API 各自绑定一个会话。在 TUI 里聊到一半，掏出手机发消息，飞书
会**新建**一个会话——上下文断掉，得手动 ``/use`` 切（而在手机上根本不知道切到
哪个）。dots 的说法是「上下文跟着你走，在 ChatGPT、Slack、Teams 都有」。

这里做的是**同机跨渠道**那一层（不要跨机器：Codex 明确说 memories 不跨机器，
我们也不需要，那属于 G1 多机协同）。

设计要点
--------
1. **只提供"最近活跃"，不改绑定**：渠道本来就有自己的当前会话，接续是"在没有
   绑定或刚开新会话时"才生效。硬改已有绑定会让人在两头同时操作时莫名跳会话。
2. **时间窗**：太旧的会话不该被"接上"。默认 8 小时——隔夜再发消息应该开新话题，
   而不是闯进昨天的对话。
3. **排除已结束的会话**：归档/删除的会话不能接续。
4. **跨渠道必须可见**：接上时明确告诉用户"接续的是 3 小时前在终端聊的 <标题>"，
   否则用户会以为自己发错了地方。静默接续比不接续更糟。
"""
from __future__ import annotations

import os
import time

import settings

#: 默认接续时间窗（秒）。8 小时：够一次工作时段，又不会跨夜。
DEFAULT_WINDOW_S = 8 * 3600

ENV_WINDOW = 'HARNESS_CONTINUE_WINDOW_S'


def enabled() -> bool:
    return settings.flag('HARNESS_CONTINUE_SESSION', True)


def window_seconds() -> int:
    raw = os.getenv(ENV_WINDOW, '').strip()
    if not raw:
        return DEFAULT_WINDOW_S
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_WINDOW_S
    return value if value > 0 else DEFAULT_WINDOW_S


def _summaries(sessions_dir):
    """遍历会话文件，取 (path, summary)；坏文件跳过。"""
    from pathlib import Path

    import session_store
    directory = Path(sessions_dir)
    if not directory.is_dir():
        return
    for path in directory.glob('session_*.jsonl'):
        try:
            store = session_store.SessionEventStore.open(path)
            summary = store.session_summary()
        except Exception:
            continue
        yield path, summary


def recent_session(sessions_dir, *, exclude_id: str = '',
                   window_s: int | None = None) -> dict | None:
    """返回时间窗内最近活跃的会话摘要；没有合适的返回 None。

    ``exclude_id`` 用来排除当前会话：自己接自己没意义，还会把"新会话"重定向回
    旧会话，看起来很怪。
    """
    if not enabled():
        return None
    limit = window_s if window_s is not None else window_seconds()
    cutoff = time.time() - limit
    best = None
    for _path, summary in _summaries(sessions_dir):
        session_id = str(summary.get('id') or '')
        if not session_id or session_id == exclude_id:
            continue
        updated = summary.get('updated') or 0
        if not isinstance(updated, (int, float)) or updated < cutoff:
            continue
        if best is None or updated > (best.get('updated') or 0):
            best = summary
    return best


def describe(summary: dict, now: float | None = None) -> str:
    """给用户看的一句接续说明。

    必须带上「多久以前」和「哪个渠道/标题」：静默接续会让用户以为自己发错了
    地方，比不接续更糟。
    """
    title = str(summary.get('title') or '新会话')
    updated = summary.get('updated') or 0
    when = _relative(now if now is not None else time.time(), updated)
    return f'{when}的会话「{title}」'


def _relative(now: float, then: float) -> str:
    if not then:
        return '稍前'
    delta = max(0, int(now - then))
    if delta < 60:
        return '刚刚'
    if delta < 3600:
        return f'{delta // 60} 分钟前'
    if delta < 86400:
        return f'{delta // 3600} 小时前'
    return f'{delta // 86400} 天前'
