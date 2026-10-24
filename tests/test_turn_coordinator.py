"""阶段 0：跨进程回合协调 + 可停止 scheduler 的行为回归。"""
import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import agent_runtime
import cron_scheduler
import harness
import turn_coordinator
from scheduler_service import SchedulerService
from session_store import SessionEventStore

ROOT = Path(__file__).resolve().parents[1]


def _run_python(code: str):
    """启动一个独立解释器，stdout 按行读。"""
    return subprocess.Popen(
        [sys.executable, '-c', textwrap.dedent(code)],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding='utf-8')


def _wait_line(process, expected='locked', timeout=20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if line:
            assert line.strip() == expected, line
            return line
        if process.poll() is not None:
            raise AssertionError(process.stderr.read())
        time.sleep(0.01)
    process.kill()
    raise AssertionError('child did not report holding the lock')


def _child_holder(base: Path, crash: bool = False) -> str:
    exit_line = 'import os; os._exit(7)' if crash else 'sys.stdin.readline()'
    return f'''
        import sys
        sys.path.insert(0, r"{ROOT / 'src'}")
        from turn_coordinator import acquire
        slot = acquire(r"{base}")
        print("locked", flush=True)
        {exit_line}
    '''


def test_same_thread_reentry_nests_and_other_thread_is_rejected(tmp_path):
    first = turn_coordinator.acquire(tmp_path)
    try:
        with turn_coordinator.turn_slot(tmp_path):
            assert turn_coordinator.held()
        errors = []

        def contend():
            try:
                turn_coordinator.acquire(tmp_path)
            except turn_coordinator.TurnBusy as exc:
                errors.append(exc)

        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=5)
        assert len(errors) == 1
    finally:
        first.release()
    with turn_coordinator.turn_slot(tmp_path):
        pass   # 释放后可再次获取


def test_cross_process_holder_blocks_and_releases(tmp_path):
    process = _run_python(_child_holder(tmp_path))
    try:
        _wait_line(process)
        with pytest.raises(turn_coordinator.TurnBusy):
            turn_coordinator.acquire(tmp_path)
    finally:
        process.stdin.close()
        process.wait(timeout=20)
    with turn_coordinator.turn_slot(tmp_path):
        assert turn_coordinator.held()


def test_crashed_process_releases_os_lock(tmp_path):
    process = _run_python(_child_holder(tmp_path, crash=True))
    _wait_line(process)
    assert process.wait(timeout=20) == 7
    with turn_coordinator.turn_slot(tmp_path):
        pass   # 锁文件仍在，但 OS 已释放


def test_stale_lock_file_is_not_ownership(tmp_path):
    lock = turn_coordinator.lock_path(tmp_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text('stale', encoding='utf-8')
    with turn_coordinator.turn_slot(tmp_path):
        assert lock.exists()


def test_runtime_send_rejects_busy_turn_without_writing_events(tmp_path):
    runtime = agent_runtime.AgentRuntime(
        run_turn=lambda messages, **kwargs: 'should not run',
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path / 'sessions')
    session_id = runtime.create_session()
    path = runtime.sessions_dir / f'session_{session_id}.jsonl'
    before = SessionEventStore.open(path).read_events()

    holder = turn_coordinator.acquire(tmp_path)
    try:
        result = []
        thread = threading.Thread(
            target=lambda: result.append(_send_expect_busy(runtime, session_id)))
        thread.start()
        thread.join(timeout=10)
        assert result and isinstance(result[0], turn_coordinator.TurnBusy)
    finally:
        holder.release()

    assert len(SessionEventStore.open(path).read_events()) == len(before),         '被拒绝的 send 不能写会话事件'
    assert runtime.send(session_id, 'now free').content == 'should not run'


def _send_expect_busy(runtime, session_id):
    try:
        runtime.send(session_id, 'blocked')
    except turn_coordinator.TurnBusy as exc:
        return exc
    raise AssertionError('send should have been rejected')


def test_cron_turn_keeps_job_queued_while_turn_is_busy(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')
    calls = []
    monkeypatch.setattr(harness, 'agent_loop', lambda *a, **k: calls.append(a))
    job = cron_scheduler.CronJob(id='cron_test', cron='* * * * *',
                                 prompt='do work', recurring=True, durable=False)
    with cron_scheduler.cron_lock:
        cron_scheduler.cron_queue.append(job)
    try:
        with turn_coordinator.turn_slot(tmp_path):
            worker = threading.Thread(target=harness.run_agent_turn_locked)
            worker.start()
            worker.join(timeout=10)
        assert calls == []
        with cron_scheduler.cron_lock:
            assert [item.id for item in cron_scheduler.cron_queue] == ['cron_test']

        harness.run_agent_turn_locked()
        assert len(calls) == 1
    finally:
        with cron_scheduler.cron_lock:
            cron_scheduler.cron_queue.clear()


def test_scheduler_service_starts_once_and_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    calls = []

    def run_turn():
        calls.append(time.monotonic())

    service = SchedulerService(run_turn, load_jobs=False, queue_poll=0.02)
    try:
        assert service.start() is True
        assert service.start() is False          # 幂等，不起第二组线程
        assert service.running
        assert len(service.threads) == 2
        assert service.stop(timeout=3) is True
        assert not service.running
        assert all(not thread.is_alive() for thread in service.threads)
        assert service.start() is True           # 停止后可重启
    finally:
        service.stop(timeout=3)


def test_scheduler_service_loads_durable_jobs_once(monkeypatch, tmp_path):
    path = tmp_path / 'crons.json'
    path.write_text(json.dumps([]), encoding='utf-8')
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', path)
    loads = []
    original = cron_scheduler.load_durable_jobs

    def counting_load():
        loads.append(threading.get_ident())
        return original()

    monkeypatch.setattr(cron_scheduler, 'load_durable_jobs', counting_load)
    service = SchedulerService(lambda: None, queue_poll=0.05)
    try:
        service.start()
        service.stop(timeout=3)
        service.start()
        assert len(loads) == 2   # 每次 start 一组，stop 后重启才再加载
    finally:
        service.stop(timeout=3)


def test_runtime_metadata_rejects_while_session_lock_is_held(tmp_path):
    from session_locks import SessionBusy, lock_path

    runtime = agent_runtime.AgentRuntime(
        run_turn=lambda messages, **kwargs: 'ok',
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path / 'sessions')
    session_id = runtime.create_session()

    from file_lock import exclusive_file_lock

    lock = lock_path(runtime.sessions_dir, session_id)
    with exclusive_file_lock(lock, timeout=0):
        for call in (
                lambda: runtime.set_workspace(session_id, str(tmp_path)),
                lambda: runtime.rename_session(session_id, '新标题'),
                lambda: runtime.fork_session(session_id),
                lambda: runtime.manage_session('archive', {'id': session_id})):
            with pytest.raises(SessionBusy):
                call()


def test_cleanup_reports_busy_session_instead_of_moving_it(tmp_path):
    from file_lock import exclusive_file_lock
    from session_admin import apply_session_cleanup
    from session_locks import lock_path

    moved = []

    class Archive:
        def delete(self, row, current_id):
            moved.append(('delete', row['id']))

        def restore(self, session_id):
            moved.append(('restore', session_id))

        def archive(self, row, current_id):
            moved.append(('archive', row['id']))

    sessions_dir = tmp_path / 'sessions'
    sessions_dir.mkdir()
    rows = [{'id': 'busy-session'}]
    lock = lock_path(sessions_dir, 'busy-session')
    with exclusive_file_lock(lock, timeout=0):
        completed, failures = apply_session_cleanup(
            Archive(), rows, 'archive', current_id='other')
    assert completed == 0
    assert failures and '执行任务' in failures[0][1]
    assert moved == []
