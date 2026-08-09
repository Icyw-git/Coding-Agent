"""worktree 模块单元测试：创建 → 绑定任务 → 删除保护 → 强制删除 → 保留 完整流程。

用临时 git 仓库做真实 `git worktree` 操作，避免污染真实仓库；
worktree/task 的目录全局在测试内重定向到临时目录（同 test_memory 的模式）。
"""
import json
import pathlib
import shutil
import subprocess
import tempfile

import task_system
import worktree


def _git(repo: pathlib.Path, *args):
    return subprocess.run(['git'] + list(args), cwd=repo,
                          capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=30)


def _make_repo(tmp: pathlib.Path):
    """init 一个带初始提交的临时 git 仓库（worktree add 需要 HEAD）。"""
    _git(tmp, 'init', '-q')
    _git(tmp, 'config', 'user.email', 'test@example.com')
    _git(tmp, 'config', 'user.name', 'test')
    (tmp / 'base.txt').write_text('base\n', encoding='utf-8')
    _git(tmp, 'add', '.')
    r = _git(tmp, 'commit', '-qm', 'init')
    assert r.returncode == 0, r.stderr


def test_worktree_full_flow():
    tmp = pathlib.Path(tempfile.mkdtemp())
    _make_repo(tmp)

    # 隔离 worktree / task 目录
    orig_wd, orig_wt = worktree.WORKDIR, worktree.WORKTREES_DIR
    orig_td = task_system.TASK_DIR
    worktree.WORKDIR = tmp
    worktree.WORKTREES_DIR = tmp / '.worktrees'
    worktree.WORKTREES_DIR.mkdir(exist_ok=True)
    task_system.TASK_DIR = tmp / 'tasks'
    task_system.TASKS_DIR = task_system.TASK_DIR

    try:
        # ── 1. 创建任务 + 创建 worktree 并绑定 ──
        task = task_system.create_task('wt task', 'work in isolation')
        out = worktree.create_worktree('alpha', task_id=task.id)
        assert 'alpha' in out, out
        assert (worktree.WORKTREES_DIR / 'alpha').is_dir(), 'worktree 目录应存在'
        # 分支 + git worktree list 都应有 alpha
        assert 'alpha' in _git(tmp, 'worktree', 'list').stdout, 'git worktree list 应含 alpha'
        # 任务已绑定
        assert task_system.load_task(task.id).worktree == 'alpha', '创建时应绑定任务'

        # ── 2. 有未提交改动时删除被拒绝 ──
        (worktree.WORKTREES_DIR / 'alpha' / 'wip.txt').write_text('wip', encoding='utf-8')
        out = worktree.remove_worktree('alpha')
        assert 'uncommitted' in out, f'未提交改动应阻止删除: {out}'
        assert (worktree.WORKTREES_DIR / 'alpha').is_dir(), '拒绝删除后目录应保留'

        # ── 3. discard_changes=true 强制删除 ──
        out = worktree.remove_worktree('alpha', discard_changes=True)
        assert 'removed' in out, out
        assert not (worktree.WORKTREES_DIR / 'alpha').exists(), '强制删除后目录应消失'
        assert 'alpha' not in _git(tmp, 'worktree', 'list').stdout

        # ── 4. 保留流程（记录 keep 事件） ──
        worktree.create_worktree('beta')
        out = worktree.keep_worktree('beta')
        assert 'kept' in out, out
        events = worktree.WORKTREES_DIR / 'events.jsonl'
        assert events.exists(), '事件日志应落盘'
        types = [json.loads(l)['type'] for l in
                 events.read_text(encoding='utf-8').splitlines() if l.strip()]
        assert 'create' in types and 'keep' in types, f'事件应含 create/keep: {types}'
    finally:
        worktree.WORKDIR = orig_wd
        worktree.WORKTREES_DIR = orig_wt
        task_system.TASK_DIR = orig_td
        task_system.TASKS_DIR = orig_td
        shutil.rmtree(tmp, ignore_errors=True)
