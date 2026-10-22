"""兼容壳：TUI 实现已迁到 `src/ui/app.py`。

保留同名导出，旧 TUI 仅在 `HARNESS_UI=textual-legacy` 时由 `harness.run_repl` 加载；
迁移期结束后再删除本文件。
"""
from ui.app import (  # noqa: F401
    _TUI_COMMANDS,
    CSS,
    HISTORY_BATCH,
    HISTORY_MARKDOWN_TAIL,
    HISTORY_RENDER_LIMIT,
    PALETTE,
    CCApp,
    PromptArea,
    SessionPickerModal,
    command_hint_text,
    complete_mention_text,
    completion_items,
    mention_hint_text,
    run,
    slash_command,
)
