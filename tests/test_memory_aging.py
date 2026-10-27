"""记忆老化与归档（N2）。

之前只有 50 条的数量上界，没有时间维度：用一年后 50 条全是早期闲聊，最近的
偏好挤不进去。Codex 用双时间淘汰（30 天未用淘汰、未召回清除）。

我们的三个不同：
1. 不静默删除——移到 .archive/，「不删除用户数据」是既有约束
2. preference/correction 不过期——偏好不会因为三个月没提就失效
3. 不写回记忆文件——召回记录存独立 json，否则会不停扰动 mtime（而 mtime 正是
   老化依据）
"""
from __future__ import annotations

import os
import time

import memory_aging


def _entry(memory_dir, name, mem_type, *, age_days=0, body='body'):
    """造一条记忆并把它（连同 mtime）设为指定年龄。"""
    import memory
    path = memory_dir / f'{name}.md'
    path.write_text(
        f'---\nname: {name}\ndescription: d\ntype: {mem_type}\n---\n\n{body}\n',
        encoding='utf-8')
    if age_days:
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
    return {'name': name, 'filename': path.name, 'type': mem_type,
            'description': 'd', 'body': body}


# ---------------------------------------------------------------- 挑选逻辑

def test_stale_picks_old_entries(tmp_path):
    entry = _entry(tmp_path, 'old', 'fact', age_days=200)
    stale = memory_aging.stale_entries(tmp_path, [entry])
    assert [item['name'] for item in stale] == ['old']
    assert stale[0]['age_days'] >= 200


def test_fresh_entries_are_kept(tmp_path):
    entry = _entry(tmp_path, 'new', 'fact', age_days=1)
    assert memory_aging.stale_entries(tmp_path, [entry]) == []


def test_preference_never_expires(tmp_path):
    """偏好不会因为三个月没提就失效。"""
    entry = _entry(tmp_path, 'pref', 'preference', age_days=500)
    assert memory_aging.stale_entries(tmp_path, [entry]) == []


def test_correction_never_expires(tmp_path):
    """教训要一直记得。"""
    entry = _entry(tmp_path, 'fix', 'correction', age_days=500)
    assert memory_aging.stale_entries(tmp_path, [entry]) == []


def test_context_and_fact_do_expire(tmp_path):
    for kind in ('fact', 'context'):
        entry = _entry(tmp_path, f'x-{kind}', kind, age_days=200)
        assert memory_aging.stale_entries(tmp_path, [entry]), kind


def test_recall_resets_the_clock(tmp_path):
    """「多久没用」看的是最后一次有用，不是最后一次写入。"""
    entry = _entry(tmp_path, 'old', 'fact', age_days=200)
    assert memory_aging.stale_entries(tmp_path, [entry])
    memory_aging.record_recall(tmp_path, ['old.md'])
    assert memory_aging.stale_entries(tmp_path, [entry]) == []


def test_threshold_is_configurable(tmp_path, monkeypatch):
    entry = _entry(tmp_path, 'x', 'fact', age_days=40)
    monkeypatch.setenv(memory_aging.ENV_DAYS, '30')
    assert memory_aging.stale_entries(tmp_path, [entry])
    monkeypatch.setenv(memory_aging.ENV_DAYS, '60')
    assert memory_aging.stale_entries(tmp_path, [entry]) == []


def test_bad_threshold_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(memory_aging.ENV_DAYS, 'not-a-number')
    assert memory_aging.threshold_days() == memory_aging.DEFAULT_DAYS
    monkeypatch.setenv(memory_aging.ENV_DAYS, '-5')
    assert memory_aging.threshold_days() == memory_aging.DEFAULT_DAYS


def test_aging_can_be_disabled(tmp_path, monkeypatch):
    entry = _entry(tmp_path, 'old', 'fact', age_days=999)
    monkeypatch.setenv('HARNESS_MEMORY_AGING', '0')
    assert memory_aging.stale_entries(tmp_path, [entry]) == []


def test_missing_file_is_not_stale(tmp_path):
    """文件已不在时不报错也不返回。"""
    entry = {'name': 'gone', 'filename': 'gone.md', 'type': 'fact'}
    assert memory_aging.stale_entries(tmp_path, [entry]) == []


# ---------------------------------------------------------------- 归档（不删除）

def test_archive_moves_but_does_not_delete(tmp_path):
    entry = _entry(tmp_path, 'old', 'fact', age_days=200)
    target = memory_aging.archive(tmp_path, entry)
    assert target is not None and target.exists()
    assert not (tmp_path / 'old.md').exists()
    assert (tmp_path / '.archive' / 'old.md').exists()


def test_archive_name_collision_does_not_overwrite(tmp_path):
    """已归档内容不能被后来的同名文件覆盖。"""
    archive_dir = tmp_path / '.archive'
    archive_dir.mkdir()
    (archive_dir / 'old.md').write_text('first', encoding='utf-8')
    entry = _entry(tmp_path, 'old', 'fact', age_days=200)
    target = memory_aging.archive(tmp_path, entry)
    assert target is not None
    assert (archive_dir / 'old.md').read_text(encoding='utf-8') == 'first'
    assert target.name != 'old.md'


def test_restore_brings_it_back(tmp_path):
    entry = _entry(tmp_path, 'old', 'fact', age_days=200)
    memory_aging.archive(tmp_path, entry)
    assert memory_aging.restore_archived(tmp_path, 'old.md')
    assert (tmp_path / 'old.md').is_file()


def test_restore_refuses_to_overwrite(tmp_path):
    entry = _entry(tmp_path, 'old', 'fact', age_days=200)
    memory_aging.archive(tmp_path, entry)
    (tmp_path / 'old.md').write_text('current', encoding='utf-8')
    assert not memory_aging.restore_archived(tmp_path, 'old.md')
    assert (tmp_path / 'old.md').read_text(encoding='utf-8') == 'current'


def test_restore_rejects_path_traversal(tmp_path):
    assert not memory_aging.restore_archived(tmp_path, '../../etc/passwd')
    assert not memory_aging.restore_archived(tmp_path, 'sub/evil.md')


def test_archived_entries_lists_content(tmp_path):
    entry = _entry(tmp_path, 'old', 'fact', age_days=200)
    memory_aging.archive(tmp_path, entry)
    listed = memory_aging.archived_entries(tmp_path)
    assert [item['filename'] for item in listed] == ['old.md']


def test_archived_entries_empty_when_no_dir(tmp_path):
    assert memory_aging.archived_entries(tmp_path) == []


# ---------------------------------------------------------------- 记录容错

def test_broken_recall_log_is_ignored(tmp_path):
    """记录损坏不该导致删除任何东西。"""
    (tmp_path / memory_aging.LOG_NAME).write_text('{not json', encoding='utf-8')
    entry = _entry(tmp_path, 'new', 'fact', age_days=1)
    assert memory_aging.stale_entries(tmp_path, [entry]) == []


def test_record_recall_with_no_names_is_noop(tmp_path):
    memory_aging.record_recall(tmp_path, [])
    assert not (tmp_path / memory_aging.LOG_NAME).exists()


# ---------------------------------------------------------------- 接入 memory.age_memories

def test_age_memories_archives_and_reindexes(tmp_path, monkeypatch):
    import memory
    import llm_route
    home = llm_route.harness()
    memories = tmp_path / 'memories'
    memories.mkdir()
    monkeypatch.setattr(home, 'MEMORY_DIR', memories, raising=False)
    monkeypatch.setattr(home, 'MEMORY_INDEX', memories / 'MEMORY.md', raising=False)
    memory._cache_clear()

    memory.write_memory_file('old fact', 'fact', 'd', 'body')
    old = time.time() - 200 * 86400
    for path in memories.glob('*.md'):
        if path.name != 'MEMORY.md':
            os.utime(path, (old, old))

    moved = memory.age_memories()
    assert [item['name'] for item in moved] == ['old fact']
    assert (memories / '.archive').is_dir()
    index = memory.read_memory_index()
    assert 'old fact' not in index


def test_age_memories_keeps_preferences(tmp_path, monkeypatch):
    import memory
    import llm_route
    home = llm_route.harness()
    memories = tmp_path / 'memories'
    memories.mkdir()
    monkeypatch.setattr(home, 'MEMORY_DIR', memories, raising=False)
    monkeypatch.setattr(home, 'MEMORY_INDEX', memories / 'MEMORY.md', raising=False)
    memory._cache_clear()

    memory.write_memory_file('my pref', 'preference', 'd', 'body')
    old = time.time() - 500 * 86400
    for path in memories.glob('*.md'):
        if path.name != 'MEMORY.md':
            os.utime(path, (old, old))

    assert memory.age_memories() == []
    assert (memories / 'my-pref.md').is_file()
