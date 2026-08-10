"""Unit tests for the task system (task_system.py).

Run with:
    python -m pytest test_task_system.py -v
"""

import pytest

import task_system


@pytest.fixture
def isolated_tasks(tmp_path, monkeypatch):
    """Point the task system at a temporary directory for test isolation."""
    monkeypatch.setattr(task_system, "TASK_DIR", tmp_path)
    monkeypatch.setattr(task_system, "TASKS_DIR", tmp_path)
    return tmp_path


def test_create_task(isolated_tasks):
    task = task_system.create_task("Write docs", "Create a README")
    assert task.id.startswith("task_")
    assert task.subject == "Write docs"
    assert task.description == "Create a README"
    assert task.status == "pending"
    assert task.owner is None
    assert task.blockedBy == []
    # The task is persisted to disk immediately.
    assert task_system._task_path(task.id).exists()
    loaded = task_system.load_task(task.id)
    assert loaded.subject == task.subject
    assert loaded.description == task.description


def test_create_task_with_dependencies(isolated_tasks):
    dep = task_system.create_task("Dependency")
    task = task_system.create_task("Blocked task", blockedBy=[dep.id])
    assert task.blockedBy == [dep.id]
    assert task_system.can_start(task.id) is False


def test_list_tasks_returns_all_persisted_tasks(isolated_tasks):
    first = task_system.create_task("First")
    second = task_system.create_task("Second")
    tasks = task_system.list_tasks()
    assert len(tasks) == 2
    assert {t.id for t in tasks} == {first.id, second.id}
    assert {t.subject for t in tasks} == {"First", "Second"}


def test_list_tasks_empty_when_directory_missing(tmp_path, monkeypatch):
    missing = tmp_path / "does_not_exist"
    monkeypatch.setattr(task_system, "TASKS_DIR", missing)
    assert task_system.list_tasks() == []


def test_claim_pending_task(isolated_tasks):
    task = task_system.create_task("Claim me")
    result = task_system.claim_task(task.id, owner="alice")
    assert "Claimed" in result
    loaded = task_system.load_task(task.id)
    assert loaded.status == "in_progress"
    assert loaded.owner == "alice"


def test_claim_non_pending_task_is_rejected(isolated_tasks):
    task = task_system.create_task("Already in progress")
    task_system.claim_task(task.id, owner="alice")
    result = task_system.claim_task(task.id, owner="bob")
    assert "cannot claim" in result
    loaded = task_system.load_task(task.id)
    assert loaded.status == "in_progress"
    assert loaded.owner == "alice"  # owner is not overwritten


def test_claim_completed_task_is_rejected(isolated_tasks):
    task = task_system.create_task("Done")
    task_system.complete_task(task.id)
    result = task_system.claim_task(task.id, owner="alice")
    assert "cannot claim" in result
    assert task_system.load_task(task.id).status == "completed"


def test_claim_blocked_task_is_rejected(isolated_tasks):
    dep = task_system.create_task("Dependency")
    blocked = task_system.create_task("Blocked", blockedBy=[dep.id])
    result = task_system.claim_task(blocked.id, owner="alice")
    assert result.startswith("Blocked by:")
    loaded = task_system.load_task(blocked.id)
    assert loaded.status == "pending"
    assert loaded.owner is None


def test_claim_blocked_task_after_dependency_completed(isolated_tasks):
    dep = task_system.create_task("Dependency")
    blocked = task_system.create_task("Blocked", blockedBy=[dep.id])
    task_system.complete_task(dep.id)
    result = task_system.claim_task(blocked.id, owner="alice")
    assert "Claimed" in result
    assert task_system.load_task(blocked.id).status == "in_progress"


def test_complete_task(isolated_tasks):
    task = task_system.create_task("Finish")
    result = task_system.complete_task(task.id)
    assert "Completed" in result
    loaded = task_system.load_task(task.id)
    assert loaded.status == "completed"


def test_complete_task_unblocks_dependents(isolated_tasks):
    dep = task_system.create_task("Dependency")
    blocked = task_system.create_task("Blocked", blockedBy=[dep.id])
    result = task_system.complete_task(dep.id)
    assert "Completed" in result
    assert "Unblocked" in result
    assert "Blocked" in result
    # The formerly blocked task can now be claimed.
    claim_result = task_system.claim_task(blocked.id, owner="alice")
    assert "Claimed" in claim_result


def test_get_task_returns_json(isolated_tasks):
    task = task_system.create_task("JSON task", "serialized")
    payload = task_system.get_task(task.id)
    assert task.id in payload
    assert '"status": "pending"' in payload
    assert '"subject": "JSON task"' in payload
