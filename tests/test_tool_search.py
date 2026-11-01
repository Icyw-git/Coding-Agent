"""tool_search：注入的路径边界、隐藏项与秘密文件过滤。"""
import tool_search


def _access(root, *, denied=None, log=None):
    log = [] if log is None else log

    def gate(tool, path, cwd):
        log.append(('gate', tool, path))
        return denied

    def resolve(path, cwd):
        log.append(('resolve', path))
        return root / path

    return gate, resolve


def test_search_root_denial_skips_resolution(tmp_path):
    log = []
    gate, resolve = _access(tmp_path, denied='Error: denied', log=log)

    root, error = tool_search.search_root('.', None, gate=gate, resolve=resolve)

    assert root is None and error == 'Error: denied'
    assert [entry[0] for entry in log] == ['gate']


def test_ls_dir_hides_dotfiles_unless_requested(tmp_path):
    gate, resolve = _access(tmp_path)
    (tmp_path / 'visible.txt').write_text('x', encoding='utf-8')
    (tmp_path / '.hidden').write_text('x', encoding='utf-8')

    hidden = tool_search.ls_dir('.', False, None, gate=gate, resolve=resolve)
    everything = tool_search.ls_dir('.', True, None, gate=gate, resolve=resolve)

    assert 'visible.txt' in hidden and '.hidden' not in hidden
    assert '.hidden' in everything


def test_glob_files_reports_missing_root(tmp_path):
    gate, resolve = _access(tmp_path)

    output = tool_search.glob_files('*.py', 'missing', None,
                                    gate=gate, resolve=resolve)

    assert 'not found' in output


def test_grep_skips_secret_files(tmp_path):
    gate, resolve = _access(tmp_path)
    (tmp_path / 'a.py').write_text('needle\n', encoding='utf-8')
    (tmp_path / '.env').write_text('needle\n', encoding='utf-8')

    output = tool_search.grep_files(
        'needle', '.', None, gate=gate, resolve=resolve, backend=lambda *a: None)

    assert 'a.py:1' in output
    assert '.env' not in output


# ---------- 输出上限 ----------

def test_ls_dir_caps_rows_and_says_how_many(tmp_path):
    """目录列举必须有上限。

    实测 ls C:/Windows/System32 一次 4689 行 / 13.6 万字符（约 4.5 万 token），
    一次只读调用能把上下文吃掉一半。截断必须报数，否则模型不知道还有多少。
    """
    total = tool_search.MAX_PATH_ROWS + 7
    for index in range(total):
        (tmp_path / f'f{index:04d}.txt').write_text('x', encoding='utf-8')
    gate, resolve = _access(tmp_path)

    output = tool_search.ls_dir('.', False, None, gate=gate, resolve=resolve)

    rows = output.splitlines()
    assert len(rows) == tool_search.MAX_PATH_ROWS + 1
    assert rows[-1] == '(truncated: 7 more entries)'
    assert 'f0000.txt' in output and f'f{total - 1:04d}.txt' not in output


def test_glob_files_caps_results(tmp_path):
    for index in range(tool_search.MAX_PATH_ROWS + 3):
        (tmp_path / f'g{index:04d}.py').write_text('x', encoding='utf-8')
    gate, resolve = _access(tmp_path)

    output = tool_search.glob_files('*.py', '.', None, gate=gate, resolve=resolve)

    rows = output.splitlines()
    assert len(rows) == tool_search.MAX_PATH_ROWS + 1
    assert rows[-1] == '(truncated: 3 more matches)'


def test_small_listings_are_not_truncated(tmp_path):
    """没到上限时不能多出一行标记。"""
    (tmp_path / 'only.txt').write_text('x', encoding='utf-8')
    gate, resolve = _access(tmp_path)

    assert 'truncated' not in tool_search.ls_dir(
        '.', False, None, gate=gate, resolve=resolve)
    assert 'truncated' not in tool_search.glob_files(
        '*.txt', '.', None, gate=gate, resolve=resolve)
