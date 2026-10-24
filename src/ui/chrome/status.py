"""编辑框边框状态：Working spinner/耗时/队列数，以及滚动到顶自动补历史。

`_tick_border_status` 由 0.2s 定时器驱动；它同时承担「滚到顶部加载上一页」
的触发条件，因为两者都需要读同一个滚动 offset。
"""
from __future__ import annotations

import time

SPINNERS = ('⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')


class StatusMixin:
    """在 composer 边框内显示回合状态，并驱动历史分页。"""

    def _tick_border_status(self) -> None:
        if self._composer is None:
            return
        offset = int(self.view.scroll_offset.y) if self.view.is_attached else 0
        if (not self._busy and self._history_auto_armed
                and self._history_start > 0 and offset <= 1
                and self._history_last_offset > 1):
            self.load_older_history()
        self._history_last_offset = offset
        if not self._busy:
            if self._composer.border_title:
                self._composer.border_title = ''
            return
        elapsed = max(0.0, time.monotonic() - self._turn_started)
        spinner = SPINNERS[int(time.monotonic() * 8) % len(SPINNERS)]
        queued = len(self._pending) + len(self._steering)
        title = f'{spinner} Working {elapsed:4.1f}s'
        if queued:
            title += f' · {queued} queued'
        self._composer.border_title = title
