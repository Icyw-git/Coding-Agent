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
