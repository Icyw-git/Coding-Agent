"""R1-c：遗忘是写入协议，不是 unlink（next-design §6.2 / §9.4）。

原来 `delete_memory_file()` 只做一件事：`unlink` 当前活动文件。于是：

- 正在跑的抽取/合并拿着**改之前的快照**，回写时把刚删的记忆原样写回来；
- 换一个 `memory_id` 重新生成同一条，绕开"同名才拦"的检查；
- 清缓存 / 重建索引之后，什么都没了，谁也不记得它被遗忘过。

契约（§6.2 第 2–5 条）：

1. 遗忘先落**不可再召回的墓碑**，墓碑落盘成功后才删文件；墓碑失败要返回"遗忘未完成"；
2. 墓碑带 generation（作用域级）+ 来源谱系，之后的召回/写入都检查它；
3. 重建索引、清缓存都不解除遗忘。
"""

import json

import pytest

import memory
import memory_tombstone


@pytest.fixture()
def home(tmp_path, monkeypatch):
    import harness

    monkeypatch.setattr(harness, 'MEMORY_DIR', tmp_path / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX', tmp_path / 'mem' / 'MEMORY.md')
    monkeypatch.setattr(memory_tombstone, 'STORE_PATH', tmp_path / 'mem' / 'tombstones.json')
    return tmp_path / 'mem'


def test_tombstone_is_written_before_the_file_disappears(home):
    memory.write_memory_file('deploy', 'fact', '部署走 make ship-prod', '正文',
                             source='s-1', quote='部署用 make ship-prod')
    assert memory.delete_memory_file('deploy') is True

    record = memory_tombstone.load()
    assert [item['name'] for item in record['entries']] == ['deploy'], record
    assert record['generation'] >= 1
    # 只留阻止复活所需的最小标识：不留被遗忘的正文
    assert 'ship-prod' not in json.dumps(record, ensure_ascii=False)


def test_recall_and_listing_skip_tombstoned_entries(home):
    memory.write_memory_file('deploy', 'fact', '部署走 make ship-prod', '正文')
    memory.delete_memory_file('deploy')
    memory.write_memory_file('other', 'fact', '别的', '正文')

    assert [item['name'] for item in memory.list_memory_files()] == ['other']
    # 重建索引也不能把它带回来
    memory._rebuild_index()
    index = (home / 'MEMORY.md').read_text(encoding='utf-8')
    assert 'deploy' not in index, index


def test_a_stale_candidate_cannot_resurrect_a_forgotten_name(home):
    """旧快照里还有这条 → 回写时必须被拦（这正是"删了又被写回来"的修复）。"""
    memory.write_memory_file('deploy', 'fact', '部署走 make ship-prod', '正文',
                             source='s-1')
    stale = [item for item in memory.list_memory_files() if item['name'] == 'deploy'][0]
    memory.delete_memory_file('deploy')

    allowed = memory_tombstone.may_revive(stale['name'], stale.get('source_session', ''))
    assert allowed is False


def test_tombstone_failure_means_the_forget_did_not_happen(home, monkeypatch):
    """墓碑落盘失败（磁盘满/权限）→ 不能删文件，也不能宣称成功。

    这里 patch 的是**真正的落盘调用**（`Path.write_text`），不是包装层——
    否则测不到「OSError 被包成 TombstoneError」这一段。
    """
    memory.write_memory_file('deploy', 'fact', 'x', '正文')

    from pathlib import Path

    real_write_text = Path.write_text

    def failing(self, *args, **kwargs):
        if str(self).endswith('.tmp'):   # 墓碑的临时文件
            raise OSError('disk full')
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', failing)
    with pytest.raises(memory_tombstone.TombstoneError):
        memory.delete_memory_file('deploy')
    monkeypatch.setattr(Path, 'write_text', real_write_text)

    # 文件必须还在：遗忘没完成
    assert [item['name'] for item in memory.list_memory_files()] == ['deploy']


def test_tombstone_survives_cache_clear_and_reindex(home):
    memory.write_memory_file('deploy', 'fact', 'x', '正文')
    memory.delete_memory_file('deploy')

    memory._cache_clear()
    memory._rebuild_index()

    assert memory_tombstone.may_revive('deploy', '') is False
    assert memory.list_memory_files() == []


def test_explicit_remember_can_lift_the_block(home):
    """只有显式“重新记住”才能解除（§6.2 第 4 条），并记下新依据。"""
    memory.write_memory_file('deploy', 'fact', 'x', '正文')
    memory.delete_memory_file('deploy')

    memory_tombstone.revive('deploy', reason='用户明确要求重新记住')
    assert memory_tombstone.may_revive('deploy', '') is True
    record = memory_tombstone.load()
    assert record['entries'] == [] or not any(
        item['name'] == 'deploy' for item in record['entries'])
    assert record.get('last_revive', {}).get('reason') == '用户明确要求重新记住'


def test_same_source_under_a_new_name_is_also_blocked(home):
    """换名字但同来源（同一段会话）也不能复活——只拦同名的检查会被绕过。"""
    memory.write_memory_file('deploy', 'fact', 'x', '正文', source='sess-42')
    memory.delete_memory_file('deploy')

    assert memory_tombstone.may_revive('deploy-v2', 'sess-42') is False
    assert memory_tombstone.may_revive('deploy-v2', '') is True   # 来源不同则放行


def test_tombstone_store_is_bounded_and_error_tolerant(home, monkeypatch):
    """明细有界、坏文件不会让遗忘功能整体失效。"""
    for index in range(memory_tombstone.MAX_ENTRIES + 5):
        memory_tombstone.block(f'mem-{index}')
    record = memory_tombstone.load()
    assert len(record['entries']) == memory_tombstone.MAX_ENTRIES
    assert record['generation'] == memory_tombstone.MAX_ENTRIES + 5

    memory_tombstone.STORE_PATH.write_text('{not json', encoding='utf-8')
    assert memory_tombstone.may_revive('anything', '') is True
