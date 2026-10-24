"""底部状态栏：cwd(branch) · 标题 · 模型 · ctx/tokens · cache · cost · 队列。

只从 `_status`、会话元数据与队列长度读数据，不修改任何状态；App 在挂载、
事件归约（`round_end`/`status`）和定时器中调用 `_refresh_footer`。
"""
from __future__ import annotations

from ui.rendering import _short_count


class FooterMixin:
    """把状态字典格式化成一行 footer 文本。"""

    def _refresh_footer(self) -> None:
        if self._footer is None:
            return
        parts = []
        # 目录/分支常驻，不会被会话标题挤掉（标题只在有实际内容时显示）。
        try:
            import harness
            workspace = harness.get_workdir()
            branch = harness.current_branch(workspace)
            label = f'{workspace.name} ({branch})' if branch else workspace.name
            parts.append(str(label))
        except Exception:
            pass
        if self._title and self._title not in ('新会话', '新对话', 'New session', 'Untitled'):
            parts.append(self._title[:28])
        try:
            import harness
            model = self._status.get('model') or harness.model_for('main')
            parts.append(model or '(model unset)')
        except Exception:
            pass
        ctx_pct = self._status.get('ctx_pct')
        ctx_tokens = self._status.get('ctx_tokens')
        ctx_window = self._status.get('ctx_window')
        if ctx_pct is not None:
            detail = f'ctx {float(ctx_pct):.0f}%'
            if ctx_tokens and ctx_window:
                detail += f' {_short_count(ctx_tokens)}/{_short_count(ctx_window)}'
            parts.append(detail)
        input_tokens = self._status.get('input_tokens')
        output_tokens = self._status.get('output_tokens')
        prompt_tokens = self._status.get('prompt_tokens')
        cached_tokens = self._status.get('cached_tokens')
        cache_write = self._status.get('cache_write_tokens') or 0
        if input_tokens is None and prompt_tokens is not None:
            input_tokens = max(0, prompt_tokens - (cached_tokens or 0)
                               - cache_write)
        if input_tokens:
            parts.append(f'↑{_short_count(input_tokens)}')
        if output_tokens:
            parts.append(f'↓{_short_count(output_tokens)}')
        if cached_tokens:
            parts.append(f'R{_short_count(cached_tokens)}')
        if cache_write:
            parts.append(f'W{_short_count(cache_write)}')
        rate = self._status.get('latest_cache_hit_rate')
        if rate is None and prompt_tokens:
            rate = min(1.0, (cached_tokens or 0) / prompt_tokens)
        if rate is not None and (cached_tokens or self._status.get('cache_seen')):
            parts.append(f'cache {100 * float(rate):.0f}%')
        total = self._status.get('tokens')
        if total and not (input_tokens or output_tokens):
            parts.append(f'tok {_short_count(total)}')
        if self._status.get('cost') and self._status.get('cost_source') != 'unavailable':
            symbol = '¥' if self._status.get('currency', 'CNY') == 'CNY' else ''
            parts.append(f"~{symbol}{self._status['cost']}")
        queued = len(self._pending) + len(self._steering)
        if queued:
            parts.append(f'{queued} queued')
        if self._busy:
            parts.append('working')
        footer = '  ·  '.join(part for part in parts if part)
        cached = getattr(self, '_footer_render_cache', None)
        if cached is not None and cached[0] is self._footer and cached[1] == footer:
            return
        self._footer.update(footer)
        self._footer_render_cache = (self._footer, footer)
