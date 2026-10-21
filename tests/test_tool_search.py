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
