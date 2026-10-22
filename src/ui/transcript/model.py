"""UI 会话块模型：事件层只写这里，渲染层只读这里。

一块 = 一条用户消息 / 一条助手回复 / 一次工具调用 / 一条提示，而不是一个 widget；
这样 2000 条历史也不会对应 2000 个 Textual 控件。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Block:
    id: str
    kind: str                      # user | assistant | thinking | tool | diff | notice
    payload: dict = field(default_factory=dict)
    version: int = 0               # 内容变化时 +1，布局缓存据此失效
    expanded: bool = False

    def touch(self) -> None:
        self.version += 1


class ConversationModel:
    """线程安全的块列表；变更只做增量和版本号，不重建列表。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._blocks: list[Block] = []
        self._by_id: dict[str, Block] = {}
        self._seq = 0
        self._dirty: set[str] = set()
        self.revision = 0

    def _new_id(self) -> str:
        self._seq += 1
        return f'b{self._seq}-{time.time_ns()}'

    def append(self, kind: str, payload: dict | None = None, *,
               block_id: str | None = None) -> Block:
        with self._lock:
            block = Block(block_id or self._new_id(), kind, dict(payload or {}))
            self._blocks.append(block)
            self._by_id[block.id] = block
            self._dirty.add(block.id)
            self.revision += 1
            return block

    def prepend(self, kind: str, payload: dict | None = None, *,
                block_id: str | None = None) -> Block:
        """把历史页插到当前模型前面；用于滚动到顶部时加载更早消息。"""
        with self._lock:
            block = Block(block_id or self._new_id(), kind, dict(payload or {}))
            self._blocks.insert(0, block)
            self._by_id[block.id] = block
            self._dirty.add(block.id)
            self.revision += 1
            return block

    def update(self, block_id: str, **payload) -> Block | None:
        with self._lock:
            block = self._by_id.get(block_id)
            if block is None:
                return None
            block.payload.update(payload)
            block.touch()
            self._dirty.add(block_id)
            self.revision += 1
            return block

    def append_text(self, block_id: str, delta: str) -> Block | None:
        """流式增量：只改 payload/version，不触发列表重建。"""
        with self._lock:
            block = self._by_id.get(block_id)
            if block is None:
                return None
            block.payload['text'] = block.payload.get('text', '') + delta
            block.touch()
            self._dirty.add(block_id)
            self.revision += 1
            return block

    def set_expanded(self, block_id: str, expanded: bool) -> Block | None:
        with self._lock:
            block = self._by_id.get(block_id)
            if block is None or block.expanded == expanded:
                return block
            block.expanded = expanded
            block.touch()
            self._dirty.add(block_id)
            self.revision += 1
            return block

    def clear(self) -> None:
        with self._lock:
            self._blocks.clear()
            self._by_id.clear()
            self._dirty.clear()
            self.revision += 1

    def take_dirty(self) -> set[str]:
        """取走自上次以来变更的块 id（布局据此做增量更新）。"""
        with self._lock:
            dirty = set(self._dirty)
            self._dirty.clear()
            return dirty

    def snapshot(self) -> list[Block]:
        with self._lock:
            return list(self._blocks)

    def get(self, block_id: str) -> Block | None:
        with self._lock:
            return self._by_id.get(block_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._blocks)

    def __iter__(self):
        return iter(self.snapshot())
