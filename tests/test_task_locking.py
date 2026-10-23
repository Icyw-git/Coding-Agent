"""Task claim must stay atomic when several processes race for the same task."""
import time
from multiprocessing import get_context
from pathlib import Path

import task_system


def _claim_after_barrier(task_dir, task_id, start, results):
    import task_system as ts

    ts.TASK_DIR = Path(task_dir)
    ts.TASKS_DIR = ts.TASK_DIR
    original_load = ts.load_task

    def slow_load(task_id):
        # Widen the read-modify-write window; without a cross-process lock both
        # workers would read 'pending' before either saves.
        task = original_load(task_id)
        time.sleep(0.05)
        return task

    ts.load_task = slow_load
    start.wait(10)
    results.put(ts.claim_task(task_id, owner='worker'))


def test_claim_is_atomic_across_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(task_system, 'TASK_DIR', tmp_path)
    monkeypatch.setattr(task_system, 'TASKS_DIR', tmp_path)
    task = task_system.create_task('cross-process claim')

    context = get_context('spawn')
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_claim_after_barrier,
                        args=(str(tmp_path), task.id, start, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()

    outcomes = [results.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert not process.is_alive(), 'claim worker did not finish'

    assert sum('Claimed' in outcome for outcome in outcomes) == 1, outcomes
    assert task_system.load_task(task.id).status == 'in_progress'
    assert task_system.load_task(task.id).owner == 'worker'
