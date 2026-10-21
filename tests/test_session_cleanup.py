import pytest

from session_cleanup import SessionArchive, cleanup_candidates
from session_store import SessionEventStore


def make_store(tmp_path, name='old'):
    store = SessionEventStore(tmp_path / 'sessions', session_id=name)
    store.append('message', ts=100, message={'role': 'user', 'content': '旧任务'})
    return store


def test_candidates_exclude_current_and_use_activity_time():
    rows = [dict(id='current', updated=1, preview='任务'),
            dict(id='old', updated=2, preview='任务'),
            dict(id='empty', updated=9999999, preview='尚无任务')]
    assert [row['id'] for row in cleanup_candidates(rows, 'current', now=3000000)] == ['old']
    assert [row['id'] for row in cleanup_candidates(rows, 'current', empty_only=True)] == ['empty']


def test_archive_and_restore_preserve_log_checkpoints_and_project(tmp_path):
    store = make_store(tmp_path)
    original = store.path.read_bytes()
    checkpoints = tmp_path / 'checkpoints' / 'old'
    checkpoints.mkdir(parents=True)
    (checkpoints / 'messages.jsonl').write_text('snapshot')
    project = tmp_path / 'project.py'
    project.write_text('untouched')
    archive = SessionArchive(store.directory, checkpoints.parent)
    archive.archive(store.session_summary(), 'current')
    assert not store.path.exists() and not checkpoints.exists()
    assert archive.list()[0]['id'] == 'old'
    archive.restore('old')
    assert store.path.read_bytes() == original
    assert (checkpoints / 'messages.jsonl').read_text() == 'snapshot'
    assert project.read_text() == 'untouched'
    assert archive.list() == []


def test_reject_current_changed_and_invalid_targets(tmp_path):
    store = make_store(tmp_path)
    archive = SessionArchive(store.directory, tmp_path / 'checkpoints')
    row = store.session_summary()
    with pytest.raises(ValueError, match='当前'):
        archive.archive(row, 'old')
    store.append_session_title('新主题')
    with pytest.raises(ValueError, match='已更新'):
        archive.archive(row, 'current')
    with pytest.raises(ValueError):
        archive.restore('../escape')
    assert store.path.exists()


def test_restore_refuses_overwrite(tmp_path):
    store = make_store(tmp_path)
    archive = SessionArchive(store.directory, tmp_path / 'checkpoints')
    archive.archive(store.session_summary(), 'current')
    make_store(tmp_path)
    with pytest.raises(ValueError, match='不覆盖'):
        archive.restore('old')
    assert archive.list()


def test_archive_rolls_back_checkpoints_if_log_move_fails(tmp_path, monkeypatch):
    from pathlib import Path

    store = make_store(tmp_path)
    checkpoints = tmp_path / 'checkpoints' / 'old'
    checkpoints.mkdir(parents=True)
    (checkpoints / 'snapshot').write_text('preserved')
    archive = SessionArchive(store.directory, checkpoints.parent)
    rename = Path.rename

    def fail_log_move(path, target):
        if path == store.path.resolve():
            raise OSError('test move failure')
        return rename(path, target)

    monkeypatch.setattr(Path, 'rename', fail_log_move)
    with pytest.raises(OSError, match='test move failure'):
        archive.archive(store.session_summary(), 'current')
    assert store.path.exists()
    assert (checkpoints / 'snapshot').read_text() == 'preserved'
    assert archive.list() == []


def test_cli_cleanup_confirmation_and_restore(tmp_path, monkeypatch, capsys):
    import cli_input
    import harness

    store = make_store(tmp_path)
    current = make_store(tmp_path, 'current')
    monkeypatch.setattr(harness, 'SESSIONS_DIR', store.directory)
    monkeypatch.setattr(harness, 'CHECKPOINT_DIR', tmp_path / 'checkpoints')
    monkeypatch.setattr(harness, 'wait_for_housekeeping', lambda: None)
    monkeypatch.setattr(harness, '_housekeeping_threads', [])
    monkeypatch.setattr(cli_input, 'choose_cleanup_sessions', lambda rows, **kw: [r['id'] for r in rows])
    monkeypatch.setattr(harness, '_confirm', lambda _: False)
    harness.handle_command('/archive', [], current)
    assert store.path.exists()
    monkeypatch.setattr(harness, '_confirm', lambda _: True)
    harness.handle_command('/archive', [], current)
    assert not store.path.exists() and current.path.exists()
    harness.handle_command('/archive restore', [], current)
    assert store.path.exists()
    assert '已恢复 1 个会话' in capsys.readouterr().out


@pytest.mark.parametrize('argument', ['0', '-1', 'abc', '30 extra', '999999'])
def test_cleanup_invalid_arguments(argument):
    from cli_commands import command_error
    assert command_error('/archive', argument)
