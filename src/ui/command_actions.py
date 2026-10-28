"""斜杠命令路由：命令注册表 → HarnessApp 动作。

只做「已迁移命令」的分发与本地提示，不持有视图状态。命令元数据仍由
`ui.commands` 提供；具体动作落在各 mixin（会话、工作区、回合控制）里。
"""
from __future__ import annotations

import asyncio

from ui import commands
from ui.modals.command_palette import CommandPaletteModal
from ui.modals.models import ModelChoice, ModelPickerModal


def _archived_when(age_days: int) -> str:
    """归档时间的人话。

    ``0 天前归档`` 读起来像 bug——今天归档的东西不该说成 "0 天前"，
    而且它跟在 "已归档（太久没用到）" 后面，两个说法看着互相矛盾。
    """
    if age_days <= 0:
        return '今天归档'
    if age_days == 1:
        return '昨天归档'
    return f'{age_days} 天前归档'


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
            self._notice(f'未知命令：/{verb}{suffix}', level='error')
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
                self._notice(f'生成 diff 失败： {exc}', level='error')
        elif verb == 'cache':
            try:
                if argument.strip().lower() == 'profile':
                    self._notice(harness.format_storage_profile())
                else:
                    self._notice(harness.tool_cache.handle_cache_command(
                        harness.TOOL_RESULTS_DIR, harness.SESSIONS_DIR, argument))
            except Exception as exc:
                self._notice(f'读取缓存失败： {exc}', level='error')
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
                self._notice(f'切换档位失败： {exc}', level='error')
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
            self._notice('已折叠/展开最近一个工具块。')
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
        elif verb == 'domains':
            import terminal_views

            self._notice(terminal_views.domains(argument))
        elif verb == 'audit':
            import terminal_views

            self._notice(terminal_views.audit(argument))
        elif verb == 'rules':
            self.handle_rules()
        elif verb == 'memories':
            self.handle_memories(argument)
        elif verb == 'improvements':
            self.handle_improvements(argument)
        elif verb == 'undo-move':
            self.handle_undo_move(argument)
        elif verb == 'usage':
            self.handle_usage(argument)
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
                self._notice('当前没有会话。', level='error')
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
                self._notice(f'导出失败： {exc}', level='error')
        elif verb in ('allow', 'deny', 'ask'):
            if not argument:
                self._notice(f'用法：/{verb} <工具名>，例如 /{verb} web_fetch。'
                             '工具名在审批提示里能看到。')
            else:
                harness.permission_gate.set_policy(argument, verb)
                self._notice(f'已设置工具策略： {argument}: {verb}')
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

    def handle_memories(self, argument: str) -> None:
        """逐会话的记忆开关（对齐 Codex 的 chat-level /memories）。

        只改本会话，不动全局环境变量：那是一个显式的进程级选择，被一条会话
        命令悄悄改掉会让人下次启动时莫名其妙。
        """
        import memory
        import memory_scope

        arg = (argument or '').strip().lower()
        if arg.startswith('quality') or arg.startswith('eval'):
            self.handle_memory_quality()
            return
        if arg.startswith('why'):
            self.handle_why((argument or '').strip()[len('why'):].strip())
            return
        if arg.startswith('restore'):
            import llm_route
            import memory_aging
            filename = (argument or '').strip()[len('restore'):].strip()
            if not filename:
                self._notice('用法：/memories restore <文件名>')
                return
            if memory_aging.restore_archived(llm_route.harness().MEMORY_DIR,
                                             filename):
                memory._rebuild_index()
                self._notice(f'已恢复：{filename}')
            else:
                available = [item['filename'] for item in
                             memory_aging.archived_entries(
                                 llm_route.harness().MEMORY_DIR)]
                hint = ('可恢复的：' + '、'.join(available[:5])
                        if available else '归档目录是空的')
                self._notice(f'恢复失败：找不到归档条目 {filename}。' + hint)
            return
        if arg:
            try:
                memory_scope.set_mode(arg)
            except ValueError as exc:
                self._notice(str(exc))
                return
            # 持久化到会话事件流：重启 / 重新打开还要生效（ContextVar 不跨进程）。
            store = getattr(self, '_session_store', None)
            if store is not None:
                try:
                    store.append(memory_scope.EVENT, mode=arg)
                except Exception:
                    pass
                try:
                    import hooks
                    hooks._append_audit(memory_scope.EVENT,
                                        {'mode': arg, 'by': 'user'})
                except Exception:
                    pass
            self._notice(f'本会话记忆：{memory_scope.describe()}')
            return
        # 不带参数：显示当前模式 + 记忆概况。
        entries = memory.list_memory_files()
        lines = [f'本会话记忆：{memory_scope.describe()}',
                 f'已存记忆：{len(entries)} 条']
        if entries:
            for item in entries[:8]:
                lines.append(f"  · {item.get('name')} ({item.get('type')})")
            if len(entries) > 8:
                lines.append(f'  … 还有 {len(entries) - 8} 条（用 recall 查全部）')
        # 归档目录：淘汰不删除，用户要能看到并找回。
        try:
            import llm_route
            import memory_aging
            archived = memory_aging.archived_entries(
                llm_route.harness().MEMORY_DIR)
        except Exception:
            archived = []
        if archived:
            lines.append(f'已归档：{len(archived)} 条（太久没用到）')
            for item in archived[:5]:
                lines.append(
                    f"  · {item['filename']}（{_archived_when(item['age_days'])}）")
            lines.append('  恢复：/memories restore <文件名>')
        lines.append('')
        lines.append('切换：/memories on|off|readonly|writeonly')
        lines.append('  readonly  = 用已有记忆，不产生新的')
        lines.append('  writeonly = 不受旧记忆影响，但本会话会被学习')
        self._notice(chr(10).join(lines))

    def handle_usage(self, argument: str) -> None:
        """`/usage [天数]`：聚合 token、耗时、工具、审批与记忆。

        数据全部来自既有事件流，不新增埋点。这也是判断"记忆/主动性/自改进
        有没有用"的前提——没有它只能不断加机制、永远无法判断。
        """
        import harness
        import usage_stats

        try:
            days = int((argument or '').strip() or usage_stats.DEFAULT_DAYS)
        except ValueError:
            self._notice('用法：/usage [天数]，天数需为整数')
            return
        days = max(1, min(days, 365))
        stats = usage_stats.collect(harness.SESSIONS_DIR, days=days)
        self._notice(usage_stats.format_report(stats))

    def handle_undo_move(self, argument: str) -> None:
        """`/undo-move [batch_id]`：撤销上一批文件移动。

        与 `/undo` 不同：`/undo` 回退**对话**，这个回退**文件移动**。
        """
        import paths
        from tool_move import undo as undo_moves

        self._notice(undo_moves(paths.cache_path(),
                                batch_id=(argument or '').strip()))

    def handle_improvements(self, argument: str) -> None:
        """`/improvements`：看自我改进记录，或回滚某次改动。

        记录只覆盖 Ring 1（skills / AGENTS.md）。策略、审批、测试由
        verifier_guard 硬拒，不会出现在这里——那条路不该有"被改过"的记录。
        """
        import paths
        import self_improve_ledger as ledger

        arg = (argument or '').strip()
        cache = paths.cache_path()
        if arg.startswith('begin'):
            self._improvements_begin(cache, arg[len('begin'):].strip())
            return
        if arg.startswith('check'):
            self._improvements_check(cache, arg[len('check'):].strip())
            return
        if arg in ('status', 'session'):
            self._improvements_status(cache)
            return
        if arg.startswith('end'):
            import self_improve
            self_improve.clear_state(cache)
            self._notice('已结束当前改进会话（台账与快照保留）。')
            return
        if arg.startswith('rollback'):
            change_id = arg[len('rollback'):].strip()
            if not change_id:
                self._notice('用法：/improvements rollback <批次号>'
                             '（批次号在 /improvements 列表里）')
                return
            restored = ledger.rollback(cache, change_id)
            if not restored:
                self._notice(f'回滚失败：{change_id} 没有可恢复的快照')
                return
            created = ledger.created_paths(cache, change_id)
            self._notice(f'已恢复 {len(restored)} 个文件：'
                         + '、'.join(restored)
                         + (chr(10) + '新增的文件不会自动删除：'
                            + '、'.join(created) if created else ''))
            return
        self._notice(ledger.format_ledger(cache))

    def _improvements_begin(self, cache, goal: str) -> None:
        """开一个改进会话并跑基线。

        基线是后续每一轮的比较起点，必须现在测——事后再补就没有"改之前"了。
        """
        import self_improve

        if not goal:
            self._notice('用法：/improvements begin <目标>')
            return
        existing = self_improve.load_state(cache)
        if existing and existing.get('goal') != goal:
            self._notice(
                f"已有未完成的会话：{existing.get('goal')}"
                f"（{existing.get('rounds')} 轮）。先 /improvements end 结束它。")
            return
        session = self_improve.Session(cache_dir=cache, workdir=self._workdir_for_improve(),
                                       goal=goal)
        baseline = session.baseline()
        self._notice('基线已记录：' + self._describe_metrics(baseline)
                     + chr(10) + '改完用 /improvements check <一句话机制说明> 判定。')

    def _improvements_check(self, cache, mechanism: str) -> None:
        """测一轮并判定。

        ``mechanism`` 是强制参数：说不清机制就说明可能不是在改进，而是在优化
        某个自己没打算优化的东西（这条来自车间经验，见 docs/RSI_2026_RESEARCH.md）。
        """
        import self_improve

        state = self_improve.load_state(cache)
        if not state:
            self._notice('没有进行中的会话。先用 /improvements begin <目标>。')
            return
        if not mechanism:
            self._notice('需要一句话机制说明：/improvements check <为什么这个改动有用>')
            return
        workdir = self._workdir_for_improve()
        session = self_improve.Session(
            cache_dir=cache, workdir=workdir, goal=state['goal'],
            metric=state.get('metric') or 'ruff_errors',
            max_rounds=int(state.get('max_rounds') or 5))
        # 本轮碰过哪些文件：用台账里最新一笔的路径；没有就按 Ring 1 全量估。
        changed = self._improve_scope_paths(workdir)
        result = session.evaluate(changed, summary=mechanism, mechanism=mechanism)
        mark = {'keep': '✓ 保留', 'observe': '◐ 观察中', 'discard': '✗ 放弃'}
        lines = [f"第 {session.rounds} 轮：{mark.get(result['decision'], result['decision'])}",
                 f"理由：{result['reason']}"]
        if result.get('after'):
            lines.append('当前：' + self._describe_metrics(result['after']))
        if session.exhausted():
            lines.append('会话已收敛（/improvements end 结束，/improvements 看记录）。')
        self._notice(chr(10).join(lines))

    def _improvements_status(self, cache) -> None:
        import self_improve

        state = self_improve.load_state(cache)
        if not state:
            self._notice('没有进行中的改进会话。'
                         '/improvements begin <目标> 开始一次。')
            return
        lines = [f"目标：{state.get('goal')}",
                 f"指标：{state.get('metric')}",
                 f"轮数：{state.get('rounds')}/{state.get('max_rounds')}",
                 f"观察中：{state.get('pending_regression') or 0}"
                 f"/{self_improve.PATIENCE_ROUNDS}"]
        if state.get('best'):
            lines.append('基线：' + self._describe_metrics(state['best']))
        self._notice(chr(10).join(lines))

    def _workdir_for_improve(self):
        """改进作用于当前工作区（skills/ 与 AGENTS.md 都在那里）。"""
        import llm_route

        return llm_route.harness().get_workdir()

    def _improve_scope_paths(self, workdir) -> list[str]:
        """会被本轮判定覆盖的路径：现有的 skills/*.md 与 AGENTS.md。

        不猜"模型刚改了哪个文件"——那要靠比对快照，而快照在登记时才建。
        直接列全部可改范围，判定更保守（任一个越界都会被拦）。
        """
        from pathlib import Path as _Path

        root = _Path(workdir)
        paths = []
        skills = root / 'skills'
        if skills.is_dir():
            paths += [p.relative_to(root).as_posix()
                      for p in sorted(skills.rglob('*.md'))]
        if (root / 'AGENTS.md').is_file():
            paths.append('AGENTS.md')
        return paths or ['skills/']

    @staticmethod
    def _describe_metrics(metrics: dict) -> str:
        parts = []
        for values in (metrics or {}).values():
            if not isinstance(values, dict) or not values:
                continue
            for key, value in values.items():
                parts.append(f'{key}={value}')
        return ' '.join(parts) or '（无可用指标）'

    def handle_memory_quality(self) -> None:
        """`/memories quality`：看异步 LLM 评测的汇总。

        数据由回合结束后的后台线程写入（默认关闭，`HARNESS_MEMORY_EVAL=1`
        开启，每 10 轮采样一次），只展示不改记忆。
        """
        import memory_eval
        import paths

        self._notice(memory_eval.format_summary(paths.cache_path()))

    def handle_why(self, name: str) -> None:
        """`/memories why <名字>`：这条记忆从哪来。

        TIME 那篇调查的教训：不可观察的长期记忆会变成黑箱。每条记忆要能回答
        「为什么记这个」——所以写盘时存 source_session + source_quote。
        """
        import memory

        target = ' '.join(str(name or '').split())
        if not target:
            self._notice('用法：/memories why <记忆名>（先 /memories 看列表）')
            return
        entries = memory.list_memory_files()
        needle = target.casefold()
        hit = next((item for item in entries
                    if needle in str(item.get('name') or '').casefold()
                    or needle in str(item.get('filename') or '').casefold()), None)
        if hit is None:
            self._notice(f'没有叫 {target} 的记忆。'
                         '用 /memories 看全部名称（不带参数）。')
            return
        filename = hit.get('filename') or ''
        raw = ''
        try:
            import llm_route
            path = llm_route.harness().MEMORY_DIR / filename
            raw = path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            pass
        meta, _body = memory._parse_frontmatter(raw) if raw else ({}, '')
        lines = [f"记忆：{hit.get('name')}（{hit.get('type')}）",
                 f"描述：{hit.get('description') or '（无）'}"]
        quote = str(meta.get('source_quote') or '').strip()
        session_id = str(meta.get('source_session') or '').strip()
        if quote:
            lines.append(f'原话：「{quote}」')
        if session_id:
            lines.append(f'来源会话：{session_id}')
        if not quote and not session_id:
            lines.append('来源：未记录（早期写入或自动抽取，没有留下原话）')
        lines.append('')
        # 用户不调工具，他们说话。"用 forget 工具"是模型侧的措辞，
        # 对着用户说等于让他去做一件做不到的事。
        lines.append('想删掉它，直接说「忘掉这条」就行。')
        self._notice(chr(10).join(lines))

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
        # 不点名内部决策码（destructive / opaque_command / external_tool）：
        # 那是代码里的枚举，用户读不出自己能影响什么。说清"能改什么、改不动什么"。
        lines.append('说明：规则只能决定「本该问你的操作要不要问」——'
                     'allow 不再问、deny 直接拒。')
        lines.append('  改不动：秘密文件、破坏性命令黑名单、执行档位、'
                     '域名规则、桌面/屏幕/剪贴板工具。')
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
                # 已经在 /grants 里了，再说“用法：/grants（查看）”是废话。
                # 空状态该回答的是“这东西干什么用、怎么加一条”。
                self._notice(
                    '（没有目录写授权）' + chr(10)
                    + '作用：默认档下允许往该目录写改而不逐次审批。' + chr(10)
                    + '加一条：/grants add "<目录>" [until YYYY-MM-DD] [note 备注]')
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
            self._notice(f'模型未更改： {exc}', level='error')
            return
        if not target.supports('tools'):
            self._notice(f'模型未更改： {target.ref} 不支持工具。',
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
