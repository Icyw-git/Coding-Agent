"""斜杠命令路由：命令注册表 → HarnessApp 动作。

只做「已迁移命令」的分发与本地提示，不持有视图状态。命令元数据仍由
`ui.commands` 提供；具体动作落在各 mixin（会话、工作区、回合控制）里。
"""
from __future__ import annotations

import asyncio

from ui import commands
from ui.modals.command_palette import CommandPaletteModal
from ui.modals.models import ModelChoice, ModelPickerModal


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
            lines = [f"model: {state.get('model') or harness.model_for('main') or 'unset'}",
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
            import grants
            import hooks
            session_id = (self._session_id or hooks.current_session_id() or '')
            count = hooks.GATE.revoke_session(session_id or None)
            removed = grants.revoke_for_session(session_id) if session_id else []
            for record in removed:
                self._audit_grant('grant_removed', record)
            extra = (f'，以及 {len(removed)} 条目录写授权' if removed else '')
            self._notice(f'已撤销 {count} 条当前会话预授权{extra}。')
        elif verb == 'rules':
            self.handle_rules()
        elif verb == 'grants':
            self.handle_grants(argument)
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

    def _audit_grant(self, event: str, record: dict) -> None:
        store = self._store
        if store is None:
            return
        try:
            store.append(event, path=record.get('path', ''),
                         scope=record.get('scope', 'write'),
                         expires=record.get('expires', ''),
                         by='cli', note=record.get('note', ''),
                         session_id=store.session_id)
        except Exception as exc:
            self._notice(f'[grants] 审计写入失败: {exc}', level='error')

    def handle_rules(self) -> None:
        import rules as user_rules

        ruleset = user_rules.load()
        if ruleset.error:
            self._notice('规则文件加载失败：' + str(ruleset.error)
                         + chr(10) + '修好之前所有规则都不生效。')
            return
        if not ruleset.rules:
            self._notice('（没有用户规则）' + chr(10)
                         + f'文件：{user_rules.path_for_display()}' + chr(10)
                         + '可写 allow / ask / deny；只影响「问不问」，改不动硬拒与档位。')
            return
        lines = [f'用户规则：{len(ruleset.rules)} 条'
                 f'（{user_rules.path_for_display()}）']
        for rule in ruleset.rules:
            tools = ','.join(rule.tools)
            scope = '' if rule.scope == user_rules.SCOPE_GLOBAL else f' [{rule.scope}]'
            note = f' · {rule.note}' if rule.note else ''
            lines.append(f'  {rule.decision:<5} [{tools}]{scope} '
                         f'{", ".join(rule.prefixes)}{note}')
        lines.append('说明：只作用于 destructive / opaque_command / external_tool 三类询问；'
                     '档位、秘密文件、域名与桌面控制不受影响。')
        self._notice(chr(10).join(lines))

    def handle_grants(self, argument: str) -> None:
        import grants

        parts = argument.split(maxsplit=1)
        action = parts[0].casefold() if parts else ''
        rest = parts[1] if len(parts) > 1 else ''
        usage = ('用法：/grants（查看）\n'
                 '/grants add "<目录>" [until YYYY-MM-DD] [note 备注]\n'
                 '/grants remove "<目录>"')
        if not action:
            rows = grants.list_grants()
            if not rows:
                self._notice('（没有目录写授权）\n' + usage)
                return
            lines = ['目录写授权：']
            for row in rows:
                expiry = (f"有效至 {row['expires']}" if row.get('expires')
                          else '永久')
                if not row.get('active'):
                    expiry = f"已过期（{row['expires']}）"
                note = f" · {row['note']}" if row.get('note') else ''
                lines.append(f"  {row['path']}  [{expiry}{note}]")
            lines.append('撤销：/grants remove "<目录>"；秘密文件与系统目录不受影响。')
            self._notice(chr(10).join(lines))
            return
        if action not in ('add', 'remove'):
            self._notice(usage, level='error')
            return
        try:
            path, options = grants.split_path_argument(rest)
        except ValueError as exc:
            self._notice(f'[grants] {exc}', level='error')
            return
        if not path:
            self._notice(usage, level='error')
            return
        session_id = self._session_id or ''
        try:
            if action == 'remove':
                if options:
                    self._notice(usage, level='error')
                    return
                record = grants.remove_path(path)
                if record is None:
                    self._notice(f'没有找到该目录的授权：{path}')
                    return
            else:
                expires, note = grants.parse_options(options)
                record = grants.add_path(path, expires=expires, note=note,
                                         created_by='cli', session_id=session_id)
        except (grants.GrantError, OSError) as exc:
            self._notice(f'[grants] {exc}', level='error')
            return
        self._audit_grant('grant_removed' if action == 'remove' else 'grant_added',
                          record)
        if action == 'remove':
            self._notice(f"已撤销目录写授权：{record['path']}")
        else:
            expiry = (f"有效至 {record['expires']}" if record.get('expires')
                      else '永久')
            self._notice(f"已授权目录写操作：{record['path']}（{expiry}）")

    def open_model_picker(self) -> None:
        import harness

        settings = harness.llm_config.get_settings()
        current_ref = (harness.os.getenv('HARNESS_MODEL_MAIN')
                       or settings.roles.get('main')
                       or harness.os.getenv('LLM_MODEL_ID', ''))
        try:
            current_target = settings.resolve_for('main')
        except harness.llm_config.ConfigError:
            current_target = None

        choices: dict[str, tuple[ModelChoice, object]] = {}

        def add_choice(ref: str, target, context_window: int | None = None) -> None:
            if not target.supports('tools'):
                return
            window = (context_window or target.context_window
                      or harness.MODEL_WINDOWS.get(target.model)
                      or harness.MODEL_WINDOWS.get(ref))
            label = f'{target.provider}/{target.model}'
            if ref != target.model:
                label += f' ({ref})'
            choices[ref] = (ModelChoice(
                ref=ref, label=label,
                search=f'{ref} {label} {target.provider} {target.model}',
                context_window=window), target)

        for ref, config in settings.models.items():
            add_choice(ref, settings.resolve(ref, role='main'),
                       config.context_window)

        provider_hint = current_target.provider if current_target else None
        for ref, window in harness.MODEL_WINDOWS.items():
            if ref in choices:
                continue
            try:
                target = settings.resolve(ref, role='main',
                                          provider_hint=provider_hint)
            except harness.llm_config.ConfigError:
                continue
            add_choice(ref, target, window)

        active_model = self._status.get('model')
        if active_model:
            active_refs = [ref for ref, (_choice, target) in choices.items()
                           if target.model == active_model]
            if len(active_refs) == 1:
                current_ref = active_refs[0]
            elif current_target is not None:
                same_provider = [
                    ref for ref in active_refs
                    if choices[ref][1].provider == current_target.provider]
                if len(same_provider) == 1:
                    current_ref = same_provider[0]

        if current_target is not None and current_ref not in choices:
            equivalent = next((ref for ref, (_choice, target) in choices.items()
                               if (target.provider, target.model)
                               == (current_target.provider, current_target.model)), None)
            if equivalent is not None:
                current_ref = equivalent
            elif current_ref:
                add_choice(current_ref, current_target)

        models = sorted((choice for choice, _target in choices.values()),
                        key=lambda choice: (choice.label.casefold(),
                                           choice.ref.casefold()))
        self.push_screen(ModelPickerModal(models, current_ref),
                         self._on_model_picked)

    def _on_model_picked(self, model) -> None:
        if model:
            self._set_model(str(model))

    def _set_model(self, model: str) -> None:
        import harness

        environ = dict(harness.os.environ)
        environ['HARNESS_MODEL_MAIN'] = model
        try:
            target = harness.llm_config.load_settings(environ).resolve_for('main')
        except harness.llm_config.ConfigError as exc:
            self._notice(f'Model unchanged: {exc}', level='error')
            return
        if not target.supports('tools'):
            self._notice(f'Model unchanged: {target.ref} does not support tools.',
                         level='error')
            return

        harness.os.environ['HARNESS_MODEL_MAIN'] = model
        harness.os.environ['LLM_MODEL_ID'] = target.model
        self._status['model'] = target.model
        window = harness._context_window(target.model)
        self._status['ctx_window'] = window
        ctx_tokens = self._status.get('ctx_tokens')
        self._status['ctx_pct'] = (
            min(100.0, 100.0 * ctx_tokens / window)
            if ctx_tokens is not None and window else None)
        self._notice(f'model: {target.ref}')
        self._refresh_footer()
