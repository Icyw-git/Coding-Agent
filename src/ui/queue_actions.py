"""队列动作：steering/follow-up 队列的取回、选择器与删除。

队列数据仍由 `HarnessApp` 持有（worker 直接从 `_pending` 取头部），这里只
负责和编辑器、选择器、确认弹窗之间的交互，避免 App 文件继续膨胀。
"""
from __future__ import annotations

from functools import partial

from ui.modals.confirm import ConfirmModal
from ui.modals.queue import QueueModal


class QueueActionsMixin:
    """把两条队列映射到编辑器与选择器；不拥有队列本身。"""

    def take_last_queued(self) -> None:
        item = self._pending.take_last()
        if item is None or self._composer is None:
            return
        self._programmatic_text = item
        self._composer.text = item
        self._composer.cursor_location = (0, len(item))
        self._refresh_queue()

    def dequeue_all(self) -> None:
        """Esc / 选择器 Ctrl+R：把 steering 和 follow-up 全部取回输入框。"""
        if self._composer is None:
            return
        items = self._steering.drain() + self._pending.drain()
        items = [item for item in items if item]
        if not items:
            self._notice('No queued messages to restore.')
            return
        self._restore_to_composer(items)
        self._notice(f'Restored {len(items)} queued message(s) to editor.')

    def _restore_to_composer(self, items: list[str]) -> None:
        """保留当前草稿，把消息按顺序接在后面，并同步队列状态。"""
        if self._composer is None:
            return
        current = self._composer.text.strip()
        text = chr(10).join(items)
        if current:
            text = current + chr(10) + text
        self._programmatic_text = text
        self._composer.text = text
        self._composer.cursor_location = (0, len(text))
        self._composer.focus()
        self._refresh_queue()

    def _queued_rows(self) -> list[dict]:
        """合并两个队列：值带队列名和位置，另存预期文本防消费后误取。"""
        rows = []
        for name, pending, prefix in (
                ('steer', self._steering, 'steer'),
                ('next', self._pending, 'next')):
            for index, text in enumerate(pending.snapshot()):
                preview = text.replace(chr(10), ' ')[:60]
                rows.append({
                    'value': f'{name}:{index}', 'queue': name,
                    'index': index, 'text': text,
                    'label': f'{prefix}  {preview}',
                    'search': text,
                })
        return rows

    def open_queue_picker(self) -> None:
        """Ctrl+G / Alt+↑ / `/dequeue`：单条直接取回，多条用选择器管理。"""
        rows = self._queued_rows()
        if not rows:
            self._notice('No queued messages to restore.')
            return
        if len(rows) == 1:
            self._restore_queue_row(rows[0])
            return
        self._queue_row_cache = {row['value']: row for row in rows}
        self.push_screen(QueueModal(rows), self._on_queue_action)

    def _queue_for(self, row: dict):
        return self._steering if row['queue'] == 'steer' else self._pending

    def _restore_queue_row(self, row: dict) -> None:
        text = self._queue_for(row).take_at(row['index'], expected=row['text'])
        if text is None:
            self._notice('队列已变化（消息可能已被消费），请重新打开。')
            return
        self._restore_to_composer([text])
        self._notice('Restored 1 queued message to editor.')

    def _on_queue_action(self, result) -> None:
        if not result:
            return
        action, value = result
        if action == 'all':
            self.dequeue_all()
            return
        row = self._queue_row_cache.get(value)
        if row is None:
            self._notice('队列已变化，请重新打开。')
            return
        if action == 'restore':
            self._restore_queue_row(row)
            return
        if action == 'delete':
            body = ('将删除这条排队消息，不会发送：' + chr(10) * 2
                    + row['text'])
            self.push_screen(
                ConfirmModal('Delete queued message?', body, 'Delete'),
                partial(self._delete_queue_row, row=row))

    def _delete_queue_row(self, confirmed, row: dict) -> None:
        if not confirmed:
            return
        text = self._queue_for(row).take_at(row['index'], expected=row['text'])
        if text is None:
            self._notice('队列已变化（消息可能已被消费），请重新打开。')
            return
        self._refresh_queue()
        preview = text.replace(chr(10), ' ')[:40]
        self._notice(f'Deleted queued message: {preview}')

    def _refresh_queue(self) -> None:
        if self._queue_widget is None:
            return
        steering = self._steering.snapshot()
        followup = self._pending.snapshot()
        items = steering + followup
        if not items:
            self._queue_widget.display = False
            self._queue_widget.update('')
            return
        preview = ' | '.join(
            ('steer:' if index < len(steering) else 'next:')
            + item.replace(chr(10), ' ')[:20]
            for index, item in enumerate(items[:3]))
        more = f' … +{len(items) - 3}' if len(items) > 3 else ''
        self._queue_widget.update(f'⏳ {len(items)} queued: {preview}{more} '
                                  '· Ctrl+G 管理')
        self._queue_widget.display = True
