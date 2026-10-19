"""文件检索工具：grep / ls / glob 的内容行为与安全边界。

检索工具是只读的：读哪里都放行，但秘密文件不能通过 grep 绕过 read 的硬拒；
teammate 在 worktree 里执行时，三个工具都必须跟着 cwd 走。
"""
from pathlib import Path

import agent_teams
import hooks
import tools


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / 'tree'
    (root / 'pkg').mkdir(parents=True)
    (root / 'a.py').write_text('def needle():\n    return 1\n', encoding='utf-8')
    (root / 'b.txt').write_text('NEEDLE here\nneedle there\n', encoding='utf-8')
    (root / 'pkg' / 'c.py').write_text('class Needle:\n    pass\n', encoding='utf-8')
    (root / '.env').write_text('TOKEN=needle-secret\n', encoding='utf-8')
    (root / '.git').mkdir()
    (root / '.git' / 'config').write_text('needle in git\n', encoding='utf-8')
    return root


def test_grep_reports_lines_and_filters_glob(tmp_path):
    root = _tree(tmp_path)
    out = tools.run_grep('needle', path=str(root))
    assert 'a.py:1: def needle():' in out
    assert 'b.txt:2: needle there' in out
    assert 'b.txt:1: NEEDLE here' not in out   # 默认大小写敏感

    out = tools.run_grep('needle', path=str(root), glob='*.py',
                         case_insensitive=True)
    assert 'a.py:1' in out
    assert 'c.py:1' in out
    assert 'b.txt' not in out


def test_grep_skips_secret_files_and_ignored_directories(tmp_path):
    root = _tree(tmp_path)
    out = tools.run_grep('needle-secret|needle in git', path=str(root))
    assert out == '(no matches)'
    assert 'TOKEN' not in out


def test_grep_explicit_secret_path_is_denied(tmp_path):
    root = _tree(tmp_path)
    out = tools.run_grep('TOKEN', path=str(root / '.env'))
    assert out.startswith('Error: access to protected path')
    assert 'needle-secret' not in out


def test_grep_invalid_regex_is_an_error_not_a_traceback(tmp_path):
    out = tools.run_grep('(', path=str(tmp_path))
    assert out.startswith('Error: invalid pattern')


def test_grep_caps_matches_and_says_so(tmp_path):
    (tmp_path / 'many.txt').write_text('\n'.join('hit' for _ in range(10)),
                                       encoding='utf-8')
    out = tools.run_grep('hit', path=str(tmp_path), max_results=3)
    assert out.count(': hit') == 3
    assert 'truncated at 3 matches' in out


def test_ls_hides_dotfiles_unless_all(tmp_path):
    root = _tree(tmp_path)
    assert '.env' not in tools.run_ls(str(root))
    shown = tools.run_ls(str(root), all=True)
    assert 'f  .env' in shown
    assert 'd  pkg/' in shown
    assert 'd  .git/' in shown
    assert 'config' not in shown   # 只列一层
    assert tools.run_ls(str(root / 'a.py')).startswith('f  a.py')


def test_ls_missing_path_is_an_error(tmp_path):
    assert tools.run_ls(str(tmp_path / 'missing')).startswith('Error: path not found')


def test_glob_accepts_a_root_outside_the_workspace(tmp_path):
    root = _tree(tmp_path)
    out = tools.run_glob('**/*.py', path=str(root))
    assert 'a.py' in out
    assert 'c.py' in out
    assert 'b.txt' not in out


def test_search_tools_honor_cwd(tmp_path):
    """teammate 用 cwd 指向 worktree；三个检索工具都不能漏掉这个参数。"""
    root = _tree(tmp_path)
    assert 'a.py' in tools.run_glob('*.py', cwd=str(root))
    assert 'a.py:1' in tools.run_grep('needle', cwd=str(root))
    assert 'pkg/' in tools.run_ls(cwd=str(root))


def test_teammate_search_tools_are_scoped_to_the_worktree(tmp_path):
    worktree = tmp_path / 'wt'
    worktree.mkdir()
    (worktree / 'only_here.py').write_text('needle\n', encoding='utf-8')

    sub_tools, handlers = agent_teams._build_team_tools(
        'worker', {'path': str(worktree)})
    names = {item['function']['name'] for item in sub_tools}
    assert {'glob', 'grep', 'ls'} <= names
    assert 'only_here.py:1' in handlers['grep']('needle', '.')
    assert 'only_here.py' in handlers['glob']('*.py')
    assert 'only_here.py' in handlers['ls']('.')


def test_policy_denies_secret_roots_for_search_tools(tmp_path):
    secret = tmp_path / '.env'
    secret.write_text('x', encoding='utf-8')
    for tool in ('grep', 'ls', 'glob'):
        decision = hooks.evaluate_call(tool, {'path': str(secret)})
        assert decision.action == 'deny'
        assert decision.code == 'secret_path'
