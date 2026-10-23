from pathlib import Path

from checkpoints import CheckpointManager


def manager(monkeypatch, output):
    mgr = object.__new__(CheckpointManager)
    mgr.ignore = ('.cache', '.git')
    monkeypatch.setattr(mgr, '_git', lambda *args: output)
    return mgr


def test_rename_baseline_matches_v1_and_does_not_report_drift(monkeypatch):
    raw = ('# branch.oid abc123\n'
           '2 R. N... 100644 100644 100644 hash hash R100 '
           'new name.txt\told name.txt\n')
    mgr = manager(monkeypatch, raw)
    baseline = mgr._git_baseline()
    live = 'R  "old name.txt" -> "new name.txt"\n'
    assert baseline == {'head': 'abc123', 'status': [live.rstrip()]}
    monkeypatch.setattr(mgr, '_git', lambda *args: live)
    assert mgr._drift_files({'git': baseline}, touched=set()) == []


def test_clean_baseline_reports_new_external_paths(monkeypatch):
    mgr = manager(monkeypatch, '?? external.txt\n?? .cache/\n')
    assert mgr._drift_files({'git': {'status': []}}, touched=set()) == ['external.txt']
    assert mgr._drift_files({'git': {'status': []}}, touched={'external.txt'}) == []


def test_new_rename_reports_destination(monkeypatch):
    mgr = manager(monkeypatch, 'R  "old name.txt" -> "new name.txt"\n')
    assert mgr._drift_files({'git': {'status': []}}, touched=set()) == ['new name.txt']
    assert mgr._drift_files({'git': {'status': []}}, touched={'new name.txt'}) == []


def test_unborn_head_is_empty(monkeypatch):
    mgr = manager(monkeypatch, '# branch.oid (initial)\n# branch.head main\n')
    assert mgr._git_baseline() == {'head': '', 'status': []}


def test_baseline_keys_split_quoted_rename_paths_only():
    quoted = CheckpointManager._status_keys([
        'R  "old name.txt" -> "new name.txt"',
    ])
    arrow_in_name = CheckpointManager._status_keys([
        'R  "old -> name.txt" -> "new -> name.txt"',
    ])
    assert quoted == {'old name.txt', 'new name.txt'}
    assert arrow_in_name == {'old -> name.txt', 'new -> name.txt'}


def test_git_preserves_leading_status_space(monkeypatch):
    from types import SimpleNamespace

    mgr = object.__new__(CheckpointManager)
    mgr.workspace = Path.cwd()
    monkeypatch.setattr('checkpoints.subprocess.run', lambda *args, **kwargs:
                        SimpleNamespace(stdout=' M tracked.txt\n'))
    assert mgr._git('status', '--porcelain') == ' M tracked.txt'
