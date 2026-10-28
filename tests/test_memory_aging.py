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

import pytest

import memory_aging


def _entry(memory_dir, name, mem_type, *, age_days=0, body='body'):
    """造一条记忆并把它（连同 mtime）设为指定年龄。"""
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
    import llm_route
    import memory
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
    import llm_route
    import memory
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


# ---------------------------------------------------------------- A6 低信号剪枝

@pytest.fixture
def low_signal_home(tmp_path, monkeypatch):
    """把记忆目录指到 tmp，读写都落在这里（否则会写进真实 .cache）。"""
    import memory
    home = memory._h()
    monkeypatch.setattr(home, 'MEMORY_DIR', tmp_path, raising=False)
    monkeypatch.setattr(home, 'MEMORY_INDEX', tmp_path / 'MEMORY.md', raising=False)
    memory._cache_clear()
    yield tmp_path
    memory._cache_clear()


def _entry_with(memory_dir, name, mem_type='fact', confidence=0.1, age_days=60):
    """造一条带置信度的条目并设年龄（需配合 low_signal_home）。"""
    import memory
    memory.write_memory_file(name, mem_type, f'{name} description', 'body',
                             confidence=confidence)
    path = memory_dir / f'{name}.md'
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    memory._cache_clear()
    return next(item for item in memory.list_memory_files()
                if item['name'] == name)


def test_low_signal_entry_is_pruned(low_signal_home, tmp_path):
    entry = _entry_with(tmp_path, 'noise', confidence=0.1)
    found = memory_aging.low_signal_entries(tmp_path, [entry])
    assert [item['name'] for item in found] == ['noise']


def test_high_confidence_is_kept(low_signal_home, tmp_path):
    """高置信条目即使没被召回也不清——它可能是刚写的、还没用上。"""
    entry = _entry_with(tmp_path, 'solid', confidence=0.9)
    assert memory_aging.low_signal_entries(tmp_path, [entry]) == []


def test_recalled_entry_is_kept(low_signal_home, tmp_path):
    """只要有召回记录，就说明它对某次对话有用。"""
    entry = _entry_with(tmp_path, 'used', confidence=0.1)
    memory_aging.record_recall(tmp_path, ['used.md'])
    assert memory_aging.low_signal_entries(tmp_path, [entry]) == []


def test_fresh_low_signal_entry_is_kept(low_signal_home, tmp_path):
    """观察期内不清：新条目还没来得及被用到。"""
    entry = _entry_with(tmp_path, 'newish', confidence=0.1, age_days=1)
    assert memory_aging.low_signal_entries(tmp_path, [entry]) == []


@pytest.mark.parametrize('mem_type', ['preference', 'correction'])
def test_preferences_and_corrections_never_pruned(low_signal_home, tmp_path, mem_type):
    entry = _entry_with(tmp_path, f'keep-{mem_type}', mem_type=mem_type,
                        confidence=0.05)
    assert memory_aging.low_signal_entries(tmp_path, [entry]) == []


def test_low_signal_thresholds_are_configurable(low_signal_home, tmp_path):
    entry = _entry_with(tmp_path, 'x', confidence=0.25, age_days=10)
    assert memory_aging.low_signal_entries(tmp_path, [entry]) == []
    assert memory_aging.low_signal_entries(
        tmp_path, [entry], days=5, confidence=0.3)


def test_missing_confidence_is_not_pruned(tmp_path):
    """没记置信度的旧条目按"不清楚"处理，不清——宁可留。"""
    path = tmp_path / 'old.md'
    path.write_text('---\nname: old\ndescription: d\ntype: fact\n---\n\nbody\n',
                    encoding='utf-8')
    stamp = time.time() - 90 * 86400
    os.utime(path, (stamp, stamp))
    entry = {'filename': 'old.md', 'name': 'old', 'type': 'fact'}
    assert memory_aging.low_signal_entries(tmp_path, [entry]) == []


def test_age_memories_prunes_noise_but_keeps_signal(tmp_path, monkeypatch):
    """端到端：噪音被归档，高置信 / 被召回 / 偏好全部保留。"""
    import memory
    home = memory._h()
    monkeypatch.setattr(home, 'MEMORY_DIR', tmp_path, raising=False)
    monkeypatch.setattr(home, 'MEMORY_INDEX', tmp_path / 'MEMORY.md', raising=False)
    memory._cache_clear()

    _entry_with(tmp_path, 'noise', confidence=0.1)
    _entry_with(tmp_path, 'solid', confidence=0.9)
    memory_aging.record_recall(tmp_path, ['solid.md'])
    _entry_with(tmp_path, 'pref', mem_type='preference', confidence=0.1)

    moved = memory.age_memories()
    assert [item['name'] for item in moved] == ['noise']
    remaining = {item['name'] for item in memory.list_memory_files()}
    assert remaining == {'solid', 'pref'}


# ---------------------------------------------------------------- 缓存淘汰（性能回归）

def test_frontmatter_cache_survives_above_the_limit(tmp_path, monkeypatch):
    """缓存超上限时逐条淘汰，不能整体清空。

    实测踩到的 bug：原来到 256 条就 `clear()`，记忆数一旦超过上限，每次插入都
    清空全表、命中率归零——500 条时一次索引重建要 6.8 秒，热缓存读也要 240ms。
    """
    import memory

    home = memory._h()
    monkeypatch.setattr(home, 'MEMORY_DIR', tmp_path, raising=False)
    monkeypatch.setattr(home, 'MEMORY_INDEX', tmp_path / 'MEMORY.md', raising=False)
    memory._cache_clear()

    total = memory._CACHE_MAX + 40
    for index in range(total):
        (tmp_path / f'n{index}.md').write_text(
            f'---\nname: n{index}\ndescription: d\ntype: fact\n---\n\nbody\n',
            encoding='utf-8')
    memory._cache_clear()

    import time

    started = time.perf_counter()
    first = memory.list_memory_files()          # 冷：填满缓存（会触发淘汰）
    cold = time.perf_counter() - started
    assert len(first) == total
    # 淘汰后缓存里仍应有内容，而不是被清空
    assert memory._FRONTMATTER_CACHE, 'cache was cleared instead of evicted'
    assert len(memory._FRONTMATTER_CACHE) <= memory._CACHE_MAX

    # 相对比较而不是绝对阈值：淘汰掉一半后第一次仍是半冷，再读一次才全热。
    # 绝对阈值在负载下会误报（实测冷读 296 条要 0.75s）。
    memory.list_memory_files()
    started = time.perf_counter()
    memory.list_memory_files()
    warm = time.perf_counter() - started
    assert warm < cold * 0.6, f'cold={cold:.3f}s warm={warm:.3f}s: 缓存没生效'


def test_age_scan_reads_recall_log_once(tmp_path, monkeypatch):
    """扫描全部记忆时召回记录只读一次，不是每条重读。

    实测：不传 log 时 30 条要 74ms（每次 age_days 都读盘 + 解析 json），
    而 age_memories 每轮合并后都会跑。
    """
    import memory

    home = memory._h()
    monkeypatch.setattr(home, 'MEMORY_DIR', tmp_path, raising=False)
    monkeypatch.setattr(home, 'MEMORY_INDEX', tmp_path / 'MEMORY.md', raising=False)
    memory._cache_clear()
    for index in range(20):
        (tmp_path / f'n{index}.md').write_text(
            f'---\nname: n{index}\ndescription: d\ntype: fact\n---\n\nbody\n',
            encoding='utf-8')
    memory._cache_clear()
    entries = memory.list_memory_files()

    calls = {'n': 0}
    original = memory_aging._read_log

    def counting(memory_dir):
        calls['n'] += 1
        return original(memory_dir)

    monkeypatch.setattr(memory_aging, '_read_log', counting)
    memory_aging.stale_entries(tmp_path, entries)
    assert calls['n'] == 1, f'recall log read {calls["n"]} times, expected 1'

    calls['n'] = 0
    memory_aging.low_signal_entries(tmp_path, entries)
    assert calls['n'] == 1
