import pytest

from session_cleanup import SessionArchive
from session_store import SessionEventStore


def archived(tmp_path):
    store = SessionEventStore(tmp_path / 'sessions', session_id='old')
    store.append_message({'role': 'user', 'content': '旧任务'})
    checkpoints = tmp_path / 'checkpoints' / 'old'
    checkpoints.mkdir(parents=True)
    (checkpoints / 'snapshot').write_text('snapshot')
    archive = SessionArchive(store.directory, checkpoints.parent)
    row = store.session_summary()
    archive.archive(row, 'current')
    return archive, row


def test_delete_removes_only_selected_archive(tmp_path):
    archive, row = archived(tmp_path)
    project = tmp_path / 'project.py'
    project.write_text('keep')
    other = SessionEventStore(archive.sessions, session_id='other')
    other.append('session_created')
    archive.delete(row, 'current')
    assert archive.list() == []
    assert not (archive.root / row['id']).exists()
    assert other.path.exists() and project.read_text() == 'keep'
    with pytest.raises(ValueError, match='不存在'):
        archive.restore(row['id'])


def test_delete_rejects_current_changed_and_unexpected_files(tmp_path):
    archive, row = archived(tmp_path)
    with pytest.raises(ValueError, match='当前'):
        archive.delete(row, 'old')
    extra = archive.root / 'old' / 'unknown.txt'
    extra.write_text('must not delete')
    with pytest.raises(ValueError, match='未知文件'):
        archive.delete(row, 'current')
    assert extra.read_text() == 'must not delete'
    log = archive.root / 'old' / 'session_old.jsonl'
    SessionEventStore.open(log).append_session_title('changed')
    with pytest.raises(ValueError, match='已更新'):
        archive.delete(row, 'current')


def test_delete_refuses_live_session_collision(tmp_path):
    archive, row = archived(tmp_path)
    live = SessionEventStore(archive.sessions, session_id='old')
    live.append('session_created')
    with pytest.raises(ValueError, match='同名'):
        archive.delete(row, 'current')
    assert live.path.exists() and archive.list()


@pytest.mark.parametrize('answer', ['n', 'y', '', '删除'])
def test_cli_delete_requires_explicit_confirmation(tmp_path, monkeypatch, capsys, answer):
    import cli_input
    import harness

    archive, row = archived(tmp_path)
    current = SessionEventStore(archive.sessions, session_id='current')
    current.append('session_created')
    monkeypatch.setattr(harness, 'SESSIONS_DIR', archive.sessions)
    monkeypatch.setattr(harness, 'CHECKPOINT_DIR', archive.checkpoints)
    monkeypatch.setattr(harness, 'wait_for_housekeeping', lambda: None)
    monkeypatch.setattr(harness, '_housekeeping_threads', [])
    monkeypatch.setattr(cli_input, 'choose_cleanup_sessions', lambda rows, **kw: [r['id'] for r in rows])
    monkeypatch.setattr('builtins.input', lambda _: answer)
    harness.handle_command('/delete', [], current)
    assert bool(archive.list()) == (answer != '删除')
    assert current.path.exists()
    output = capsys.readouterr().out
    assert ('不可恢复' if answer == '删除' else '已取消') in output


def test_delete_rejects_arguments():
    from cli_commands import command_error
    assert command_error('/delete', 'all')
    assert command_error('/delete', '') is None
