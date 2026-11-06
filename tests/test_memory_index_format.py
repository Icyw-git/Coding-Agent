"""人看的索引要长得像它注释里说的那样。

`_rebuild_index_unlocked` 的注释写着「索引结构：[名称](文件名) - 描述」，
实际拼的是：

    f'- [{name}({f.name}) - {desc}]'      # ']' 在整行末尾

于是 `[名称(文件名) - 描述]` —— **不是 markdown 链接**（`[` 后面没有紧跟 `](`），
描述也被包进了方括号里。用户打开 `~/.cache/memories/MEMORY.md` 看到的是一堆
形状奇怪的括号。

这份索引**代码里没有任何读取方**（召回走 `list_memory_files()`，`/memories` 也走它），
所以它纯粹是给人看的——那就更该长得对。
"""

import memory


def _index(tmp_path, monkeypatch):
    import harness

    monkeypatch.setattr(harness, 'MEMORY_DIR', tmp_path / 'mem')
    monkeypatch.setattr(harness, 'MEMORY_INDEX', tmp_path / 'mem' / 'MEMORY.md')
    (tmp_path / 'mem').mkdir(parents=True, exist_ok=True)
    return harness


def test_index_line_is_a_real_markdown_link(tmp_path, monkeypatch):
    _index(tmp_path, monkeypatch)
    memory.write_memory_file('prefer-tea', 'preference',
                             '下午更想喝奶茶', '正文')
    text = (tmp_path / 'mem' / 'MEMORY.md').read_text(encoding='utf-8')

    line = next(item for item in text.splitlines() if 'prefer-tea' in item)
    assert line.startswith('- [prefer-tea](prefer-tea.md)'), line
    assert line.endswith('下午更想喝奶茶'), line
