"""Process-level task management for server-side experiments.

A task is one long-running command (experiment / training / shell job) tracked
with a full state machine and persisted to disk so status survives page
refreshes and API restarts:

    queued → running → succeeded | failed
                 ↓
             cancelled

Layout under the task root directory:

    <root>/<task_id>.json   task record (atomic writes)
    <root>/<task_id>.log    combined stdout+stderr of the process

Security note: the caller constrains where tasks may run via ``allowed_root``,
and decides *whether* a command may run at all via ``enforce`` — the HTTP
adapter wires both to the server workspace and to the execution policy layer
(hooks.enforce_headless). Without ``enforce`` this manager spawns whatever it
is given, exactly as it did before the policy layer existed.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from process_utils import close_process, kill_process_tree
from sandbox import SandboxProcess, spawn_shell

TERMINAL_STATUSES = {'succeeded', 'failed', 'cancelled', 'interrupted'}
ACTIVE_STATUSES = {'queued', 'running'}


def _duration_ms(record: 'TaskRecord') -> int | None:
    if record.started_at is None or record.ended_at is None:
        return None
    return max(0, round((record.ended_at - record.started_at) * 1000))


@dataclass
class TaskRecord:
    id: str
    command: str
    cwd: str
    status: str                 # queued | running | succeeded | failed | cancelled | interrupted
    created_at: float
    started_at: float | None = None
    ended_at: float | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    pid: int | None = None
    log_path: str = ''
    detail: str = ''


class TaskManager:
    """Create, observe, and stop background commands with persisted state."""

    def __init__(self, root, allowed_root=None, enforce=None, allowed_roots=None) -> None:
        """enforce: 可选的 command -> 拒绝语|None 判定（执行策略层接线用）。"""
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if allowed_roots is None:
            allowed_roots = ()
        elif isinstance(allowed_roots, (str, os.PathLike)):
            allowed_roots = (allowed_roots,)
        roots = []
        seen = set()
        for value in (*allowed_roots, allowed_root):
            if value is None:
                continue
            candidate = Path(value).resolve()
            key = os.path.normcase(str(candidate))
            if key not in seen:
                seen.add(key)
                roots.append(candidate)
        self.allowed_roots = roots or None
        self.allowed_root = roots[0] if roots else None
        self._enforce = enforce
        self._lock = threading.RLock()
        self._tasks: dict[str, TaskRecord] = {}
        self._procs: dict[str, subprocess.Popen | SandboxProcess] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._closed = False
        self._load_all()

    # ---------- public API ----------

    def create(self, command: str, cwd: str | None = None) -> TaskRecord:
        """Validate inputs, persist a queued task, and start its runner thread."""
        if not isinstance(command, str) or not command.strip():
            raise ValueError('command must be a non-empty string')
        workdir = self._validate_cwd(cwd)
        task_id = f'task_{uuid.uuid4().hex[:12]}'
        record = TaskRecord(
            id=task_id,
            command=command.strip(),
            cwd=str(workdir),
            status='queued',
            created_at=time.time(),
            log_path=str(self.root / f'{task_id}.log'),
        )
        with self._lock:
            if self._closed:
                raise RuntimeError('task manager is closed')
            self._tasks[task_id] = record
            self._save(record)
            thread = threading.Thread(target=self._run, args=(record,), daemon=True)
            self._threads[task_id] = thread
            thread.start()
        return record

    def get(self, task_id: str) -> TaskRecord:
        with self._lock:
            record = self._tasks.get(task_id)
        if record is None:
            raise KeyError(task_id)
        return record

    def list(self, limit: int = 50) -> list[TaskRecord]:
        with self._lock:
            records = sorted(self._tasks.values(),
                             key=lambda r: r.created_at, reverse=True)
        return records[:max(1, limit)]

    def stop(self, task_id: str) -> TaskRecord:
        """Cancel a queued task or terminate an owned running process tree."""
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise KeyError(task_id)
            if record.status not in ACTIVE_STATUSES:
                raise RuntimeError(f'task {task_id} is not running')
            proc = self._procs.get(task_id)
            if proc is not None:
                try:
                    self._kill_tree(proc)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise RuntimeError('could not terminate task process tree') from exc
            record.status = 'cancelled'
            record.ended_at = time.time()
            record.duration_ms = _duration_ms(record)
            record.detail = 'cancelled by request'
            self._save(record)
        return record

    def close(self) -> None:
        """Stop owned tasks and wait for their logs and final records to flush."""
        with self._lock:
            self._closed = True
            active = [r.id for r in self._tasks.values() if r.status in ACTIVE_STATUSES]
            errors = []
            for task_id in active:
                try:
                    self.stop(task_id)
                except RuntimeError as exc:
                    errors.append(str(exc))
            threads = [thread for task_id, thread in self._threads.items()
                       if self._tasks[task_id].status not in ACTIVE_STATUSES]
        for thread in threads:
            thread.join()
        if errors:
            raise RuntimeError('; '.join(errors))

    def tail_logs(self, task_id: str, lines: int = 200) -> str:
        """Return the last N lines of a task's combined output."""
        record = self.get(task_id)
        limit = max(1, min(int(lines), 5000))
        try:
            with open(record.log_path, 'r', encoding='utf-8', errors='replace') as handle:
                return ''.join(deque(handle, maxlen=limit))
        except FileNotFoundError:
            return ''

    # ---------- internals ----------

    def _validate_cwd(self, cwd: str | None) -> Path:
        base = Path(cwd).resolve() if cwd else (
            self.allowed_root or Path.cwd()
        )
        if not base.is_dir():
            raise ValueError(f'cwd does not exist: {base}')
        if self.allowed_roots and not any(
                base == root or root in base.parents for root in self.allowed_roots):
            shown = (self.allowed_roots[0] if len(self.allowed_roots) == 1
                     else self.allowed_roots)
            raise ValueError(
                f'cwd {base} is outside the allowed root {shown}'
            )
        return base

    def _run(self, record: TaskRecord) -> None:
        proc = None
        try:
            # 第四条执行路径：HTTP 建的任务不经过 PreToolUse hook，Popen 之前必须自己判。
            # 判在 runner 里而不是 create 里，是为了让 POST 立刻返回 queued：需要审批的
            # 命令停在队列里等人决议，既不占用请求线程，也不让 HTTP 客户端挂到超时。
            blocked = self._enforce(record.command) if self._enforce is not None else None
            if blocked is not None:
                with self._lock:
                    if record.status == 'queued':   # 等审批期间可能已被 stop
                        record.status = 'cancelled'
                        record.ended_at = time.time()
                        record.duration_ms = _duration_ms(record)
                        record.detail = blocked
                        self._save(record)
                return
            # Cancellation cannot interleave between the check, spawn and registration.
            with self._lock:
                if record.status != 'queued':
                    return
                handle = open(record.log_path, 'w', encoding='utf-8')
                try:
                    handle.write(f'$ {record.command}\n[cwd] {record.cwd}\n')
                    handle.flush()
                    sandbox_root = None
                    if self.allowed_roots:
                        cwd_path = Path(record.cwd)
                        # 嵌套根取最具体的一条：否则 [C:\, C:\proj] 会把沙箱
                        # 放宽到 C:\，多根反而比单根更松。
                        sandbox_root = max(
                            (root for root in self.allowed_roots
                             if cwd_path.is_relative_to(root)),
                            key=lambda root: len(root.parts), default=cwd_path)
                    proc = spawn_shell(
                        record.command, cwd=record.cwd, stdout=handle,
                        workspace=str(sandbox_root) if sandbox_root is not None else None,
                    )
                    record.pid = proc.pid
                    self._procs[record.id] = proc
                    record.status = 'running'
                    record.started_at = time.time()
                    self._save(record)
                except Exception:
                    handle.close()
                    raise
            with handle:
                code = proc.wait()
                handle.write(f'\n[exit {code}]\n')
            with self._lock:
                self._procs.pop(record.id, None)
                record.exit_code = code
                record.ended_at = time.time()
                record.duration_ms = _duration_ms(record)
                if record.status not in TERMINAL_STATUSES:
                    record.status = 'succeeded' if code == 0 else 'failed'
                self._save(record)
        except Exception as exc:   # spawn failures (bad cwd, OS errors)
            if proc is not None and proc.poll() is None:
                self._kill_tree(proc)
            with self._lock:
                self._procs.pop(record.id, None)
                if record.status not in TERMINAL_STATUSES:
                    record.status = 'failed'
                    record.ended_at = time.time()
                    record.duration_ms = _duration_ms(record)
                    record.detail = f'{type(exc).__name__}: {exc}'
                self._save(record)
        finally:
            close_process(proc)
            with self._lock:
                self._threads.pop(record.id, None)

    @staticmethod
    def _kill_tree(proc: subprocess.Popen | SandboxProcess) -> None:
        """Kill the process and its children (cmd.exe spawns intermediaries)."""
        kill_process_tree(proc)

    def _save(self, record: TaskRecord) -> None:
        path = self.root / f'{record.id}.json'
        payload = json.dumps(asdict(record), ensure_ascii=False, indent=2)
        # 唯一临时名：并发写者（不同 manager 实例/线程）互不踩踏；Windows 上
        # Path.replace 目标被短暂占用（杀毒/索引器）时退避重试。
        tmp = path.with_name(f'{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
        for attempt in range(3):
            try:
                tmp.write_text(payload, encoding='utf-8')
                tmp.replace(path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def _load_all(self) -> None:
        names = {f.name for f in fields(TaskRecord)}
        for path in self.root.glob('task_*.json'):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                record = TaskRecord(**{k: data[k] for k in names if k in data})
            except (OSError, json.JSONDecodeError, TypeError):
                continue   # foreign/corrupt file: not one of ours, skip it
            if record.status in ACTIVE_STATUSES:
                # An unclean exit loses ownership; a saved PID may already be reused.
                # Do not claim the command exited or kill an unverified process.
                record.status = 'interrupted'
                record.detail = 'process state unknown after server restart; check before rerunning'
                self._save(record)
            self._tasks[record.id] = record
