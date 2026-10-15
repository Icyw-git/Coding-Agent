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

Security note: the caller constrains where tasks may run via ``allowed_root``;
the HTTP adapter wires this to the server workspace so the gateway can never
point ``cwd`` at arbitrary machine paths.
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
from process_utils import kill_process_tree

TERMINAL_STATUSES = {'succeeded', 'failed', 'cancelled', 'interrupted'}
ACTIVE_STATUSES = {'queued', 'running'}


@dataclass
class TaskRecord:
    id: str
    command: str
    cwd: str
    status: str                 # queued | running | succeeded | failed | cancelled | interrupted
    created_at: float
    started_at: float | None = None
    ended_at: float | None = None
    exit_code: int | None = None
    pid: int | None = None
    log_path: str = ''
    detail: str = ''


class TaskManager:
    """Create, observe, and stop background commands with persisted state."""

    def __init__(self, root, allowed_root=None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.allowed_root = (
            Path(allowed_root).resolve() if allowed_root else None
        )
        self._lock = threading.RLock()
        self._tasks: dict[str, TaskRecord] = {}
        self._procs: dict[str, subprocess.Popen] = {}
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
        if self.allowed_root is not None and base != self.allowed_root \
                and self.allowed_root not in base.parents:
            raise ValueError(
                f'cwd {base} is outside the allowed root {self.allowed_root}'
            )
        return base

    def _run(self, record: TaskRecord) -> None:
        proc = None
        try:
            # Cancellation cannot interleave between the check, spawn and registration.
            with self._lock:
                if record.status != 'queued':
                    return
                handle = open(record.log_path, 'w', encoding='utf-8')
                try:
                    handle.write(f'$ {record.command}\n[cwd] {record.cwd}\n')
                    handle.flush()
                    proc = subprocess.Popen(
                        record.command, cwd=record.cwd, shell=True,
                        stdout=handle, stderr=subprocess.STDOUT,
                        start_new_session=os.name != 'nt',
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
                    record.detail = f'{type(exc).__name__}: {exc}'
                self._save(record)
        finally:
            with self._lock:
                self._threads.pop(record.id, None)

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """Kill the process and its children (cmd.exe spawns intermediaries)."""
        kill_process_tree(proc)

    def _save(self, record: TaskRecord) -> None:
        path = self.root / f'{record.id}.json'
        payload = json.dumps(asdict(record), ensure_ascii=False, indent=2)
        # 唯一临时名：并发写者（不同 manager 实例/线程）互不踩踏；Windows 上
        # os.replace 目标被短暂占用（杀毒/索引器）时退避重试。
        tmp = path.with_name(f'{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
        for attempt in range(3):
            try:
                tmp.write_text(payload, encoding='utf-8')
                os.replace(tmp, path)
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
