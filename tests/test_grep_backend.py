"""grep 的 ripgrep 快路径与 Python 回退边界。"""
import tools


def _tree(tmp_path):
    root = tmp_path / 'tree'
    root.mkdir()
    (root / 'a.py').write_text('needle\n', encoding='utf-8')
    return root


def test_ascii_pattern_goes_through_ripgrep_path(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    calls = []
    real = tools._grep_with_ripgrep

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(tools, '_grep_with_ripgrep', spy)
    out = tools.run_grep('needle', path=str(root))
    assert calls, 'directory search should try the ripgrep backend'
    assert 'a.py:1: needle' in out


def test_non_ascii_pattern_skips_ripgrep(tmp_path):
    root = _tree(tmp_path)
    assert tools._grep_with_ripgrep(
        '关键词', root, None, False, 10) is None


def test_missing_ripgrep_falls_back_to_python(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    monkeypatch.setattr(tools, '_rg_binary', lambda: None)
    assert 'a.py:1: needle' in tools.run_grep('needle', path=str(root))


def test_lossy_ripgrep_output_falls_back_for_gbk_file(tmp_path):
    root = _tree(tmp_path)
    (root / 'gbk.txt').write_bytes('前缀 needle 后缀'.encode('gbk'))
    out = tools.run_grep('needle', path=str(root))
    assert 'gbk.txt:1' in out
    assert '前缀 needle 后缀' in out


# ---------- 两个后端的排除范围必须一致 ----------

def _nested_ignored_tree(tmp_path):
    """根目录**没有** node_modules，但子目录里有——globs 必须递归。"""
    root = tmp_path / 'repo'
    (root / 'pkg' / 'node_modules' / 'dep').mkdir(parents=True)
    (root / 'keep.py').write_text('needle\n', encoding='utf-8')
    (root / 'pkg' / 'node_modules' / 'dep' / 'index.js').write_text(
        'needle\n', encoding='utf-8')
    return root


def test_ripgrep_excludes_nested_directories(tmp_path):
    """`--glob '!node_modules/**'` 只匹配根目录，扫不进 `pkg/node_modules`。

    实测：仓库根没有 node_modules，但 `visualizer/node_modules` 有 106MB，于是
    `grep` 扫全仓要 12–64 秒、命中 0 条。Python 回退按**目录名**在任意层级跳过，
    两个后端范围不一致（同样的查询给出不同结果）。
    """
    root = _nested_ignored_tree(tmp_path)
    out = tools._grep_with_ripgrep('needle', root, None, False, 50)
    assert out is not None, 'ripgrep 应当可用'
    assert 'keep.py' in out
    assert 'node_modules' not in out


def test_both_backends_agree_on_scope(tmp_path):
    """同一棵树、同一个查询：快路径与回退路径的结果集合必须相同。

    后端不同只该影响"多快"，不该影响"搜到了什么"。
    """
    import tool_search

    root = _nested_ignored_tree(tmp_path)
    gate = lambda *_a, **_k: None          # noqa: E731
    resolve = lambda path, cwd: root / path  # noqa: E731

    fast = tool_search.grep_files('needle', '.', None, gate=gate, resolve=resolve,
                                  backend=tool_search._grep_with_ripgrep)
    slow = tool_search.grep_files('needle', '.', None, gate=gate, resolve=resolve,
                                  backend=lambda *a, **k: None)
    fast_hits = {line.split(':')[0] for line in fast.splitlines() if ':' in line}
    slow_hits = {line.split(':')[0] for line in slow.splitlines() if ':' in line}
    assert fast_hits == slow_hits == {'keep.py'}
