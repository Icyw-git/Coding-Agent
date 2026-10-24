"""输入补全：斜杠命令/文件提及候选、浮层定位与采用。

候选本身由 `ui.composer.completions` 计算；这里负责把编辑器文本、工作区文件
缓存和浮层坐标串起来。App 只转发 Enter/Tab/↑↓ 等动作。
"""
from __future__ import annotations

import time

from textual.widgets import TextArea

from file_mentions import workspace_files
from ui.composer.completions import apply_suggestion, suggestions
from ui.widgets import SUGGESTION_LIMIT


class SuggestionsMixin:
    """维护补全候选与浮层；不拥有 Composer，只读写其文本和焦点。"""

    def _workspace_files(self) -> list[str]:
        import harness
        root = harness.get_workdir()
        now = time.monotonic()
        if root != self._mention_root or now - self._mention_at > 5.0:
            self._mention_files = workspace_files(root)
            self._mention_root = root
            self._mention_at = now
        return self._mention_files

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if getattr(event.text_area, 'id', '') != 'composer':
            return
        if self._programmatic_text is not None:
            if event.text_area.text == self._programmatic_text:
                return    # 程序化写入：保持补全关闭，直到用户再次编辑
            self._programmatic_text = None
        self._update_suggestions(event.text_area.text)

    def _update_suggestions(self, value: str) -> None:
        files = self._workspace_files() if '@' in (value or '') else []
        self._suggestions = suggestions(value, files)
        self._suggestion_index = 0
        if self._suggestions:
            self._popup.show(self._suggestions, self._suggestion_index)
            self._place_popup()
        else:
            self._popup.hide_popup()

    def _place_popup(self) -> None:
        if self._popup is None or self._composer is None or not self._suggestions:
            return
        region = self._composer.region
        if region.width <= 0:
            return
        rows = min(len(self._suggestions), SUGGESTION_LIMIT) + 2
        width = min(max(24, region.width), 100)
        x = region.x
        y = max(0, region.y - rows)
        self._popup.styles.offset = (x, y)
        self._popup.styles.width = width
        self._popup.styles.height = rows

    def has_suggestions(self) -> bool:
        return bool(self._suggestions)

    def accept_suggestion(self) -> None:
        if not self._suggestions or self._composer is None:
            return
        value, _label = self._suggestions[self._suggestion_index]
        text = self._composer.text
        new_text = apply_suggestion(text, value)
        self._programmatic_text = new_text
        self._composer.text = new_text
        self.close_suggestions()
        self._composer.cursor_location = (0, len(new_text))
        self._composer.focus()

    def choose_suggestion(self, index: int) -> None:
        if 0 <= index < len(self._suggestions):
            self._suggestion_index = index
            self.accept_suggestion()

    def cycle_suggestion(self, delta: int) -> None:
        if not self._suggestions:
            return
        self._suggestion_index = ((self._suggestion_index + delta)
                                  % len(self._suggestions))
        self._popup.show(self._suggestions, self._suggestion_index)
        self._place_popup()

    def close_suggestions(self) -> None:
        self._suggestions = []
        if self._popup is not None:
            self._popup.hide_popup()
