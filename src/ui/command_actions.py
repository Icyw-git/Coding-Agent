"""斜杠命令路由：命令注册表 → HarnessApp 动作。

只做「已迁移命令」的分发与本地提示，不持有视图状态。命令元数据仍由
`ui.commands` 提供；具体动作落在各 mixin（会话、工作区、回合控制）里。
"""
from __future__ import annotations

import asyncio

from ui import commands
from ui.modals.command_palette import CommandPaletteModal
from ui.modals.models import ModelPickerModal


class CommandActionsMixin:
    """HarnessApp 的 `/` 命令分发与模型切换。"""

    def action_open_command_palette(self) -> None:
        self.open_command_palette()

    def open_command_palette(self) -> None:
        self.close_suggestions()
        specs = [spec for spec in commands.all_commands() if spec.ported]
        self.push_screen(CommandPaletteModal(specs),
                         self._command_palette_selected)

    def _command_palette_selected(
            self, spec: commands.CommandSpec | None) -> None:
        if spec is None:
            return
        if spec.argument_hint:
            value = f'/{spec.name} '
            self.close_suggestions()
            self._programmatic_text = value
            self._composer.text = value
            self._composer.cursor_location = (0, len(value))
            self._composer.focus()
        else:
            self.submit_text(f'/{spec.name}')

    def _handle_command(self, text: str) -> bool:
        """返回 True 表示已作为命令处理，不再发给模型。"""
        if not text.startswith('/'):
            return False
        raw = text[1:].strip()
        verb, _, argument = raw.partition(' ')
        verb = verb.lower()
        argument = argument.strip()

        if verb.startswith('skill:'):
            self.submit_skill(verb[len('skill:'):], argument)
            return True

        spec = commands.find(verb)
        if spec is None:
            hint = commands.did_you_mean(verb)
            suffix = f'；是否想输入 {hint}？' if hint else ''
            self._notice(f'Unknown command: /{verb}{suffix}', level='error')
            return True
        verb = spec.name
        if not spec.ported:
            self._notice(f'/{verb} 暂未迁移到新 TUI；请用 --ui ansi。')
            return True

        if self._busy and verb in ('new', 'resume', 'sessions', 'fork', 'clone', 'cd'):
            self._notice('当前任务尚未结束，请结束后再切换会话或工作区。')
            return True

        import harness
        if verb == 'quit':
            self.exit()
        elif verb == 'help':
            spec = commands.find(argument) if argument else None
            if spec is None:
                self._notice(commands.help_text())
            else:
                availability = ('' if spec.ported
                                else f'{chr(10)}仅行式 REPL 提供（--ui ansi）')
                self._notice(f'{spec.usage}{chr(10)}{spec.description}'
                             f'{availability}')
        elif verb == 'hotkeys':
            self._notice(commands.hotkeys_text())
        elif verb == 'clear':
            self._stream_render_cache = None
            self.model.clear()
            self._history_messages = []
            self._history_start = 0
            self._history_auto_armed = False
            self._history_last_offset = 0
            self._mark_dirty()
        elif verb == 'more':
            if not self.load_older_history():
                self._notice('没有更早的历史了。')
        elif verb in ('status', 'pwd'):
            state = self._display_status()
            lines = [f"model: {harness.os.getenv('LLM_MODEL_ID', '') or 'unset'}",
                     f'workspace: {harness.get_workdir()}']
            if state.get('ctx_pct') is not None:
                lines.append(f"context: {state['ctx_pct']:.0f}%")
            if state.get('tokens'):
                lines.append(f"tokens: {state['tokens']}")
            prompt_tokens = state.get('prompt_tokens')
            cached_tokens = state.get('cached_tokens')
            if prompt_tokens and cached_tokens is not None:
                lines.append(
                    f"cache: {100 * min(1.0, cached_tokens / prompt_tokens):.0f}%")
            self._notice(chr(10).join(lines))
        elif verb == 'model':
            if argument:
                self._set_model(argument.strip())
            else:
                self.open_model_picker()
        elif verb == 'diff':
            try:
                self._notice(harness.format_workspace_diff(color=False))
            except Exception as exc:
                self._notice(f'Diff failed: {exc}', level='error')
        elif verb == 'cache':
            try:
                if argument.strip().lower() == 'profile':
                    self._notice(harness.format_storage_profile())
                else:
                    self._notice(harness.tool_cache.handle_cache_command(
                        harness.TOOL_RESULTS_DIR, harness.SESSIONS_DIR, argument))
            except Exception as exc:
                self._notice(f'Cache failed: {exc}', level='error')
        elif verb == 'sandbox':
            try:
                if argument:
                    message = harness.apply_sandbox_tier(argument)
                    self._status['sandbox'] = argument.split()[0]
                    self._refresh_footer()
                else:
                    message = harness.format_sandbox_tier()
                self._notice(message)
            except Exception as exc:
                self._notice(f'Sandbox failed: {exc}', level='error')
        elif verb == 'review':
            self.start_review()
        elif verb == 'watch':
            self.start_watch(argument)
        elif verb == 'copy':
            self.copy_last_tool()
        elif verb == 'dequeue':
            self.open_queue_picker()
        elif verb == 'expand':
            self.toggle_last_tool()
            self._notice('Toggled the most recent tool block.')
        elif verb == 'approvals':
            error = harness.command_error('/approvals', argument)
            if error:
                self._notice(error, level='error')
            elif argument:
                index, choice = argument.split()
                pending = harness.hooks.GATE.pending()
                if not 1 <= int(index) <= len(pending):
                    self._notice('没有该序号的待审批操作；先运行 /approvals 查看列表。')
                else:
                    req = pending[int(index) - 1]
                    record = harness.hooks.GATE.resolve(req.req_id, choice == 'y', by='cli')
                    self._notice(f'已决议：{record.status} · {req.tool}')
            else:
                self._notice(harness.format_pending_approvals())
        elif verb == 'revoke':
            import hooks
            count = hooks.GATE.revoke_session(
                self._session_id or hooks.current_session_id() or None)
            self._notice(f'已撤销 {count} 条当前会话预授权。')
        elif verb == 'workspaces':
            self.show_workspaces()
        elif verb == 'workspace':
            self.handle_workspace(argument)
        elif verb == 'cd':
            self.change_workspace(argument)
        elif verb == 'find':
            self.start_find(argument)
        elif verb in ('archive', 'delete'):
            self.start_cleanup(verb, argument)
        elif verb == 'new':
            self.start_new_session(argument)
        elif verb == 'rename':
            self.rename_session(argument)
        elif verb == 'fork':
            if argument:
                self.fork_session(argument)
            else:
                self.open_fork_picker()
        elif verb == 'clone':
            self.fork_session('')
        elif verb == 'checkpoint':
            self.create_checkpoint(argument)
        elif verb == 'checkpoints':
            self.show_checkpoints()
        elif verb == 'checkpoint-gc':
            if self._busy:
                self._notice('当前任务尚未结束，请结束后再清理快照。')
            elif not self._store:
                self._notice('No session context.', level='error')
            else:
                self.start_checkpoint_gc(argument)
        elif verb == 'rewind':
            self.start_rewind(argument)
        elif verb == 'undo':
            if argument:
                self._notice('用法：/undo（不带参数）', level='error')
            else:
                self.start_rewind('turn --yes')
        elif verb == 'compact':
            self.start_compact(argument)
        elif verb == 'sessions':
            self.open_session_picker()
        elif verb == 'resume':
            if argument:
                asyncio.create_task(self._resume_by_argument(argument))
            else:
                self.open_session_picker()
        elif verb == 'export':
            try:
                path = harness.export_session(self._messages)
                self._notice(f'Exported transcript: {path}')
            except Exception as exc:
                self._notice(f'Export failed: {exc}', level='error')
        elif verb in ('allow', 'deny', 'ask'):
            if not argument:
                self._notice(f'Use /{verb} <tool>.')
            else:
                harness.permission_gate.set_policy(argument, verb)
                self._notice(f'Permission policy for {argument}: {verb}')
        return True

    def _display_status(self) -> dict:
        return dict(self._status)

    def open_model_picker(self) -> None:
        import harness
        current = harness.os.getenv('LLM_MODEL_ID', '')
        models = sorted(set(harness.MODEL_WINDOWS) | ({current} if current else set()))
        self.push_screen(ModelPickerModal(models, current), self._on_model_picked)

    def _on_model_picked(self, model) -> None:
        if model:
            self._set_model(str(model))

    def _set_model(self, model: str) -> None:
        import harness
        harness.os.environ['LLM_MODEL_ID'] = model
        self._notice(f'model: {model}')
        self._refresh_footer()
