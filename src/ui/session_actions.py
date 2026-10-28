"""会话与快照动作：new/rename/fork/checkpoint/rewind/compact/resume。

这些方法依赖 HarnessApp 的运行时属性（store/messages/composer/model 等），
以 mixin 形式拆出，让 Textual App 只负责视图与事件分发。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import re
import threading
import time
from functools import partial
from pathlib import Path

from ui.modals.confirm import ConfirmModal
from ui.modals.forks import ForkPickerModal
from ui.modals.sessions import RenameSessionModal, SessionPickerModal
from workspace_choices import find_workspace

_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')


def _strip_ansi(text: str) -> str:
    """harness 的 format_* 报告带 ANSI 色码；Textual 里统一剥掉。"""
    return _ANSI_RE.sub('', str(text))


class SessionActionsMixin:
    """HarnessApp 的会话生命周期与 checkpoint 命令。"""

    def _finish_local_operation(self) -> None:
        self._busy = False
        self._operation = 'agent'
        self._refresh_footer()
        if self._pending.snapshot():
            self._start_worker()

    def _adopt_session(self, store, messages: list, title: str = '') -> None:
        self._store = store
        self._session_id = store.session_id
        self._messages = list(messages)
        self._title = title
        self._status.clear()
        self._tool_blocks.clear()
        self._mention_root = None
        self.model.clear()
        self._load_history(messages)
        self._refresh_footer()
        self._mark_dirty()

    def start_new_session(self, argument: str = '') -> None:
        """`/new [--ws <序号|名称|路径>|会话ID]`（对齐旧 REPL 与 Pi `/new`）。"""
        import harness
        requested = (argument or '').strip()
        target = harness.get_workdir()
        session_id = None
        lowered = requested.lower()
        if lowered.startswith('--'):
            if lowered != '--ws' and not lowered.startswith('--ws '):
                self._notice('用法：/new [--ws <序号|名称|路径>|会话ID]',
                             level='error')
                return
            selector = requested[4:].strip()
            if len(selector) >= 2 and selector[0] == selector[-1] and selector[0] in ('"', "'"):
                selector = selector[1:-1]
            if not selector:
                self._notice('用法：/new --ws <序号|名称|路径>', level='error')
                return
            try:
                target = find_workspace(
                    harness.registered_workspaces(), selector)['path']
            except ValueError as exc:
                # 未登记的目录也允许直接使用，和旧 REPL 保持一致。
                candidate = Path(selector).expanduser()
                if not candidate.is_absolute():
                    candidate = harness.get_workdir() / candidate
                if not candidate.is_dir():
                    self._notice(f'[workspace] {exc}', level='error')
                    return
                target = candidate.resolve()
        elif requested:
            if not requested.replace('-', '').replace('_', '').isalnum():
                self._notice('用法：/new [--ws <序号|名称|路径>|会话ID]',
                             level='error')
                return
            session_id = requested
        try:
            store = harness.start_new_session(workspace=target,
                                              session_id=session_id)
            summary = store.session_summary()
        except Exception as exc:
            self._notice(f'新建会话失败： {exc}', level='error')
            return
        self._adopt_session(store, [], summary.get('title', ''))
        self._notice(f"New session: {store.session_id}")

    def rename_session(self, argument: str) -> None:
        if not self._store:
            self._notice('当前没有会话，无法重命名。', level='error')
            return
        if not argument:
            self._notice('用法：/rename <标题>', level='error')
            return
        import harness
        from session_locks import SessionBusy, session_write

        try:
            with session_write(harness.SESSIONS_DIR, self._store.session_id,
                               action='重命名会话'):
                title = self._store.append_session_title(argument[:80], source='user')
        except SessionBusy as exc:
            self._notice(str(exc), level='error')
            return
        self._title = title['title']
        self._refresh_footer()
        self._notice(f"会话标题： {self._title}")

    def fork_session(self, argument: str = '') -> None:
        """`/fork [事件序号]`；`/clone` 无参复制到当前叶。"""
        if not self._store:
            self._notice('当前没有会话，无法分叉。', level='error')
            return
        upto = None
        if argument:
            if not argument.isdigit():
                self._notice('用法：/fork [事件序号]', level='error')
                return
            upto = int(argument)
        if self._fork_to(upto):
            self._notice(f'已分叉到 {self._session_id} （原会话保留）.')

    def _fork_to(self, upto: int | None) -> bool:
        try:
            forked = self._store.__class__.fork(self._store, upto_seq=upto)
            messages = forked.rebuild_openai_history(recover_pending=True)
            summary = forked.session_summary()
        except Exception as exc:
            self._notice(f'分叉失败： {exc}', level='error')
            return False
        self._adopt_session(forked, messages, summary.get('title', ''))
        return True

    def open_fork_picker(self) -> None:
        """Pi 风格 `/fork`：列出用户消息，默认选最新一条。"""
        if not self._store:
            self._notice('当前没有会话，无法分叉。', level='error')
            return
        rows = self._forkable_messages()
        if not rows:
            self._notice('没有可用来分叉的消息。')
            return
        self._fork_rows = {row['seq']: row['text'] for row in rows}
        self.push_screen(ForkPickerModal(rows), self._on_fork_picked)

    def _forkable_messages(self) -> list[dict]:
        rows = []
        try:
            events = self._store.read_events()
        except Exception:
            return rows
        for event in events:
            if event.get('type') != 'message':
                continue
            message = event.get('message') or {}
            if message.get('role') != 'user':
                continue
            content = message.get('content')
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = ''.join(str(block.get('text', ''))
                               for block in content
                               if isinstance(block, dict)
                               and block.get('type') == 'text')
            else:
                text = ''
            text = text.strip()
            if not text or text.startswith('[Compacted]'):
                continue
            rows.append({'seq': int(event.get('seq', 0)), 'text': text})
        return rows

    def _on_fork_picked(self, seq) -> None:
        if seq is None:
            return
        text = getattr(self, '_fork_rows', {}).get(int(seq), '')
        if not self._fork_to(max(0, int(seq) - 1)):
            return
        if text and self._composer is not None:
            self._programmatic_text = text
            self._composer.text = text
            self._composer.cursor_location = (0, len(text))
            self._composer.focus()
        self._notice(f'已分叉到 {self._session_id}，消息已放回输入框。')

    def show_checkpoints(self) -> None:
        import harness
        if not self._store:
            self._notice('当前没有会话。', level='error')
            return
        try:
            manager = harness.get_checkpoint_manager(self._store)
            self._notice(harness.format_checkpoint_list(manager))
        except Exception as exc:
            self._notice(f'读取快照失败： {exc}', level='error')

    def create_checkpoint(self, label: str = '') -> None:
        """`/checkpoint [label]`：手动快照当前会话（文件前像 + 历史）。"""
        import harness
        if self._busy:
            self._notice('回合进行中，无法创建快照。')
            return
        if not self._store:
            self._notice('当前没有会话。', level='error')
            return
        try:
            manager = harness.get_checkpoint_manager(self._store)
            cp_id = manager.begin_turn(self._messages, self._store,
                                       label=label.strip(), trigger='manual')
            manager.seal()
            self._notice(f'已创建快照 {cp_id}. 可用 /rewind head 回滚。')
        except Exception as exc:
            self._notice(f'创建快照失败： {exc}', level='error')

    def start_checkpoint_gc(self, argument: str) -> None:
        """`/checkpoint-gc [--all] [--keep N]`：当前会话直接回收，全局先确认。"""
        import harness
        try:
            manager = harness.get_checkpoint_manager(self._store)
            preview = harness.checkpoint_gc_preview(manager, argument)
        except ValueError as exc:
            self._notice(str(exc), level='error')
            return
        except Exception as exc:
            self._notice(f'清理快照失败： {exc}', level='error')
            return
        summary = harness.format_checkpoint_gc(preview)
        if not preview['removed']:
            self._notice(summary)
            return
        if preview['mode'] != 'all':
            self._finish_checkpoint_gc(argument)
            return
        self.push_screen(
            ConfirmModal('Clean old checkpoints?', summary, 'Clean'),
            partial(self._confirm_checkpoint_gc, argument=argument))

    def _confirm_checkpoint_gc(self, confirmed, argument: str) -> None:
        if not confirmed:
            self._notice('已取消，未删除任何快照。')
            return
        if self._busy:
            self._notice('当前任务尚未结束，请结束后再清理快照。')
            return
        self._finish_checkpoint_gc(argument)

    def _finish_checkpoint_gc(self, argument: str) -> None:
        import harness
        try:
            result = harness.checkpoint_gc_execute(
                harness.get_checkpoint_manager(self._store), argument)
            self._notice(harness.format_checkpoint_gc(result, done=True))
        except Exception as exc:
            self._notice(f'清理快照失败： {exc}', level='error')

    def start_rewind(self, argument: str) -> None:
        """`/rewind`：先预览，再确认；`--yes` 跳过确认，`/undo` 即 turn --yes。"""
        import harness
        if self._busy:
            self._notice('回合进行中，无法回溯。')
            return
        if not self._store:
            self._notice('当前没有会话。', level='error')
            return
        argument = (argument or '').strip()
        error = harness.command_error('/rewind', argument)
        if error:
            self._notice(error, level='error')
            return
        parts = argument.split()
        ref = next((part for part in parts if not part.startswith('--')), 'turn')
        force = '--force' in parts
        scope = 'session' if '--session' in parts else 'all'
        try:
            manager = harness.get_checkpoint_manager(self._store)
            cp_id = manager.resolve(ref)
            if not manager.snapshot_path(cp_id).exists():
                self._notice(f'[回溯] 缺少快照： {cp_id}', level='error')
                return
            preview = manager.plan_restore(cp_id, scope=scope)
            preview['scope'] = scope
        except Exception as exc:
            self._notice(f'[rewind] {exc}', level='error')
            return
        report = _strip_ansi(harness.format_restore_report(preview))
        if preview['conflicts'] and not force:
            self._notice(report + chr(10) + '未执行任何改动；确认可覆盖后重试 '
                         f'/rewind {cp_id} --force')
            return
        if not preview.get('ok'):
            self._notice(report)
            return
        if '--yes' in parts:
            self._execute_rewind(True, cp_id, force, scope)
            return
        self.push_screen(
            ConfirmModal('Rewind checkpoint?', report, 'Rewind'),
            # callback 先收到 True/False，再带上用于执行的参数
            partial(self._execute_rewind, cp_id=cp_id,
                    force=force, scope=scope),
        )

    def _execute_rewind(self, confirmed: bool, cp_id: str,
                        force: bool, scope: str) -> None:
        if not confirmed or self._busy or not self._store:
            return
        import harness
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                ok = harness.rewind_to(
                    self._messages, self._store,
                    harness.get_checkpoint_manager(self._store),
                    cp_id, force=force, scope=scope, report=False)
        except Exception as exc:
            self._notice(f'[rewind] {exc}', level='error')
            return
        detail = _strip_ansi(output.getvalue()).strip()
        if not ok:
            self._notice(detail or f'[回溯] 无法恢复 {cp_id}',
                         level='error')
            return
        self._reload_transcript()
        self._notice(f'Rewound to {cp_id}.' + (chr(10) + detail if detail else ''))

    def _reload_transcript(self) -> None:
        """回溯会替换 canonical history，按它重建块模型。"""
        self.model.clear()
        self._load_history(self._messages)
        self._mark_dirty()
        self._flush_layout(force=True)
        self._refresh_footer()

    def start_compact(self, instructions: str = '') -> None:
        """`/compact`：后台压缩当前上下文（Pi `/compact [instructions]`）。"""
        if self._busy:
            self._notice('回合进行中，无法压缩上下文。')
            return
        if not self._store:
            self._notice('当前没有会话。', level='error')
            return
        if not self._messages:
            self._notice('当前会话没有可压缩的内容。')
            return
        self._busy = True
        self._operation = 'compact'
        self._turn_started = time.monotonic()
        block = self.model.append('notice', {'text': '压缩中…'})
        self._mark_dirty()
        threading.Thread(target=self._run_compact,
                         args=(block.id, list(self._messages)),
                         daemon=True).start()

    def _run_compact(self, block_id: str, messages: list) -> None:
        import harness
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                compacted = harness.compact_history(list(messages))
                harness._record_compaction(self._store, compacted, 'manual')
        except Exception as exc:
            self.call_from_thread(self._compact_done, block_id, None,
                                  f'Compact failed: {exc}')
            return
        self.call_from_thread(self._compact_done, block_id, compacted, '')

    def _compact_done(self, block_id: str, compacted, error: str = '') -> None:
        self._busy = False
        if compacted is not None:
            self._messages[:] = compacted
            text = (f'已压缩会话上下文：{len(compacted)} 条消息。'
                    '完整记录已保存。')
        else:
            text = error
        if self.model.get(block_id) is not None:
            self.model.update(block_id, text=text)
        self._mark_dirty()
        self._refresh_footer()
        self._operation = 'agent'
        if self._pending.snapshot() and not self._busy:
            self._start_worker()

    def open_session_picker(self) -> None:
        import harness
        rows = harness.session_rows(limit=None, include_empty=False)
        if not rows:
            # 空列表容易被误读成“会话丢了”：把实际使用的目录和 home 一起报出来。
            self._notice(
                '还没有保存的会话。' + chr(10)
                + f'会话目录：{harness.SESSIONS_DIR}' + chr(10)
                + f'数据根目录：{harness.HOME_DIR}（可用环境变量 HARNESS_HOME 指定）')
            return
        current_workspace = harness.get_workdir()
        self.push_screen(
            SessionPickerModal(rows, current_workspace=current_workspace),
            self._on_session_picked)

    def _on_session_picked(self, result) -> None:
        if not result:
            return
        if isinstance(result, dict) and '_session_picker_action' in result:
            action = result['_session_picker_action']
            row = result['row']
            if action == 'rename':
                self.push_screen(
                    RenameSessionModal(str(row.get('title') or '')),
                    partial(self._rename_picker_session, row=row))
            else:
                self.start_picker_session_action(action, row)
            return
        asyncio.create_task(self._resume_session_row(result))

    def _rename_picker_session(self, title: str | None, row: dict) -> None:
        if not title:
            return
        if self._busy:
            self._notice('回合进行中，无法重命名会话。')
            return
        import harness
        if title == str(row.get('title') or ''):
            self._notice('标题没有变化。')
            return
        try:
            if row['id'] == self._session_id and self._store is not None:
                store = self._store
            else:
                store = harness.SessionEventStore.open(
                    harness.resolve_session_path(row['id']))
            event = store.append_session_title(title[:80], source='user')
        except Exception as exc:
            self._notice(f'重命名会话失败： {exc}', level='error')
            return
        if row['id'] == self._session_id:
            self._title = event['title']
            self._refresh_footer()
        self._notice(f"会话标题： {event['title']}")

    async def _resume_by_argument(self, argument: str) -> None:
        import harness
        rows = harness.session_rows(limit=None, include_empty=False)
        if not rows:
            self._notice('还没有保存的会话。')
            return
        selected = None
        if argument.casefold() == 'last':
            selected = rows[0]
        elif argument.isdigit():
            index = int(argument)
            if 1 <= index <= len(rows):
                selected = rows[index - 1]
        else:
            matches = [row for row in rows
                       if row['id'].startswith(argument)
                       or argument.casefold() in (row['title'] or '').casefold()]
            if len(matches) == 1:
                selected = matches[0]
            elif len(matches) > 1:
                self._notice(f'{len(matches)} 个会话匹配；用 /sessions 查看。')
                return
        if selected is None:
            self._notice(f'没有匹配的会话 {argument!r}.')
            return
        await self._resume_session_row(selected)

    async def _resume_session_row(self, row: dict) -> None:
        import harness
        if self._busy:
            self._notice('当前任务尚未结束，请结束后再恢复会话。')
            return
        try:
            store = harness.SessionEventStore.open(
                harness.resolve_session_path(row['id']))
            messages = store.rebuild_openai_history(recover_pending=True)
            summary = store.session_summary()
        except Exception as exc:
            self._notice(f'无法恢复会话： {exc}', level='error')
            return
        try:
            if summary.get('workspace'):
                harness.set_workspace(summary['workspace'])
        except (OSError, ValueError) as exc:
            self._notice(f'无法恢复该工作区： {exc}', level='error')
            return
        self._mention_root = None
        self._adopt_session(store, messages, summary.get('title', ''))
        self._notice(f"Resumed {summary['title']}.")
