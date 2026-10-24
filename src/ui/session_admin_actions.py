"""TUI 的会话管理动作：工作区切换、跨会话搜索、归档/恢复/删除。

对应 `session_admin.py` 的只读/轻量写接口，以 mixin 形式拆出。方法依赖
HarnessApp 的运行时属性（model/store/_busy/_notice 等），但不持有额外状态；
后台线程只做扫描，结果统一经 `call_from_thread` 回到 UI 线程更新块。
"""
from __future__ import annotations

import asyncio
import threading
import time
from functools import partial

from ui.modals.choices import ChoiceModal
from ui.modals.cleanup import CleanupPickerModal, TypedConfirmModal
from ui.modals.confirm import ConfirmModal


class SessionAdminActionsMixin:
    """HarnessApp 的工作区、搜索与清理命令。"""

    # ---------- 工作区 ----------

    def show_workspaces(self) -> None:
        import harness
        self._notice(harness.format_workspace_list())

    def handle_workspace(self, argument: str) -> None:
        """`/workspace [list|add <路径> [--name 名称]|remove [选择器]]`。"""
        import harness
        parts = (argument or '').split(maxsplit=1)
        action = parts[0].lower() if parts else 'list'
        if action in ('', 'list'):
            self.show_workspaces()
            return
        if action == 'add':
            try:
                entry = harness.add_workspace_from_argument(
                    parts[1] if len(parts) > 1 else '')
            except (OSError, ValueError) as exc:
                self._notice(f'[workspace] {exc}', level='error')
                return
            self._notice(f"已登记工作区：{entry['name']}  {entry['path']}")
            return
        if action == 'remove':
            selector = parts[1].strip() if len(parts) > 1 else ''
            if not selector:
                self.open_workspace_remover()
                return
            try:
                removed = harness.remove_workspace_selector(selector)
            except (KeyError, OSError, ValueError) as exc:
                self._notice(f'[workspace] {exc}', level='error')
                return
            self._notice(f"已移除登记：{removed['name']}  {removed['path']}")
            return
        self._notice('用法：/workspaces；/workspace add <路径> [--name 名称]；'
                     '/workspace remove [序号|名称|路径]', level='error')

    def open_workspace_remover(self) -> None:
        import harness
        entries = harness.registered_workspaces()
        if not entries:
            self._notice('尚未登记工作区。')
            return
        rows = [{'value': entry['id'],
                 'label': f"{index}. {entry['name']}  {entry['path']}",
                 'search': f"{entry['name']} {entry['path']} {entry['id']}"}
                for index, entry in enumerate(entries, 1)]
        self.push_screen(
            ChoiceModal('Remove workspace', rows,
                        hint='Enter 移除登记 · Esc 取消'),
            self._on_workspace_remove_picked)

    def _on_workspace_remove_picked(self, workspace_id) -> None:
        if not workspace_id:
            return
        import harness
        try:
            removed = harness.workspaces.remove_workspace(
                str(workspace_id), file=harness.workspace_registry_path())
        except (KeyError, OSError, ValueError) as exc:
            self._notice(f'[workspace] {exc}', level='error')
            return
        self._notice(f"已移除登记：{removed['name']}  {removed['path']}")

    def change_workspace(self, argument: str) -> None:
        """`/cd [路径]`：只切 Agent 工作区，不改进程 cwd（同旧 REPL）。"""
        import harness
        argument = (argument or '').strip()
        if not argument:
            self._notice(f'工作区：{harness.get_workdir()}')
            return
        path = argument
        if len(path) >= 2 and path[0] == path[-1] and path[0] in ('"', "'"):
            path = path[1:-1]
        try:
            workspace = harness.set_workspace(path)
            if self._store is not None:
                self._store.record_workspace(workspace)
        except (OSError, ValueError) as exc:
            self._notice(f'[workspace] {exc}', level='error')
            return
        self._mention_root = None      # @ 文件索引跟随工作区重扫
        self._refresh_footer()
        self._notice(f'工作区：{workspace}')

    # ---------- 跨会话搜索 ----------

    def start_find(self, argument: str) -> None:
        import harness
        argument = (argument or '').strip()
        error = harness.command_error('/find', argument)
        if error:
            self._notice(error, level='error')
            return
        block = self.model.append('notice',
                                  {'text': f'find: 搜索「{argument}」…'})
        self._mark_dirty()
        threading.Thread(target=self._run_find,
                         args=(block.id, argument), daemon=True).start()

    def _run_find(self, block_id: str, query: str) -> None:
        import harness
        try:
            rows = harness.SessionEventStore.search_all(
                harness.SESSIONS_DIR, query, limit=8)
            error = ''
        except Exception as exc:
            rows = []
            error = f'{type(exc).__name__}: {exc}'
        try:
            self.call_from_thread(self._find_done, block_id, query, rows, error)
        except Exception:
            pass

    def _find_done(self, block_id: str, query: str, rows: list,
                   error: str = '') -> None:
        if self.model.get(block_id) is None:
            return
        from turn_progress import relative_time
        if self.model.get(block_id) is not None:
            if error:
                text = f'find: {error}'
            elif rows:
                text = f'find: {len(rows)} 个会话命中「{query}」。'
            else:
                text = f'没有包含「{query}」的会话。'
            self.model.update(block_id, text=text,
                              level='error' if error else 'info')
            self._mark_dirty()
        if error or not rows:
            return
        self._find_rows = {row['id']: row for row in rows}
        choices = [{
            'value': row['id'],
            'label': (f"{row['title']}  ·  {relative_time(row.get('updated'))}"
                      f"  ·  {row['id'][:8]}"),
            'search': ' '.join(str(row.get(key) or '') for key in
                               ('title', 'workspace', 'id', 'matched')),
        } for row in rows]
        self.push_screen(
            ChoiceModal('Find sessions', choices,
                        hint='Enter 恢复会话 · Esc 关闭'),
            self._on_find_picked)

    def _on_find_picked(self, session_id) -> None:
        if not session_id:
            return
        row = getattr(self, '_find_rows', {}).get(str(session_id))
        if row is not None:
            asyncio.create_task(self._resume_session_row(row))

    # ---------- 会话归档 / 删除 ----------

    def start_cleanup(self, command: str, argument: str) -> None:
        """`/archive`、`/delete`：后台扫描候选，再打开多选与确认。"""
        import harness
        if self._busy:
            self._notice('Cannot clean up sessions while a request is running.')
            return
        if not self._store:
            self._notice('No session context.', level='error')
            return
        error = harness.command_error('/' + command, argument)
        if error:
            self._notice(error, level='error')
            return
        mode = ('delete' if command == 'delete'
                else 'restore' if argument == 'restore' else 'archive')
        block = self.model.append('notice',
                                  {'text': f'cleanup: 扫描{mode}候选…'})
        self._mark_dirty()
        threading.Thread(target=self._scan_cleanup,
                         args=(block.id, command, argument), daemon=True).start()

    def _scan_cleanup(self, block_id: str, command: str, argument: str) -> None:
        import harness
        try:
            harness.wait_for_housekeeping()
            if harness.housekeeping_busy():
                raise RuntimeError('会话后台整理尚未结束，请稍后再清理。')
            archive, rows = harness.session_cleanup_plan(
                command, argument, self._session_id, emit=lambda text: None)
        except Exception as exc:
            try:
                self.call_from_thread(
                    self._cleanup_failed, block_id,
                    f'{type(exc).__name__}: {exc}')
            except Exception:
                pass
            return
        try:
            self.call_from_thread(self._cleanup_scan_done, block_id,
                                  archive, rows, command, argument)
        except Exception:
            pass

    def _cleanup_failed(self, block_id: str, message: str) -> None:
        if self.model.get(block_id) is not None:
            self.model.update(block_id, text=f'[cleanup] {message}',
                              level='error')
            self._mark_dirty()
        if self._cleanup_block_id == block_id:
            self._cleanup_block_id = None
            self._finish_local_operation()

    def _cleanup_scan_done(self, block_id: str, archive, rows: list,
                           command: str, argument: str) -> None:
        if self.model.get(block_id) is None:
            return
        mode = ('delete' if command == 'delete'
                else 'restore' if argument == 'restore' else 'archive')
        if self._busy:
            self._cleanup_failed(block_id, '当前任务尚未结束，请稍后重新清理。')
            return
        if self.model.get(block_id) is not None:
            if rows:
                text = f'[cleanup] {len(rows)} 个会话可{self._cleanup_action(mode)}。'
            elif mode in ('delete', 'restore'):
                text = '[cleanup] 没有已归档的会话。'
            else:
                text = '[cleanup] 没有符合条件的会话。'
            self.model.update(block_id, text=text)
            self._mark_dirty()
        if not rows:
            return
        self.push_screen(
            CleanupPickerModal(rows, mode=mode),
            partial(self._on_cleanup_selected,
                    archive=archive, rows=rows, mode=mode))

    @staticmethod
    def _cleanup_action(mode: str) -> str:
        return {'delete': '永久删除', 'restore': '恢复'}.get(mode, '归档')

    def _on_cleanup_selected(self, selected, archive, rows: list,
                             mode: str) -> None:
        if not selected:
            self._notice('已取消，未执行改动。')
            return
        chosen_ids = set(selected)
        chosen = [row for row in rows if row['id'] in chosen_ids]
        action = self._cleanup_action(mode)
        detail = chr(10).join(
            f"  {row.get('title') or '未命名任务'} · "
            f"{row.get('workspace') or '目录未记录'}"
            for row in chosen)
        body = (f'将{action}以下 {len(chosen)} 个会话（含关联快照）：'
                + chr(10) + detail)
        if mode == 'delete':
            body += chr(10) + '记录及关联快照不可恢复；项目文件不受影响。'
            self.push_screen(
                TypedConfirmModal('Delete archived sessions?', body,
                                  phrase='删除'),
                partial(self._execute_cleanup,
                        archive=archive, rows=chosen, mode=mode))
            return
        label = 'Restore' if mode == 'restore' else 'Archive'
        self.push_screen(
            ConfirmModal(f'{action}会话?', body, label),
            partial(self._execute_cleanup,
                    archive=archive, rows=chosen, mode=mode))

    def _execute_cleanup(self, confirmed, archive, rows: list,
                         mode: str) -> None:
        import harness
        if not confirmed:
            self._notice('已取消，未执行改动。')
            return
        if self._busy or harness.housekeeping_busy():
            self._notice('当前任务或后台整理尚未结束，请稍后重新清理。')
            return
        self._busy = True
        self._operation = 'cleanup'
        self._turn_started = time.monotonic()
        block = self.model.append(
            'notice', {'text': f'[cleanup] {self._cleanup_action(mode)}中…'})
        self._cleanup_block_id = block.id
        self._mark_dirty()
        threading.Thread(target=self._run_cleanup,
                         args=(block.id, archive, rows, mode),
                         daemon=True).start()

    def _run_cleanup(self, block_id: str, archive, rows: list,
                     mode: str) -> None:
        import harness
        try:
            completed, failures = harness.apply_session_cleanup(
                archive, rows, mode, self._session_id)
        except Exception as exc:
            try:
                self.call_from_thread(
                    self._cleanup_failed, block_id,
                    f'{type(exc).__name__}: {exc}')
            except Exception:
                pass
            return
        try:
            self.call_from_thread(self._cleanup_done, block_id,
                                  completed, len(rows), failures, mode)
        except Exception:
            pass

    def _cleanup_done(self, block_id: str, completed: int, total: int,
                      failures: list, mode: str) -> None:
        action = self._cleanup_action(mode)
        lines = [f'[cleanup] 已{action} {completed}/{total} 个会话。']
        if mode == 'delete':
            lines.append('记录及关联快照不可恢复，项目文件未改动。')
        elif mode == 'restore':
            lines.append('可用 /resume 继续会话。')
        else:
            lines.append('用 /archive restore 可恢复；项目文件未改动。')
        for row, error in failures:
            lines.append(f"未处理 {row.get('title') or row['id']}：{error}")
        if self.model.get(block_id) is not None:
            self.model.update(block_id, text=chr(10).join(lines),
                              level='error' if failures else 'info')
            self._mark_dirty()
            self._refresh_footer()
        if self._cleanup_block_id == block_id:
            self._cleanup_block_id = None
            self._finish_local_operation()
