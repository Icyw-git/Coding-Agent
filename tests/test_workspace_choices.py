"""新建会话的工作区候选与绑定：用户只按键选择，不输入长路径。"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import harness
from workspace_choices import browse_entries, workspace_choices, workspace_label


def _rows(*entries):
    return [{'workspace': path, 'updated': updated, 'title': '', 'id': str(index)}
            for index, (path, updated) in enumerate(entries)]


def test_choices_start_with_current_and_skip_unrecorded_workspaces(tmp_path):
    other = tmp_path / 'other'
    choices = workspace_choices(_rows(('', 99), (str(other), 50)), tmp_path)
    assert choices[0]['current'] is True
    assert Path(choices[0]['path']) == tmp_path
    assert [choice['path'] for choice in choices[1:]] == [str(other)]
    assert '当前目录' in workspace_label(choices[0])


def test_choices_rank_recent_directories_and_apply_the_limit(tmp_path):
    paths = [tmp_path / f'p{index}' for index in range(4)]
    rows = _rows(*((str(path), index) for index, path in enumerate(paths)))
    choices = workspace_choices(rows, str(tmp_path / 'now'), limit=2)
    assert [choice['path'] for choice in choices[1:]] == [str(paths[3]), str(paths[2])]


def test_choices_count_sessions_and_dedupe_by_normalized_path(tmp_path):
    project = tmp_path / 'proj'
    rows = _rows((str(project), 1), (os.path.normcase(str(project)), 2), ('', 3))
    choices = workspace_choices(rows, str(tmp_path))
    assert len(choices) == 2
    assert choices[1]['sessions'] == 2
    assert choices[1]['last_active'] == 2
    assert choices[1]['current'] is False


def test_current_directory_session_count_is_kept(tmp_path):
    rows = _rows((str(tmp_path), 1), (str(tmp_path), 2))
    choices = workspace_choices(rows, str(tmp_path))
    assert choices[0]['sessions'] == 2
    assert choices[0]['last_active'] == 2


def test_start_new_session_binds_the_chosen_workspace(tmp_path, monkeypatch):
    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    project = tmp_path / 'project'
    project.mkdir()
    monkeypatch.setattr(harness, 'SESSIONS_DIR', sessions)
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)

    store = harness.start_new_session(workspace=str(project), session_id='bound1')

    assert harness.WORKDIR == project.resolve()
    assert Path(store.session_summary()['workspace']) == project.resolve()
    assert (sessions / 'session_bound1.jsonl').is_file()


def test_start_new_session_rejects_a_missing_workspace_without_writing(tmp_path, monkeypatch):
    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    monkeypatch.setattr(harness, 'SESSIONS_DIR', sessions)
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)

    with pytest.raises(ValueError):
        harness.start_new_session(workspace=str(tmp_path / 'missing'), session_id='bad1')
    assert not (sessions / 'session_bad1.jsonl').exists()


def test_start_new_session_refuses_an_existing_id(tmp_path, monkeypatch):
    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    monkeypatch.setattr(harness, 'SESSIONS_DIR', sessions)
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    harness.start_new_session(workspace=str(tmp_path), session_id='dup1')

    with pytest.raises(FileExistsError):
        harness.start_new_session(workspace=str(tmp_path), session_id='dup1')


def test_start_new_session_restores_workspace_when_the_store_fails(
        tmp_path, monkeypatch):
    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    project = tmp_path / 'project'
    project.mkdir()
    monkeypatch.setattr(harness, 'SESSIONS_DIR', sessions)
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)

    def broken(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(harness, 'SessionEventStore', broken)
    with pytest.raises(OSError):
        harness.start_new_session(workspace=str(project), session_id='fail1')

    assert harness.WORKDIR == tmp_path
    assert not (sessions / 'session_fail1.jsonl').exists()


def test_browse_entries_navigate_folders_without_typing_paths(tmp_path):
    (tmp_path / 'alpha').mkdir()
    (tmp_path / 'beta').mkdir()
    (tmp_path / 'note.txt').write_text('x', encoding='utf-8')

    rows = browse_entries(tmp_path, drives=[])

    # 子目录紧跟在置顶的「进入此文件夹」后面：Enter 进入后光标回到置顶行，
    # 再按 Enter 即确认；想继续浏览就按 ↓ 到子目录。
    assert [row['kind'] for row in rows] == ['dir', 'dir', 'parent']
    assert [row['name'] for row in rows[:2]] == ['alpha', 'beta']
    assert rows[-1] == {'kind': 'parent', 'name': '上级目录',
                        'path': str(tmp_path.parent.resolve())}
    assert 'note.txt' not in [row['name'] for row in rows]


def test_browse_entries_offer_drives_after_the_folders(tmp_path):
    roots = [Path('C:/'), Path('D:/')]
    rows = browse_entries(tmp_path, drives=roots)
    assert [row['kind'] for row in rows] == ['parent', 'drive', 'drive']
    assert [row['path'] for row in rows[-2:]] == [str(root) for root in roots]


def test_non_interactive_new_session_keeps_the_current_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    monkeypatch.setattr(harness.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(harness.sys, 'stdout', SimpleNamespace(isatty=lambda: False))
    assert harness._workspace_for_new_session() == tmp_path
