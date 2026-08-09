"""Git worktree 隔离：每个任务一个独立工作目录，teammate 在其内干活。"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from task_system import load_task, save_task

WORKDIR = Path(os.getcwd()).resolve()
WORKTREES_DIR = WORKDIR / '.worktrees'
WORKTREES_DIR.mkdir(exist_ok=True)

VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


def validate_worktree_name(name: str):
    if not name:
        return 'Worktree name cannot be empty'
    if name == '.' or name == '..':
        return f"{name} is not a valid worktree name"
    if not VALID_WT_NAME.match(name):
        return (f"Invalid worktree name '{name}': "
                "only letters, digits, dots, underscores, dashes (1-64 chars)")
    return None


def run_git(args: list) -> tuple:
    try:
        r = subprocess.run(['git'] + args, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).strip()
        out = out[:5000] if out else '(no output)'
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "Error: git timeout"


def log_event(event_type: str, worktree_name: str, task_id: str = ''):
    event = {
        'type': event_type,
        'worktree': worktree_name,
        'task_id': task_id,
        'ts': time.time(),
    }
    events_file = WORKTREES_DIR / "events.jsonl"
    with open(events_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(event, ensure_ascii=False) + '\n')


def create_worktree(name: str, task_id: str = '') -> str:
    err = validate_worktree_name(name)
    if err:
        return f'Error:{err}'
    path = WORKTREES_DIR / name
    if path.exists():
        return f'Worktree {name} already exists at {path}'
    ok, result = run_git(['worktree', 'add', str(path), '-b', f'wt-{name}', 'HEAD'])
    if not ok:
        return f'Git error:{result}'
    if task_id:
        bind_task_to_worktree(task_id, name)
    log_event('create', name, task_id)
    print(f"  \033[33m[worktree] created: {name} at {path}\033[0m")
    return f'Worktree {name} create at {path}'


def bind_task_to_worktree(task_id: str, worktree_name: str):
    task = load_task(task_id)
    task.worktree = worktree_name
    save_task(task)
    print(f"  \033[33m[bind] {task.subject} → worktree:{worktree_name}\033[0m")


def _count_worktree_changes(path: Path) -> tuple:
    try:
        r1 = subprocess.run(['git', 'status', '--porcelain'], cwd=path,
                            capture_output=True, text=True, timeout=10)
        files = len([l for l in r1.stdout.strip().splitlines() if l.strip()])
        r2 = subprocess.run(['git', 'log', '@{push}..HEAD', '--oneline'], cwd=path,
                            capture_output=True, text=True, timeout=10)
        commits = len([l for l in r2.stdout.strip().splitlines() if l.strip()])
        return files, commits
    except Exception:
        return -1, -1


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    err = validate_worktree_name(name)
    if err:
        return err
    path = WORKTREES_DIR / name
    if not path.exists():
        return f'Worktree {name} not found'
    if not discard_changes:
        files, commits = _count_worktree_changes(path)
        if files < 0:
            return (f"Cannot verify worktree '{name}' status. "
                    "Use discard_changes=true to force removal.")
        if files > 0 or commits > 0:
            return (f"Worktree '{name}' has {files} uncommitted file(s) "
                    f"and {commits} unpushed commit(s). "
                    "Use discard_changes=true to force removal, "
                    "or keep_worktree to preserve for review.")

    ok1, _ = run_git(['worktree', 'remove', str(path), '--force'])
    if not ok1:
        return f'Failed to remove worktree directory for {name}'
    run_git(['branch', '-D', f'wt-{name}'])
    log_event('remove', name)
    print(f"  \033[33m[worktree] removed: {name}\033[0m")
    return f"Worktree {name} removed"


def keep_worktree(name: str) -> str:
    err = validate_worktree_name(name)
    if err:
        return err
    log_event('keep', name)
    print(f"  \033[36m[worktree] kept: {name}\033[0m")
    return f'Worktree {name} kept for review (branch: wt-{name})'
