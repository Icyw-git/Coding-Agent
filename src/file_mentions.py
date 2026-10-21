"""`@path` 文件提及：工作区内相对路径的索引、匹配与光标解析。

只做文本补全，不把文件内容附加进消息：模型要看内容仍走 read 工具，
权限、编码、截断与敏感文件规则保持一致。
"""
from __future__ import annotations

import os
from pathlib import Path

SKIP_DIRS = frozenset({
    '.git', '.cache', '.venv', 'venv', 'node_modules', '__pycache__',
    '.pytest_cache', '.ruff_cache', '.mypy_cache', '.idea', '.vscode',
})
MAX_FILES = 8000
MAX_RESULTS = 50


def workspace_files(root, limit: int = MAX_FILES) -> list[str]:
    """工作区文件相对路径（POSIX 分隔）；跳过隐藏项与依赖/构建目录。"""
    root = Path(root)
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIP_DIRS and not name.startswith('.'))
        for name in sorted(filenames):
            if name.startswith('.'):
                continue
            try:
                files.append((Path(dirpath) / name).relative_to(root).as_posix())
            except ValueError:
                continue
            if len(files) >= limit:
                return files
    return files


def match_mentions(files: list[str], query: str,
                   limit: int = MAX_RESULTS) -> list[str]:
    """文件名前缀优先，其次路径包含；同组内保持索引顺序。"""
    needle = query.casefold()
    if not needle:
        return files[:limit]
    basename_hits = [path for path in files
                     if path.rsplit('/', 1)[-1].casefold().startswith(needle)]
    rest = [path for path in files
            if path not in basename_hits and needle in path.casefold()]
    return (basename_hits + rest)[:limit]


def mention_token(text: str) -> tuple[int, str] | None:
    """光标前最后一个 `@token`：返回 (`@` 的索引, 查询串)；不匹配时返回 None。

    只在行首或空白/左括号之后识别，避免把邮箱 `a@b` 当成提及。
    """
    at = text.rfind('@')
    if at == -1:
        return None
    if at > 0 and not text[at - 1].isspace() and text[at - 1] not in '([{':
        return None
    token = text[at + 1:]
    if any(char.isspace() for char in token):
        return None
    return at, token


def format_mention(path: str) -> str:
    """插入到输入框的文本；含空格时用双引号包住。"""
    return f'@"{path}"' if any(char.isspace() for char in path) else f'@{path}'
