"""tool_cache：缓存统计、会话引用保护与 /cache gc 的预览/执行。"""
import json
import os
import time
from pathlib import Path

import tool_cache


def _write(path: Path, data: bytes = b'x', age_days: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if age_days:
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
    return path


def _session(sessions: Path, payload: dict) -> Path:
    path = sessions / 's1.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + '\n',
                    encoding='utf-8')
    return path


def test_scan_counts_files_bytes_and_images(tmp_path):
    root = tmp_path / 'tool_results'
    _write(root / 'call-1.txt', b'1234')
    _write(root / 'images' / 'a.png', b'12')

    stats = tool_cache.scan(root)

    assert stats['files'] == 2
    assert stats['images'] == 1
    assert stats['bytes'] == 6
    assert stats['age_days'] == 0


def test_scan_missing_directory_is_empty(tmp_path):
    stats = tool_cache.scan(tmp_path / 'missing')

    assert stats['files'] == 0 and stats['bytes'] == 0
    assert stats['age_days'] is None


def test_cleanup_keeps_referenced_and_recent_files(tmp_path):
    root = tmp_path / 'tool_results'
    sessions = tmp_path / 'sessions'
    old = 10
    _write(root / 'old-orphan.txt', b'x' * 10, age_days=old)
    _write(root / 'old-used.txt', b'x' * 20, age_days=old)
    _write(root / 'images' / 'old-image.png', b'x' * 30, age_days=old)
    _write(root / 'recent-orphan.txt', b'x' * 40)
    _session(sessions, {
        'content': (f'Full output: {root / "old-used.txt"}'),
        'parts': [{'type': 'harness_image',
                   'path': str(root / 'images' / 'old-image.png')}],
    })

    plan = tool_cache.plan_cleanup(root, sessions, days=7)

    assert {path.name for path, _ in plan['remove']} == {'old-orphan.txt'}
    assert plan['bytes'] == 10
    assert plan['kept_recent'] == 1
    assert plan['kept_referenced'] == 2


def test_cleanup_keeps_files_referenced_by_windows_path_json(tmp_path):
    root = tmp_path / 'tool_results'
    sessions = tmp_path / 'sessions'
    target = _write(root / 'call-9.txt', b'x', age_days=30)
    # JSON 会把反斜杠写成两个；引用匹配必须能穿过转义。
    _session(sessions, {'content': f'Full output: {target}'})

    plan = tool_cache.plan_cleanup(root, sessions, days=7)

    assert plan['remove'] == []
    assert plan['kept_referenced'] == 1


def test_handler_defaults_to_status_and_rejects_bad_days(tmp_path):
    root = tmp_path / 'tool_results'
    sessions = tmp_path / 'sessions'
    _write(root / 'call-1.txt', b'12')

    status = tool_cache.handle_cache_command(root, sessions, '')
    assert '1 个文件' in status and '2 B' in status

    assert '用法' in tool_cache.handle_cache_command(root, sessions, 'gc 0')
    assert '用法' in tool_cache.handle_cache_command(root, sessions, 'gc x')


def test_handler_previews_before_deleting(tmp_path):
    root = tmp_path / 'tool_results'
    sessions = tmp_path / 'sessions'
    orphan = _write(root / 'old-orphan.txt', b'x' * 10, age_days=10)

    preview = tool_cache.handle_cache_command(root, sessions, 'gc 7')

    assert orphan.exists()
    assert 'old-orphan.txt' in preview and '--apply' in preview

    result = tool_cache.handle_cache_command(root, sessions, 'gc 7 --apply')

    assert not orphan.exists()
    assert '已清理 1 个文件' in result


def test_handler_reports_nothing_to_clean(tmp_path):
    root = tmp_path / 'tool_results'
    sessions = tmp_path / 'sessions'
    _write(root / 'recent.txt', b'x')

    output = tool_cache.handle_cache_command(root, sessions, 'gc 7')

    assert '没有可清理的文件' in output


def test_human_size_units():
    assert tool_cache.human_size(999) == '999 B'
    assert tool_cache.human_size(2048) == '2.0 KB'
    assert tool_cache.human_size(3 * 1024 * 1024) == '3.0 MB'
