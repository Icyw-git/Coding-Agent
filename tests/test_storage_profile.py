"""存储 profile：分类、引用保护、快照回收量估算与未知目录。"""
import time
from pathlib import Path

from checkpoints import CheckpointManager
from storage_profile import format_profile, scan


def _age(path: Path, days: int) -> None:
    stamp = time.time() - days * 86400
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x')
    import os
    os.utime(path, (stamp, stamp))


def test_profile_separates_source_derived_and_unmanaged(tmp_path):
    root = tmp_path / '.cache'
    sessions = root / 'sessions'
    sessions.mkdir(parents=True)
    used = root / 'tool_results' / 'used.txt'
    orphan = root / 'tool_results' / 'old.txt'
    _age(used, 30)
    _age(orphan, 30)
    (sessions / 'session_s.jsonl').write_text(str(used), encoding='utf-8')
    _age(root / 'tmp' / 'old.tmp', 30)
    (root / 'memories').mkdir(parents=True)
    (root / 'memories' / 'a.md').write_text('memory', encoding='utf-8')
    (root / 'unknown').mkdir()
    (root / 'unknown' / 'x.bin').write_bytes(b'x' * 10)

    mgr = CheckpointManager(root / 'checkpoints', tmp_path, 's', max_nodes=1)
    nodes = [mgr.begin_turn([{'role': 'user', 'content': str(i)}])
             for i in range(3)]
    for _ in nodes:
        mgr.seal()

    profile = scan(root, sessions_dir=sessions, checkpoint_keep=1)

    assert profile['known_bytes'] > profile['unmanaged_bytes']
    assert profile['unmanaged'][0]['key'] == 'unknown'
    tool = next(e for e in profile['entries'] if e['key'] == 'tool_results')
    assert tool['reclaimable_files'] == 1          # used.txt 被会话引用
    assert tool['kept_referenced'] == 1
    snap = next(e for e in profile['entries'] if e['key'] == 'checkpoints')
    assert snap['reclaimable_files'] == 2
    text = format_profile(profile)
    assert '源数据' in text and '可回收派生缓存' in text and '未识别目录' in text
    assert '/checkpoint-gc' in text


def test_profile_shallow_does_not_scan_references(tmp_path):
    root = tmp_path / '.cache'
    (root / 'tool_results').mkdir(parents=True)
    (root / 'tool_results' / 'old.txt').write_bytes(b'x')
    profile = scan(root, deep=False)
    tool = next(e for e in profile['entries'] if e['key'] == 'tool_results')
    assert 'reclaimable' not in tool
    assert profile['reclaimable_bytes'] == 0
