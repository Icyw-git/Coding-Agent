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

VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$') # 有效工作目录名称正则表达式，1-64个字母、数字、下划线、点、短横线


def validate_worktree_name(name: str): # 验证工作目录名称函数，返回错误信息或 None
    """验证工作目录名称是否符合要求。"""
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
                           capture_output=True, text=True, timeout=30) #运行 Git 命令
        out = (r.stdout + r.stderr).strip()
        out = out[:5000] if out else '(no output)'
        return r.returncode == 0, out # 返回是否成功和输出结果
    except subprocess.TimeoutExpired:
        return False, "Error: git timeout"


def log_event(event_type: str, worktree_name: str, task_id: str = ''): # 记录事件函数，将事件写入 JSONL 文件
    """记录事件，将事件写入 JSONL 文件。"""
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
    if err: # 如果工作目录名称无效
        return f'Error:{err}'
    path = WORKTREES_DIR / name
    if path.exists():
        return f'Worktree {name} already exists at {path}'
    ok, result = run_git(['worktree', 'add', str(path), '-b', f'wt-{name}', 'HEAD']) #执行 Git worktree add 命令
    if not ok:
        return f'Git error:{result}'
    if task_id: #如果id 有效，绑定任务到工作目录
        bind_task_to_worktree(task_id, name)
    log_event('create', name, task_id)
    print(f"  \033[33m[worktree] created: {name} at {path}\033[0m")
    return f'Worktree {name} create at {path}'


def bind_task_to_worktree(task_id: str, worktree_name: str): # 绑定任务到工作目录函数
    """绑定任务到工作目录。"""
    task = load_task(task_id)
    task.worktree = worktree_name
    save_task(task)
    print(f"  \033[33m[bind] {task.subject} → worktree:{worktree_name}\033[0m")


def _count_worktree_changes(path: Path) -> tuple:
    try:
        r1 = subprocess.run(['git', 'status', '--porcelain'], cwd=path,
                            capture_output=True, text=True, timeout=10) #运行 Git status 命令
        files = len([l for l in r1.stdout.strip().splitlines() if l.strip()]) #统计未提交文件数
        r2 = subprocess.run(['git', 'log', '@{push}..HEAD', '--oneline'], cwd=path,
                            capture_output=True, text=True, timeout=10) #运行 Git log 命令
        commits = len([l for l in r2.stdout.strip().splitlines() if l.strip()]) #统计未推送提交数
        return files, commits #返回未提交文件数和未推送提交数
    except Exception:
        return -1, -1


def remove_worktree(name: str, discard_changes: bool = False) -> str: # 删除工作目录函数
    err = validate_worktree_name(name)
    if err:
        return err
    path = WORKTREES_DIR / name
    if not path.exists():
        return f'Worktree {name} not found'
    if not discard_changes: # 如果不强制删除，检查是否有未提交或未推送的变更
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
    run_git(['branch', '-D', f'wt-{name}']) #删除工作目录分支
    log_event('remove', name) #记录删除事件
    print(f"  \033[33m[worktree] removed: {name}\033[0m")
    return f"Worktree {name} removed"


def keep_worktree(name: str) -> str: # 保留工作目录函数，用于后续审核
    """保留工作目录，用于后续审核。"""
    err = validate_worktree_name(name)
    if err:
        return err
    log_event('keep', name)
    print(f"  \033[36m[worktree] kept: {name}\033[0m")
    return f'Worktree {name} kept for review (branch: wt-{name})'
