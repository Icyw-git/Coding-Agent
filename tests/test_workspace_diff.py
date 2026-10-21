"""逐行 diff 与回合改动清单：真实 git 仓库 + checkpoint 回退。"""
import json
import os
import shutil
import subprocess

import pytest

import harness
import test_context_manage as tcm
import workspace_diff
from session_store import SessionEventStore

pytestmark = pytest.mark.skipif(
    shutil.which('git') is None, reason='git is not installed')


def _git(repo, *args):
    return subprocess.run(['git', *args], cwd=str(repo), check=True,
                          capture_output=True, text=True, encoding='utf-8')


@pytest.fixture()
def repo(tmp_path):
    path = tmp_path / 'repo'
    path.mkdir()
    _git(path, 'init', '-q')
    _git(path, 'config', 'user.email', 'test@example.com')
    _git(path, 'config', 'user.name', 'Test')
    (path / 'app.py').write_text('old\n', encoding='utf-8')
    _git(path, 'add', '.')
    _git(path, 'commit', '-qm', 'init')
    return path


def test_split_porcelain_consumes_rename_origin():
    rows = workspace_diff._split_porcelain(
        'R  new.py\0old.py\0 M a.py\0?? b.txt\0')
    assert rows == [('R ', 'new.py', 'old.py'),
                    (' M', 'a.py', None),
                    ('??', 'b.txt', None)]


def test_format_diff_shows_unified_lines(repo):
    (repo / 'app.py').write_text('new\n', encoding='utf-8')
    output = workspace_diff.format_diff(repo)
    assert '文件状态' in output
    assert 'app.py' in output
    assert '逐行改动' in output
    assert '-old' in output and '+new' in output
    assert '@@' in output


def test_format_diff_includes_untracked_file(repo):
    (repo / 'notes.txt').write_text('fresh\n', encoding='utf-8')
    output = workspace_diff.format_diff(repo)
    assert '?? notes.txt' in output
    assert '+fresh' in output


def test_format_diff_handles_rename(repo):
    _git(repo, 'mv', 'app.py', 'renamed.py')
    output = workspace_diff.format_diff(repo)
    assert 'app.py → renamed.py' in output


def test_format_diff_clean_and_color(repo):
    assert '工作区干净' in workspace_diff.format_diff(repo)
    (repo / 'app.py').write_text('new\n', encoding='utf-8')
    colored = workspace_diff.format_diff(repo, color=True)
    assert '\033[32m+new' in colored


def test_format_diff_ignores_harness_cache(repo):
    (repo / '.cache' / 'sessions').mkdir(parents=True)
    (repo / '.cache' / 'sessions' / 'x.jsonl').write_text('{}', encoding='utf-8')
    assert '工作区干净' in workspace_diff.format_diff(repo)


def test_format_diff_non_git_workspace(tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    assert '不是 Git 仓库' in workspace_diff.format_diff(plain)


def test_format_diff_unborn_repo_staged_file(tmp_path):
    path = tmp_path / 'newrepo'
    path.mkdir()
    _git(path, 'init', '-q')
    (path / 'first.py').write_text('hello\n', encoding='utf-8')
    _git(path, 'add', 'first.py')
    output = workspace_diff.format_diff(path)
    assert 'A  first.py' in output
    assert '+hello' in output


def test_snapshot_and_changed_since(repo):
    baseline = workspace_diff.snapshot(repo)
    assert baseline == {}

    (repo / 'app.py').write_text('changed\n', encoding='utf-8')
    (repo / 'notes.txt').write_text('fresh\n', encoding='utf-8')
    changes = workspace_diff.changed_since(repo, baseline)
    assert {entry['path'] for entry in changes} == {'app.py', 'notes.txt'}

    # 未改动的脏文件不重复上报：以当前状态为基线，再只动一个新文件
    baseline = workspace_diff.snapshot(repo)
    (repo / 'other.txt').write_text('other\n', encoding='utf-8')
    changes = workspace_diff.changed_since(repo, baseline)
    assert [entry['path'] for entry in changes] == ['other.txt']


def test_changed_since_ignores_timestamp_only_touch(repo):
    (repo / 'app.py').write_text('changed\n', encoding='utf-8')
    baseline = workspace_diff.snapshot(repo)
    os.utime(repo / 'app.py', None)   # 只改 mtime，内容不变
    assert workspace_diff.changed_since(repo, baseline) == []


def test_turn_changes_falls_back_without_git(tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    changes = workspace_diff.turn_changes(
        plain, None, {'new.py': 'A', '.cache/x': 'M', 'old.py': 'D'})
    assert [(entry['code'], entry['path']) for entry in changes] == [
        ('A', 'new.py'), ('D', 'old.py')]
    summary = workspace_diff.format_changes(changes)
    assert '本轮修改 2 个文件' in summary and '/diff' in summary


def test_format_changes_limits_and_renames():
    changes = [{'code': 'M', 'path': f'f{i}.py', 'old': None} for i in range(5)]
    output = workspace_diff.format_changes(changes, max_files=3)
    assert '本轮修改 5 个文件' in output
    assert '另有 2 个文件' in output
    assert output.index('f0.py') < output.index('f2.py')
    renamed = workspace_diff.format_changes(
        [{'code': 'R', 'path': 'new.py', 'old': 'old.py'}])
    assert 'old.py → new.py' in renamed


def test_agent_loop_prints_changes_and_records_event(repo, tmp_path, monkeypatch,
                                                     capsys):
    monkeypatch.setattr(harness, 'get_workdir', lambda: repo)
    monkeypatch.setattr(harness, 'load_memories', lambda messages: '')
    monkeypatch.setattr(harness, 'extract_memories', lambda pre: None)
    monkeypatch.setattr(harness, 'consolidate_memories', lambda: None)
    monkeypatch.setattr(harness, 'generate_session_title', lambda messages: 't')
    monkeypatch.setattr(harness, '_post_turn_housekeeping', lambda *a, **k: None)

    store = SessionEventStore(tmp_path / 'sessions', session_id='turn')
    fake = tcm.FakeCompletions([
        tcm.make_tool_response([
            ('c1', 'write', json.dumps({'path': 'app.py', 'content': 'new\n'}))]),
        tcm.make_text_response('done'),
    ])
    monkeypatch.setattr(harness.client.chat.completions, 'create', fake.create)

    assert harness.agent_loop(
        [{'role': 'user', 'content': '改一下'}], session_store=store) == 'done'

    output = capsys.readouterr().out
    assert '[改动] 本轮修改 1 个文件' in output
    assert 'app.py' in output and '/diff' in output

    events = [event for event in store.read_events()
              if event.get('type') == 'files_changed']
    assert len(events) == 1
    assert events[0]['files'] == [
        {'code': 'M', 'path': 'app.py', 'old': None}]

