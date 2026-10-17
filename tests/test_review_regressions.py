import json

import pytest

from checkpoints import CheckpointManager, CheckpointError
from session_store import SessionEventStore
from session_cleanup import SessionArchive


def test_checkpoint_refuses_other_workspace_and_mixed_chain(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    file = a / 'file.txt'
    file.write_text('original')
    (b / 'file.txt').write_text('changed')
    manager = CheckpointManager(tmp_path / 'checkpoints', a, 'demo')
    cp = manager.begin_turn([])
    manager.capture_pre_image(file)
    file.write_text('changed')
    manager.seal()
    other = CheckpointManager(tmp_path / 'checkpoints', b, 'demo')
    for force in (False, True):
        with pytest.raises(CheckpointError, match='工作区'):
            other.restore(cp, force=force)
    assert other.restore(cp, scope='session')['applied'] == []
    other.begin_turn([])
    with pytest.raises(CheckpointError):
        other.plan_restore(cp)
    assert (b / 'file.txt').read_text() == 'changed'


def test_legacy_checkpoint_allows_only_session_restore(tmp_path):
    manager = CheckpointManager(tmp_path / 'checkpoints', tmp_path, 'demo')
    cp = manager.begin_turn([])
    meta_path = manager.base_dir / cp / 'meta.json'
    meta = json.loads(meta_path.read_text())
    del meta['workspace']
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(CheckpointError):
        manager.restore(cp, force=True)
    assert manager.restore(cp, scope='session')['ok']


def test_corrupt_archive_does_not_hide_healthy_entries(tmp_path, capsys):
    archive = SessionArchive(tmp_path / 'sessions', tmp_path / 'checkpoints')
    for name in ('good', 'bad'):
        store = SessionEventStore(archive.sessions, session_id=name)
        store.append('session_created')
        archive.archive(store.session_summary(), 'current')
    (archive.root / 'bad' / 'session_bad.jsonl').write_text('broken\n{}\n')
    assert [row['id'] for row in archive.list()] == ['good']
    assert '已跳过' in capsys.readouterr().out


def test_catalog_reuses_and_invalidates_summaries(tmp_path, monkeypatch):
    import harness

    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    monkeypatch.setattr(harness, '_session_summary_cache', {})
    store = SessionEventStore(tmp_path, session_id='one')
    store.append_message({'role': 'user', 'content': 'task'})
    original = SessionEventStore.read_events
    reads = []

    def counted(self, **kwargs):
        reads.append(self.path)
        return original(self, **kwargs)

    monkeypatch.setattr(SessionEventStore, 'read_events', counted)
    assert harness.session_rows()[0]['title'] == 'task'
    initial = len(reads)
    assert initial == 1   # _read_last_seq 走尾部扫描，不再触发全量 read_events
    harness.session_rows()
    harness.list_session_paths()
    assert len(reads) == initial
    store.append_session_title('new topic')
    assert harness.session_rows()[0]['title'] == 'new topic'
    archive = SessionArchive(tmp_path, tmp_path / 'checkpoints')
    archive.archive(store.session_summary(), 'current')
    assert harness.session_rows() == []
    archive.restore('one')
    assert harness.session_rows()[0]['title'] == 'new topic'
