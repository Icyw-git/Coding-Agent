"""块 → 行 的布局与视口切片（虚拟化的核心）。

只做纯计算：给定宽度、块版本，缓存每块的行；滚动/换宽度时只重算受影响的块。
真正的 Textual 渲染由 view 层把 `visible()` 的行画到单个自绘控件上。
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Sequence

from rich.text import Text

from ui.transcript.model import Block, ConversationModel

Line = str | Text
RenderBlock = Callable[[Block, int], Sequence[Line]]


@dataclass(slots=True)
class LayoutEntry:
    block_id: str
    version: int
    width: int
    lines: list[Line] | None
    start: int = 0
    height: int = 0


# 每块的行文本是模型 payload 之外的第二份副本；只保留最近访问的窗口，
# 离屏块保留高度、需要时再渲染。这样 10k 块长会话不会把完整历史复制进布局。
DEFAULT_LINE_CACHE_LINES = 6000


class TranscriptLayout:
    """维护块的高度/起始行；支持按行切片与 block 命中测试。"""

    def __init__(self, render_block: RenderBlock,
                 line_cache_limit: int = DEFAULT_LINE_CACHE_LINES) -> None:
        self._render_block = render_block
        self._entries: list[LayoutEntry] = []
        self._width = 0
        self._total = 0
        self._render_calls = 0     # 诊断/基准用：确认没有整表重绘
        self._revision = -1
        self._index: dict[str, int] = {}
        self._model: ConversationModel | None = None
        self._recent: OrderedDict[str, None] = OrderedDict()
        self._cached_lines = 0
        self._trim_scan = 0
        self._line_cache_limit = max(0, int(line_cache_limit))

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

    def _entry(self, block: Block, width: int) -> LayoutEntry:
        lines = list(self._render_block(block, width)) or ['']
        self._render_calls += 1
        self._cached_lines += len(lines)
        return LayoutEntry(block.id, block.version, width, lines,
                           height=len(lines))

    def _touch(self, block_id: str) -> None:
        self._recent.pop(block_id, None)
        self._recent[block_id] = None

    def _materialize(self, entry: LayoutEntry) -> list[Line]:
        if entry.lines is not None:
            self._touch(entry.block_id)
            return entry.lines
        block = self._model.get(entry.block_id) if self._model is not None else None
        if block is None:
            entry.lines = ['']
            entry.height = 1
        else:
            entry.lines = list(self._render_block(block, entry.width)) or ['']
            entry.version = block.version
            entry.height = len(entry.lines)
            self._render_calls += 1
        self._cached_lines += entry.height
        self._touch(entry.block_id)
        return entry.lines

    def _drop(self, entry: LayoutEntry) -> None:
        if entry.lines is None:
            return
        self._cached_lines -= len(entry.lines)
        entry.lines = None
        self._recent.pop(entry.block_id, None)

    def _trim_lines(self, protect: set[str] | None = None) -> None:
        if self._cached_lines <= self._line_cache_limit:
            return
        protect = protect or set()
        candidates = list(self._recent)
        for block_id in candidates:
            if self._cached_lines <= self._line_cache_limit:
                break
            if block_id in protect:
                continue
            entry = self._entries[self._index[block_id]]
            self._drop(entry)
        # 新布局尚未被访问时，从最旧块开始淘汰。游标避免全量构建时
        # 每个新块都从第 0 项重新扫描，导致 O(n^2)。
        if self._cached_lines > self._line_cache_limit:
            while (self._cached_lines > self._line_cache_limit
                   and self._trim_scan < len(self._entries)):
                entry = self._entries[self._trim_scan]
                self._trim_scan += 1
                if entry.block_id not in protect:
                    self._drop(entry)

    def update(self, model: ConversationModel, width: int) -> None:
        """同步模型：只重渲染新增/变更的块，滚动时不做任何重算。"""
        width = max(1, int(width))
        if model.revision == self._revision and width == self._width:
            return
        full = width != self._width or len(model) < len(self._entries)
        if full:
            self._entries = []
            self._index = {}
            self._recent.clear()
            self._cached_lines = 0
            self._trim_scan = 0
        self._model = model
        dirty = model.take_dirty()   # 无论是否全量都清空脏集合
        blocks = model.snapshot()

        if not full and len(blocks) == len(self._entries):
            changed = [block_id for block_id in dirty if block_id in self._index]
            if len(changed) == len(dirty):
                changed_indexes = []
                for block_id in changed:
                    index = self._index[block_id]
                    block = blocks[index]
                    if self._entries[index].version == block.version:
                        continue
                    changed_indexes.append(index)
                    old_entry = self._entries[index]
                    self._cached_lines -= (old_entry.height
                                           if old_entry.lines is not None else 0)
                    entry = self._entry(block, width)
                    entry.start = old_entry.start
                    self._entries[index] = entry
                    if self._cached_lines > self._line_cache_limit:
                        self._trim_lines()
                self._revision = model.revision
                if changed_indexes:
                    self._reindex_from(min(changed_indexes))
                else:
                    self._trim_lines()
                return
            full = True

        if not full:
            # 追加路径：模型只会在尾部新增，避免整表重建。
            old_count = len(self._entries)
            changed_indexes = []
            for block in blocks[old_count:]:
                entry = self._entry(block, width)
                self._index[block.id] = len(self._entries)
                self._entries.append(entry)
                if self._cached_lines > self._line_cache_limit:
                    self._trim_lines()
            # 同时可能有旧块被修改（append + 其它事件同帧）
            for block_id in dirty:
                if block_id not in self._index:
                    continue
                index = self._index[block_id]
                block = blocks[index]
                if self._entries[index].version == block.version:
                    continue
                changed_indexes.append(index)
                old = self._entries[index]
                if old.lines is not None:
                    self._cached_lines -= old.height
                entry = self._entry(block, width)
                entry.start = old.start
                self._entries[index] = entry
                if self._cached_lines > self._line_cache_limit:
                    self._trim_lines()
            self._width = width
            self._revision = model.revision
            self._reindex_from(min(changed_indexes) if changed_indexes else old_count)
            return

        # 全量重建（宽度变化 / 清空 / 顺序变化）；边建边淘汰，避免峰值
        # 暂时堆出整份历史的所有行。
        entries: list[LayoutEntry] = []
        self._entries = entries
        for block in blocks:
            entries.append(self._entry(block, width))
            if self._cached_lines > self._line_cache_limit:
                self._trim_lines()
        self._index = {entry.block_id: i for i, entry in enumerate(entries)}
        self._width = width
        self._revision = model.revision
        self._reindex_from(0)

    def _reindex_from(self, index: int = 0) -> None:
        """只从变更位置重算 starts；流式更新尾部块时避免每次 O(n)。"""
        if index >= len(self._entries):
            return
        if index:
            previous = self._entries[index - 1]
            start = previous.start + previous.height
        else:
            start = 0
        for entry in self._entries[index:]:
            entry.start = start
            start += entry.height
        self._total = start
        self._trim_lines()

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
            lines.extend(self._materialize(entry)[lo:hi])
        self._trim_lines({entry.block_id for entry in self._entries
                          if entry.start < bottom and entry.start + entry.height > top})
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
        lines = self._materialize(entry)
        self._trim_lines({entry.block_id})
        return lines[line_index - entry.start]

    def max_scroll(self, viewport_height: int) -> int:
        return max(0, self._total - max(0, int(viewport_height)))
