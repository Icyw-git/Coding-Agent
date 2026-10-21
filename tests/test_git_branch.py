"""git_branch：HEAD 解析、分离 HEAD 与 worktree 的 .git 文件。"""
from git_branch import current_branch


def _repo(tmp_path, head: str):
    gitdir = tmp_path / '.git'
    gitdir.mkdir()
    (gitdir / 'HEAD').write_text(head + '\n', encoding='utf-8')
    return tmp_path


def test_branch_from_head_ref(tmp_path):
    root = _repo(tmp_path, 'ref: refs/heads/feature/login')

    assert current_branch(root) == 'login'


def test_detached_head_returns_short_hash(tmp_path):
    root = _repo(tmp_path, '0123456789abcdef0123456789abcdef01234567')

    assert current_branch(root) == '01234567'


def test_git_file_points_to_worktree_gitdir(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    (real / 'HEAD').write_text('ref: refs/heads/work\n', encoding='utf-8')
    worktree = tmp_path / 'wt'
    worktree.mkdir()
    (worktree / '.git').write_text(f'gitdir: {real}\n', encoding='utf-8')

    assert current_branch(worktree) == 'work'


def test_missing_or_unreadable_git_returns_none(tmp_path):
    assert current_branch(tmp_path) is None

    gitdir = tmp_path / '.git'
    gitdir.mkdir()
    assert current_branch(tmp_path) is None   # 没有 HEAD


def test_relative_gitdir_path(tmp_path):
    (tmp_path / 'meta' / 'git').mkdir(parents=True)
    (tmp_path / 'meta' / 'git' / 'HEAD').write_text(
        'ref: refs/heads/rel\n', encoding='utf-8')
    (tmp_path / '.git').write_text('gitdir: meta/git\n', encoding='utf-8')

    assert current_branch(tmp_path) == 'rel'
