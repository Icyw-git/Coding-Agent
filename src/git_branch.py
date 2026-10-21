"""读取工作区的当前 git 分支，供提示符显示。

直接读 `.git/HEAD`，不启动 git 子进程：提示符每次输入前都会求值，几十毫秒的
进程启动会变成明显的输入延迟。支持 `.git` 是文件（worktree / submodule）。
"""
from __future__ import annotations

from pathlib import Path


def _git_dir(root: Path) -> Path | None:
    dotgit = Path(root) / '.git'
    if dotgit.is_dir():
        return dotgit
    if dotgit.is_file():
        try:
            text = dotgit.read_text(encoding='utf-8', errors='replace').strip()
        except OSError:
            return None
        if text.startswith('gitdir:'):
            target = Path(text[len('gitdir:'):].strip())
            return target if target.is_absolute() else (Path(root) / target).resolve()
    return None


def current_branch(root) -> str | None:
    """返回分支名或分离 HEAD 的短哈希；非 git 工作区返回 None。"""
    gitdir = _git_dir(Path(root))
    if gitdir is None:
        return None
    try:
        head = (gitdir / 'HEAD').read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return None
    if head.startswith('ref:'):
        ref = head[len('ref:'):].strip()
        return ref.rsplit('/', 1)[-1] or None
    return head[:8] or None
