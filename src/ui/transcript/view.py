"""虚拟对话视口：只把可见行画到 Textual，不按消息创建控件。

基于 `textual.scroll_view.ScrollView`：设置 `virtual_size` 为总行数，
`render_line(y)` 用 `scroll_y + y` 取布局中的单行，行缓存随布局 revision 失效。
"""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.selection import Selection
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
        width = self._content_width()
        if self.transcript_layout.width != width and self.text_selection is not None:
            # 选区坐标属于已渲染的行；换宽后清除，避免复制到重排后的其他文字。
            self.screen.clear_selection()
        self.transcript_layout.update(self.model, width)
        self._line_cache.clear()
        self._cached_revision = self.model.revision
        self.virtual_size = Size(self._content_width(),
                                 max(1, self.transcript_layout.total_height))
        if follow:
            self.scroll_end(animate=False)
        self.refresh()

    def on_resize(self, event: events.Resize) -> None:
        self.refresh_layout()

    def get_selection(self, selection: Selection) -> tuple[str, str]:
        """从布局行提取显示文本，坐标是字符索引与文档行号，不是终端列。"""
        start, end = selection
        top = max(0, start.y) if start is not None else 0
        bottom = (min(self.content_line_count, end.y + 1)
                  if end is not None else self.content_line_count)
        lines = []
        for index, line in enumerate(self.transcript_layout.visible(top, bottom - top), top):
            # Rich 的代码块/表格会把行补齐到面板宽度；复制时去掉行尾填充，
            # 保留代码缩进、正文内部空格与跨行换行。
            text = (line.plain if isinstance(line, Text) else str(line)).rstrip(' \t')
            left = max(0, start.x) if start is not None and index == start.y else 0
            right = max(0, end.x) if end is not None and index == end.y else len(text)
            lines.append(text[left:right])
        return '\n'.join(lines).rstrip('\n'), '\n'

    def _strip_for(self, line_index: int, width: int) -> Strip:
        selection = self.text_selection
        span = selection.get_span(line_index) if selection is not None else None
        key = (line_index, width, self.model.revision)
        cached = self._line_cache.get(key)
        if cached is not None and span is None:
            return cached
        line: Line | None = self.transcript_layout.line_at(line_index)
        if line is None:
            strip = Strip.blank(width, self.rich_style)
        else:
            text = line.copy() if isinstance(line, Text) else Text(str(line))
            if span is not None:
                start, end = span
                text.stylize(self.selection_style, start, None if end == -1 else end)
            # Textual 的鼠标命中依赖 offset 元数据，并自行处理中文/emoji 的宽字符。
            # 先标记真实内容，再补齐视口空白；复制不包含右侧的屏幕填充。
            strip = Strip(text.render(self.app.console)).apply_offsets(0, line_index)
            strip = strip.adjust_cell_length(width, self.rich_style)
        if span is None:
            if len(self._line_cache) > 4000:      # 视口缓存有界，防止长会话膨胀
                self._line_cache.clear()
            self._line_cache[key] = strip
        return strip

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        if scroll_x:
            return Strip.blank(self.size.width, self.rich_style)
        return self._strip_for(scroll_y + y, self.size.width)
