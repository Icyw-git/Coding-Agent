"""记忆写入安全：合并失败不丢旧数据、文件名不越界、索引与文件一致。"""
import threading

import harness
import memory


def _seed(mem, count=3):
    for index in range(count):
        harness.write_memory_file(f'old-{index}', 'user',
                                  f'旧描述 {index}', f'旧正文 {index}')


def _names(mem):
    return sorted(path.name for path in mem.glob('*.md')
                  if path.name != 'MEMORY.md')


def _setup(monkeypatch, tmp_path, count=3):
    mem = tmp_path / 'mem'
    monkeypatch.setattr(harness, 'MEMORY_DIR', mem)
    monkeypatch.setattr(harness, 'MEMORY_INDEX', mem / 'MEMORY.md')
    _seed(mem, count)
    return mem


def test_invalid_consolidation_keeps_existing_memories(monkeypatch, tmp_path):
    mem = _setup(monkeypatch, tmp_path)
    before = _names(mem)
    index_before = harness.read_memory_index()

    for bad in ([{'name': 'x', 'type': 'user', 'description': 'd'}],          # 缺 body
                [{'name': 'x', 'type': 'nope', 'description': 'd', 'body': 'b'}],
                [{'name': 'dup', 'type': 'user', 'description': 'd', 'body': 'b'},
                 {'name': 'dup', 'type': 'user', 'description': 'd', 'body': 'b'}]):
        try:
            prepared = memory._validate_consolidated(bad)
        except ValueError:
            prepared = None
        assert prepared is None, bad
        assert _names(mem) == before
        assert harness.read_memory_index() == index_before
    assert not list(mem.glob('.staging-*'))


def test_staging_failure_keeps_old_files_and_cleans_up(monkeypatch, tmp_path):
    mem = _setup(monkeypatch, tmp_path)
    before = _names(mem)
    prepared = memory._validate_consolidated([
        {'name': 'new-a', 'type': 'project', 'description': 'a', 'body': 'A'},
        {'name': 'new-b', 'type': 'project', 'description': 'b', 'body': 'B'},
    ])
    original = memory._atomic_write
    calls = {'staged': 0}

    def flaky(path, text):
        if path.parent.name.startswith('.staging-'):
            calls['staged'] += 1
            if calls['staged'] == 2:
                raise OSError('disk full')
        return original(path, text)

    monkeypatch.setattr(memory, '_atomic_write', flaky)
    try:
        memory._replace_memory_files(prepared)
    except OSError:
        pass
    else:  # pragma: no cover - 失败路径必须抛出让调用方记录
        raise AssertionError('staging failure should propagate')
    assert _names(mem) == before, '旧记忆不能被删除'
    assert not list(mem.glob('.staging-*'))


def test_consolidation_replaces_set_and_rebuilds_index(monkeypatch, tmp_path):
    mem = _setup(monkeypatch, tmp_path)
    prepared = memory._validate_consolidated([
        {'name': 'merged one', 'type': 'project',
         'description': '合并描述', 'body': '合并正文'},
    ])
    memory._replace_memory_files(prepared)
    assert _names(mem) == ['merged-one.md']
    index = harness.read_memory_index()
    assert 'merged one' in index and '旧描述' not in index
    assert harness.list_memory_files()[0]['body'].strip() == '合并正文'


def test_memory_name_cannot_escape_directory(monkeypatch, tmp_path):
    mem = tmp_path / 'mem'
    monkeypatch.setattr(harness, 'MEMORY_DIR', mem)
    monkeypatch.setattr(harness, 'MEMORY_INDEX', mem / 'MEMORY.md')
    for name in ('../evil', '..\\evil', '/etc/passwd', 'a/b'):
        path, _action = harness.write_memory_file(name, 'user', 'd', 'b')
        assert path.parent == mem, path
        assert mem in path.parents
    assert not (tmp_path / 'evil.md').exists()


def test_multiline_description_stays_in_frontmatter(monkeypatch, tmp_path):
    mem = tmp_path / 'mem'
    monkeypatch.setattr(harness, 'MEMORY_DIR', mem)
    monkeypatch.setattr(harness, 'MEMORY_INDEX', mem / 'MEMORY.md')
    harness.write_memory_file('multi', 'user', 'first\nsecond', 'body line')
    files = harness.list_memory_files()
    assert files[0]['description'] == 'first second'
    assert files[0]['body'].strip() == 'body line'


def test_concurrent_writes_keep_index_consistent(monkeypatch, tmp_path):
    mem = tmp_path / 'mem'
    monkeypatch.setattr(harness, 'MEMORY_DIR', mem)
    monkeypatch.setattr(harness, 'MEMORY_INDEX', mem / 'MEMORY.md')
    errors = []

    def worker(prefix):
        try:
            for index in range(5):
                # 内容必须真正不同：写入策略会合并"同一条信息的不同措辞"，
                # 只差一个字母的描述（a/b 开头）本来就会被当成同一条。
                # 这里测的是并发写索引，不是去重。
                harness.write_memory_file(
                    f'{prefix}-{index}', 'user',
                    f'{prefix} 主题{index} 的部署说明',
                    f'{prefix} 正文{index} 关于构建与发布的细节')
        except Exception as exc:  # pragma: no cover - 失败即测试失败
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(prefix,))
               for prefix in ('a', 'b')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    assert len(_names(mem)) == 10
    index_lines = [line for line in harness.read_memory_index().splitlines() if line]
    assert len(index_lines) == 10, index_lines


def test_frontmatter_cache_invalidates_on_rewrite(monkeypatch, tmp_path):
    mem = tmp_path / 'mem'
    monkeypatch.setattr(harness, 'MEMORY_DIR', mem)
    monkeypatch.setattr(harness, 'MEMORY_INDEX', mem / 'MEMORY.md')
    harness.write_memory_file('cached', 'user', '第一版', '正文 v1')
    assert harness.list_memory_files()[0]['description'] == '第一版'
    assert harness.read_memory_file('cached.md').count('正文 v1') == 1

    harness.write_memory_file('cached', 'user', '第二版', '正文 v2')
    files = harness.list_memory_files()
    assert files[0]['description'] == '第二版'
    assert '正文 v2' in harness.read_memory_file('cached.md')
    assert '正文 v1' not in harness.read_memory_file('cached.md')


def test_frontmatter_cache_sees_external_edit(monkeypatch, tmp_path):
    import os

    mem = tmp_path / 'mem'
    monkeypatch.setattr(harness, 'MEMORY_DIR', mem)
    monkeypatch.setattr(harness, 'MEMORY_INDEX', mem / 'MEMORY.md')
    harness.write_memory_file('external', 'user', '原描述', '原正文')
    assert harness.list_memory_files()[0]['description'] == '原描述'

    path = mem / 'external.md'
    path.write_text('---\nname: external\ndescription: 新描述\ntype: user\n---\n\n新正文\n',
                    encoding='utf-8')
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert harness.list_memory_files()[0]['description'] == '新描述'


def test_read_memory_file_rejects_path_escape(monkeypatch, tmp_path):
    mem = tmp_path / 'mem'
    monkeypatch.setattr(harness, 'MEMORY_DIR', mem)
    monkeypatch.setattr(harness, 'MEMORY_INDEX', mem / 'MEMORY.md')
    harness.write_memory_file('inside', 'user', 'd', 'b')
    (tmp_path / 'secret.md').write_text('secret', encoding='utf-8')
    assert harness.read_memory_file('../secret.md') is None
    assert harness.read_memory_file(tmp_path / 'secret.md') is None
    assert harness.read_memory_file('inside.md') is not None
