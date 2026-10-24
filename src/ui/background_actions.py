"""本地后台动作：`!command` shell 执行、`/review` 审阅与 `/watch` 轮询。

Shell 输出回写为工具块并占用本地操作状态；review/watch 不占对话回合，结果写进
notice 块。后台进度通过 `call_from_thread` 回到 UI 线程。
"""
from __future__ import annotations

import threading
import time


class BackgroundActionsMixin:
    """把后台进度和最终文本折叠进同一个 notice 块。"""

    def submit_shell(self, command: str) -> None:
        """`!cmd`：直接执行 shell，输出按工具块展示，不进入模型上下文。"""
        if self._busy:
            self._notice('当前任务尚未结束，请结束后再执行 shell 命令。')
            return
        self._busy = True
        self._operation = 'shell'
        self._turn_started = time.monotonic()
        block = self.model.append('tool', {
            'name': 'bash', 'args': {'command': command},
            'state': 'running', 'output': '', 'expanded': False})
        self._mark_dirty()
        threading.Thread(target=self._run_shell,
                         args=(block.id, command), daemon=True).start()

    def _run_shell(self, block_id: str, command: str) -> None:
        import harness
        import turn_control
        started = time.monotonic()
        output = ''
        try:
            harness.permission_gate.use_ui = True
            turn_control.begin_turn()
            output = harness.run_bash(command)
        except Exception as exc:
            output = f'Error: {exc}'
        finally:
            turn_control.end_turn()
            harness.permission_gate.use_ui = False
        duration = int((time.monotonic() - started) * 1000)
        try:
            self.call_from_thread(self._finish_shell, block_id, output, duration)
        except Exception:
            pass

    def _finish_shell(self, block_id: str, output: str, duration_ms: int) -> None:
        failed = str(output).lstrip().startswith(('Error', 'Permission denied'))
        if self.model.get(block_id) is not None:
            self.model.update(block_id,
                              state='failed' if failed else 'complete',
                              output=str(output), duration_ms=duration_ms)
        self._mark_dirty()
        self._finish_local_operation()

    def start_review(self) -> None:
        """`/review`：后台只读审阅，进度/结果写进同一块（不占对话回合）。"""
        if self._busy:
            self._notice('Cannot review while a request is running.')
            return
        block = self.model.append('notice', {'text': 'review: running…'})
        self._mark_dirty()
        threading.Thread(target=self._run_review,
                         args=(block.id,), daemon=True).start()

    def _run_review(self, block_id: str) -> None:
        from review import run_review

        def progress(text: str) -> None:
            try:
                self.call_from_thread(self._review_progress, block_id, text)
            except Exception:
                pass

        try:
            result = run_review(progress=progress)
        except Exception as exc:
            result = f'Review failed: {type(exc).__name__}: {exc}'
        try:
            self.call_from_thread(self._review_done, block_id, result)
        except Exception:
            pass

    def _review_progress(self, block_id: str, text: str) -> None:
        if self.model.get(block_id) is not None:
            self.model.update(block_id, text=str(text))
            self._mark_dirty()

    def _review_done(self, block_id: str, text: str) -> None:
        if self.model.get(block_id) is not None:
            self.model.update(block_id, text=str(text))
            self._mark_dirty()

    def start_watch(self, argument: str) -> None:
        """`/watch`：后台线程轮询，进度与最终输出写进同一块。"""
        block = self.model.append('notice', {'text': 'watch: 等待后台任务…'})
        self._mark_dirty()
        threading.Thread(target=self._run_watch,
                         args=(block.id, argument), daemon=True).start()

    def _run_watch(self, block_id: str, argument: str) -> None:
        from background_watch import watch_command
        lines = []

        def progress(text: str) -> None:
            lines.append(str(text))
            try:
                self.call_from_thread(self._watch_progress, block_id, list(lines))
            except Exception:
                pass

        try:
            result = watch_command(argument, emit=progress)
        except Exception as exc:
            result = f'Watch failed: {type(exc).__name__}: {exc}'
        try:
            self.call_from_thread(self._watch_done, block_id, result)
        except Exception:
            pass

    def _watch_progress(self, block_id: str, lines: list) -> None:
        if self.model.get(block_id) is not None:
            self.model.update(block_id,
                              text='watch:' + chr(10) + chr(10).join(lines[-8:]))
            self._mark_dirty()

    def _watch_done(self, block_id: str, result: str) -> None:
        if self.model.get(block_id) is not None:
            self.model.update(block_id, text=str(result))
            self._mark_dirty()
