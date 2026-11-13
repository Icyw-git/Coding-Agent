"""R1-c：记忆的 scope / 来源 / 版本（next-design §9.4）。

当前 `SCOPES = ('global', 'session')`——**没有 workspace**。后果是一台机器上所有项目
共享同一批"项目事实"：改了 A 项目的约定，B 项目下一次会话就开始按 A 的规则做事，
而用户完全不知道。这是设计文档 §6.1 点名的第一个问题。

本批只做「字段与读写保真」这一层：新增 `workspace` scope、`owner`（归属哪块工作区）、
`revision`（写入版本），并保证它们在 抽取 → UPDATE → SUPERSEDE → 合并 → 归档/恢复
的每一步都不丢。启用（按 workspace 过滤召回）是下一批。
"""

import memory
import memory_schema


def test_workspace_scope_exists_and_old_files_still_parse():
    assert 'workspace' in memory_schema.SCOPES
    assert memory_schema.normalize_scope('workspace') == 'workspace'
    # 旧文件没有 scope：仍按 global 读（不做自动归类，§6.1）
    assert memory_schema.normalize_scope('') == 'global'
    assert memory_schema.normalize_scope('nonsense') == 'global'


def test_entry_carries_owner_and_revision(tmp_path, monkeypatch):
    import harness

    monkeypatch.setattr(harness, 'MEMORY_DIR', tmp_path / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX', tmp_path / 'mem' / 'MEMORY.md')

    memory.write_memory_file('proj-convention', 'fact', '部署走 make ship-prod',
                             '正文', scope='workspace', owner='/repos/alpha')
    entry = next(item for item in memory.list_memory_files()
                 if item['name'] == 'proj-convention')

    assert entry['scope'] == 'workspace', entry
    assert entry['owner'] == '/repos/alpha', entry
    assert entry['revision'] == 1, '首次写入的 revision 应当是 1'


def test_revision_increments_on_each_write(tmp_path, monkeypatch):
    import harness

    monkeypatch.setattr(harness, 'MEMORY_DIR', tmp_path / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX', tmp_path / 'mem' / 'MEMORY.md')

    memory.write_memory_file('note', 'fact', '第一版', '正文')
    first = memory.list_memory_files()[0]['revision']
    memory.write_memory_file('note', 'fact', '第二版', '正文')
    second = memory.list_memory_files()[0]['revision']
    assert second == first + 1, (first, second)
