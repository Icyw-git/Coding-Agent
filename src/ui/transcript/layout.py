"""块 → 行 的布局与视口切片（虚拟化的核心）。

只做纯计算：给定宽度、块版本，缓存每块的行；滚动/换宽度时只重算受影响的块。
真正的 Textual 渲染由 view 层把 `visible()` 的行画到单个自绘控件上。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from rich.text import Text

from ui.transcript.model import Block, ConversationModel

Line = str | Text
RenderBlock = Callable[[Block, int], Sequence[Line]]


@dataclass
class LayoutEntry:
    block_id: str
    version: int
    width: int
    lines: list[Line]
    start: int = 0

    @property
    def height(self) -> int:
        return len(self.lines)


class TranscriptLayout:
    """维护块的高度/起始行；支持按行切片与 block 命中测试。"""

    def __init__(self, render_block: RenderBlock) -> None:
        self._render_block = render_block
        self._entries: list[LayoutEntry] = []
        self._width = 0
        self._total = 0
        self._render_calls = 0     # 诊断/基准用：确认没有整表重绘
        self._revision = -1
        self._index: dict[str, int] = {}

    @property
    def total_height(self) -> int:
        return self._total

    @property
    def width(self) -> int:
        return self._width

    @property
    def render_calls(self) -> int:
        return self._render_calls

    def entries(self) -> list[LayoutEntry]:
        return list(self._entries)

    def update(self, model: ConversationModel, width: int) -> None:
        """同步模型：只重渲染新增/变更的块，滚动时不做任何重算。"""
        width = max(1, int(width))
        if model.revision == self._revision and width == self._width:
            return
        full = width != self._width or len(model) < len(self._entries)
        if full:
            self._entries = []
            self._index = {}
        dirty = model.take_dirty()   # 无论是否全量都清空脏集合
        blocks = model.snapshot()

        if not full and len(blocks) == len(self._entries):
            changed = [bid for bid in dirty if bid in self._index]
            if len(changed) == len(dirty):
                for block_id in changed:
                    index = self._index[block_id]
                    block = blocks[index]
                    if self._entries[index].version == block.version:
                        continue
                    lines = list(self._render_block(block, width))
                    self._render_calls += 1
                    self._entries[index] = LayoutEntry(
                        block.id, block.version, width, lines or [''])
                self._revision = model.revision
                self._rebuild_starts()
                return
            full = True

        if not full:
            # 追加路径：模型只会在尾部新增，避免整表重建。
            new_blocks = blocks[len(self._entries):]
            for block in new_blocks:
                lines = list(self._render_block(block, width))
                self._render_calls += 1
                entry = LayoutEntry(block.id, block.version, width, lines or [''])
                self._index[block.id] = len(self._entries)
                self._entries.append(entry)
            # 同时可能有旧块被修改（append + 其它事件同帧）
            for block_id in dirty:
                if block_id in self._index:
                    index = self._index[block_id]
                    block = blocks[index]
                    if self._entries[index].version == block.version:
                        continue
                    lines = list(self._render_block(block, width))
                    self._render_calls += 1
                    self._entries[index] = LayoutEntry(
                        block.id, block.version, width, lines or [''])
            self._width = width
            self._revision = model.revision
            self._rebuild_starts()
            return

        # 全量重建（宽度变化 / 清空 / 顺序变化）
        entries: list[LayoutEntry] = []
        for block in blocks:
            lines = list(self._render_block(block, width))
            self._render_calls += 1
            entries.append(LayoutEntry(block.id, block.version, width,
                                       lines or ['']))
        self._entries = entries
        self._index = {entry.block_id: i for i, entry in enumerate(entries)}
        self._width = width
        self._revision = model.revision
        self._rebuild_starts()

    def _rebuild_starts(self) -> None:
        start = 0
        for entry in self._entries:
            entry.start = start
            start += entry.height
        self._total = start

    def visible(self, scroll_y: int, height: int,
                overscan: int = 0) -> list[Line]:
        """返回 [scroll_y, scroll_y+height) 的行；越界部分返回空白。"""
        top = max(0, int(scroll_y) - max(0, int(overscan)))
        bottom = max(top, int(scroll_y) + max(0, int(height)) + max(0, int(overscan)))
        lines: list[Line] = []
        for entry in self._entries:
            if entry.start + entry.height <= top:
                continue
            if entry.start >= bottom:
                break
            lo = max(0, top - entry.start)
            hi = min(entry.height, bottom - entry.start)
            lines.extend(entry.lines[lo:hi])
        return lines

    def block_at(self, line_index: int) -> str | None:
        """行号 → block id（点击展开用）。"""
        if line_index < 0 or line_index >= self._total:
            return None
        lo, hi = 0, len(self._entries) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            entry = self._entries[mid]
            if line_index < entry.start:
                hi = mid - 1
            elif line_index >= entry.start + entry.height:
                lo = mid + 1
            else:
                return entry.block_id
        return None

    def line_at(self, line_index: int) -> Line | None:
        """单行读取（视口按需渲染用）；越界返回 None。"""
        if line_index < 0 or line_index >= self._total:
            return None
        result = self.block_at(line_index)
        if result is None:
            return None
        index = self._index.get(result)
        if index is None:
            return None
        entry = self._entries[index]
        return entry.lines[line_index - entry.start]

    def max_scroll(self, viewport_height: int) -> int:
        return max(0, self._total - max(0, int(viewport_height)))
