"""虚拟对话视口：只把可见行画到 Textual，不按消息创建控件。

基于 `textual.scroll_view.ScrollView`：设置 `virtual_size` 为总行数，
`render_line(y)` 用 `scroll_y + y` 取布局中的单行，行缓存随布局 revision 失效。
"""
from __future__ import annotations

from rich.segment import Segment
from rich.text import Text
from textual import events
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip

from ui.transcript.layout import Line, TranscriptLayout
from ui.transcript.model import ConversationModel


class VirtualTranscript(ScrollView):
    """把 `ConversationModel + TranscriptLayout` 渲染成可滚动的行视口。"""

    def __init__(self, model: ConversationModel, layout: TranscriptLayout,
                 *, follow_end: bool = True) -> None:
        super().__init__()
        self.model = model
        self.transcript_layout = layout
        self.follow_end = follow_end
        self._line_cache: dict[tuple[int, int, int], Strip] = {}
        self._cached_revision = -1

    @property
    def content_line_count(self) -> int:
        return self.transcript_layout.total_height

    def _content_width(self) -> int:
        return max(1, self.scrollable_content_region.width or self.size.width or 80)

    def refresh_layout(self) -> None:
        """模型或宽度变化后重算布局并刷新；保持“贴底”行为。"""
        follow = self.follow_end and self.is_vertical_scroll_end
        self.transcript_layout.update(self.model, self._content_width())
        self._line_cache.clear()
        self._cached_revision = self.model.revision
        self.virtual_size = Size(self._content_width(),
                                 max(1, self.transcript_layout.total_height))
        if follow:
            self.scroll_end(animate=False)
        self.refresh()

    def on_resize(self, event: events.Resize) -> None:
        self.refresh_layout()

    def _strip_for(self, line_index: int, width: int) -> Strip:
        key = (line_index, width, self.model.revision)
        cached = self._line_cache.get(key)
        if cached is not None:
            return cached
        line: Line | None = self.transcript_layout.line_at(line_index)
        if line is None:
            strip = Strip.blank(width, self.rich_style)
        else:
            if isinstance(line, Text):
                strip = Strip(line.render(self.app.console))
            else:
                strip = Strip([Segment(str(line))])
            strip = strip.adjust_cell_length(width, self.rich_style)
        if len(self._line_cache) > 4000:      # 视口缓存有界，防止长会话膨胀
            self._line_cache.clear()
        self._line_cache[key] = strip
        return strip

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        if scroll_x:
            return Strip.blank(self.size.width, self.rich_style)
        return self._strip_for(scroll_y + y, self.size.width)
