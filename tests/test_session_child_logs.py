"""父会话归档/恢复/删除时，子代理与 teammate 日志必须一起移动。"""
import os
import time
from pathlib import Path

import pytest

import session_cleanup
from session_cleanup import SessionArchive, orphan_child_logs, purge_orphan_child_logs
from session_store import SessionEventStore


def _parent_with_children(tmp_path):
    store = SessionEventStore(tmp_path / 'sessions', session_id='old')
    store.append_message({'role': 'user', 'content': '旧任务'})
    child = SessionEventStore(store.directory / 'subagents', session_id='subagent_abc')
    child.append('subagent_started', description='inspect')
    child.append_message({'role': 'user', 'content': 'child'})
    child.append('subagent_finished', status='completed')
    store.append('subagent_started', subagent_id='abc', description='inspect',
                 log='subagents/session_subagent_abc.jsonl')
    mate = SessionEventStore(store.directory / 'teammates',
                             session_id='teammate_worker_abc')
    mate.append('teammate_started', name='worker', role='tester', description='work')
    mate.append('teammate_finished', status='completed')
    store.append('teammate_started', teammate='worker', run_id='abc', role='tester',
                 log='teammates/session_teammate_worker_abc.jsonl')
    return store, child, mate


def test_archive_and_restore_move_child_logs(tmp_path):
    store, child, mate = _parent_with_children(tmp_path)
    original = store.path.read_bytes()
    child_bytes = child.path.read_bytes()
    archive = SessionArchive(store.directory, tmp_path / 'checkpoints')

    archive.archive(store.session_summary(), 'current')

    assert not store.path.exists() and not child.path.exists() and not mate.path.exists()
    target = archive.root / 'old'
    assert (target / 'subagents' / child.path.name).read_bytes() == child_bytes
    assert (target / 'teammates' / mate.path.name).is_file()
    assert archive.list()[0]['id'] == 'old'

    archive.restore('old')

    assert store.path.read_bytes() == original
    assert child.path.read_bytes() == child_bytes
    assert SessionEventStore.open(child.path).read_events()[-1]['type'] == 'subagent_finished'
    assert SessionEventStore.open(mate.path).read_events()[-1]['type'] == 'teammate_finished'
    assert archive.list() == []


def test_delete_removes_child_logs_with_archive(tmp_path):
    store, child, mate = _parent_with_children(tmp_path)
    archive = SessionArchive(store.directory, tmp_path / 'checkpoints')
    row = store.session_summary()
    archive.archive(row, 'current')

    archive.delete(row, 'current')

    assert not (archive.root / 'old').exists()
    assert not child.path.exists() and not mate.path.exists()


def test_restore_refuses_live_child_log_collision(tmp_path):
    store, child, _ = _parent_with_children(tmp_path)
    archive = SessionArchive(store.directory, tmp_path / 'checkpoints')
    archive.archive(store.session_summary(), 'current')
    live = SessionEventStore(child.directory, session_id='subagent_abc')
    live.append('subagent_started', description='new')

    with pytest.raises(ValueError, match='不覆盖'):
        archive.restore('old')

    assert not store.path.exists()
    assert (archive.root / 'old' / 'subagents' / child.path.name).is_file()


def test_archive_rejects_child_log_outside_store(tmp_path):
    store, _, _ = _parent_with_children(tmp_path)
    victim = tmp_path / 'victim.jsonl'
    victim.write_text('keep')
    store.append('subagent_started', subagent_id='bad', log='../victim.jsonl')
    archive = SessionArchive(store.directory, tmp_path / 'checkpoints')

    with pytest.raises(ValueError, match='路径无效'):
        archive.archive(store.session_summary(), 'current')

    assert store.path.exists() and victim.read_text() == 'keep'
    assert not (archive.root / 'old').exists()


def test_delete_rejects_unknown_file_inside_child_dir(tmp_path):
    store, _, _ = _parent_with_children(tmp_path)
    archive = SessionArchive(store.directory, tmp_path / 'checkpoints')
    row = store.session_summary()
    archive.archive(row, 'current')
    extra = archive.root / 'old' / 'subagents' / 'unknown.txt'
    extra.write_text('must not delete')

    with pytest.raises(ValueError, match='未知文件'):
        archive.delete(row, 'current')

    assert extra.read_text() == 'must not delete'


def test_orphan_child_logs_reclaims_only_stale_unreferenced(tmp_path):
    store, child, mate = _parent_with_children(tmp_path)
    stale = SessionEventStore(store.directory / 'teammates',
                              session_id='teammate_lost_old')
    stale.append('teammate_started', name='lost', role='r', description='d')
    fresh = SessionEventStore(store.directory / 'subagents',
                              session_id='subagent_fresh')
    fresh.append('subagent_started', description='d')
    old = time.time() - 40 * 86400
    os.utime(stale.path, (old, old))
    os.utime(mate.path, (old, old))   # 被父会话引用，再旧也不回收

    orphans = orphan_child_logs(store.directory, days=30)

    assert [path.name for path in orphans] == ['session_teammate_lost_old.jsonl']

    deleted, failures = purge_orphan_child_logs(store.directory, days=30)

    assert failures == 0 and len(deleted) == 1
    assert not stale.path.exists()
    assert child.path.exists() and mate.path.exists() and fresh.path.exists()


def test_orphan_scan_skips_linked_child_directory(tmp_path, monkeypatch):
    sessions = tmp_path / 'sessions'
    target = tmp_path / 'outside'
    sessions.mkdir()
    target.mkdir()
    victim = SessionEventStore(target, session_id='teammate_victim')
    victim.append('teammate_started', name='victim')
    old = time.time() - 40 * 86400
    os.utime(victim.path, (old, old))
    linked_directory = sessions / 'teammates'
    linked_directory.mkdir()
    victim_copy = linked_directory / victim.path.name
    victim_copy.write_text('keep')
    monkeypatch.setattr(session_cleanup, '_is_link',
                        lambda path: Path(path) == linked_directory)

    assert orphan_child_logs(sessions, days=30) == []
    assert victim_copy.exists()


def test_orphan_scan_skips_linked_child_file(tmp_path, monkeypatch):
    sessions = tmp_path / 'sessions'
    directory = sessions / 'subagents'
    directory.mkdir(parents=True)
    child = SessionEventStore(directory, session_id='subagent_linked')
    child.append('subagent_started', description='linked')
    old = time.time() - 40 * 86400
    os.utime(child.path, (old, old))
    monkeypatch.setattr(session_cleanup, '_is_link',
                        lambda path: Path(path) == child.path)

    assert orphan_child_logs(sessions, days=30) == []
    assert child.path.exists()


def test_orphan_scan_aborts_when_parent_log_unreadable(tmp_path, monkeypatch):
    store, child, _ = _parent_with_children(tmp_path)

    class BrokenStore:
        @staticmethod
        def open(*args, **kwargs):
            raise OSError('locked')

    monkeypatch.setattr(session_cleanup, 'SessionEventStore', BrokenStore)

    assert orphan_child_logs(store.directory) == []
    assert child.path.exists()


def test_archive_skips_child_log_deleted_by_hand(tmp_path):
    store, child, mate = _parent_with_children(tmp_path)
    child.path.unlink()
    archive = SessionArchive(store.directory, tmp_path / 'checkpoints')

    archive.archive(store.session_summary(), 'current')

    target = archive.root / 'old'
    assert (target / 'teammates' / mate.path.name).is_file()
    assert not (target / 'subagents').exists()
    assert not store.path.exists()


def test_archive_rolls_back_child_logs_if_parent_move_fails(tmp_path, monkeypatch):
    store, child, mate = _parent_with_children(tmp_path)
    archive = SessionArchive(store.directory, tmp_path / 'checkpoints')
    rename = Path.rename

    def fail_log_move(path, target):
        if path == store.path.resolve():
            raise OSError('test move failure')
        return rename(path, target)

    monkeypatch.setattr(Path, 'rename', fail_log_move)
    with pytest.raises(OSError, match='test move failure'):
        archive.archive(store.session_summary(), 'current')

    assert store.path.exists()
    assert child.path.exists() and mate.path.exists()
    assert not (archive.root / 'old').exists()
