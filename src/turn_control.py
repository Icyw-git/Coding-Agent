"""跨线程的回合状态：取消请求与「回合进行中」标志。

输入线程在 Ctrl-C 时只置取消位，而不用把 KeyboardInterrupt 塞进消息队列：
否则回合结束后会多出一次无意义的「再按一次 Ctrl-C」提示。主循环在流式回调里
检查取消位并抛出 KeyboardInterrupt，让当前回合尽快收敛。
"""
from __future__ import annotations

import threading

_CANCEL = threading.Event()
_ACTIVE = threading.Event()


def begin_turn() -> None:
    _CANCEL.clear()
    _ACTIVE.set()


def end_turn() -> None:
    _ACTIVE.clear()
    _CANCEL.clear()


def active() -> bool:
    return _ACTIVE.is_set()


def request_cancel() -> None:
    _CANCEL.set()


def cancel_requested() -> bool:
    return _CANCEL.is_set()
