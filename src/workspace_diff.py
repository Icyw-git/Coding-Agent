"""工作区只读视图：逐行统一 diff 与回合改动清单。

只调用 `git`，不写索引、不改工作区。所有入口都返回可直接打印的文本；
调用方（CLI `/diff`、回合结束汇总）不需要再猜测输出结构。
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

MAX_DIFF_CHARS = 30_000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES = 50
GIT_TIMEOUT = 15
# harness 自身写入的工作区内目录：会话/快照/转录不是用户的改动，`/diff` 和
# 回合清单都不该被它们淹没。调用方可以传入自己的 ignore（harness 用
# CHECKPOINT_IGNORE，与回溯的忽略口径保持一致）。
DEFAULT_IGNORE = ('.git', '.cache', '.worktrees', '.mailbox',
                  '__pycache__', '.pytest_cache')


def _is_ignored(path: str, ignore) -> bool:
    parts = Path(path.replace('\\', '/')).parts
    return any(part in ignore for part in parts)


def _git(args: list[str], workspace: Path, *, timeout: float = GIT_TIMEOUT):
    try:
        return subprocess.run(
            # --no-optional-locks：status 默认可能刷新并写回 .git/index；
            # 这是只读视图，不跟用户/其他进程抢索引锁。
            ['git', '--no-optional-locks', *args], cwd=str(workspace),
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def repo_root(workspace: Path) -> Path | None:
    """返回仓库根；不是 Git 仓库或 git 不可用时返回 None。"""
    proc = _git(['rev-parse', '--show-toplevel'], workspace)
    if proc is None or proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return Path(root) if root else None


def _split_porcelain(data: str) -> list[tuple[str, str, str | None]]:
    """解析 `git status --porcelain=v1 -z`，返回 (XY, 新路径, 旧路径)。

    -z 下重命名/复制为 `R  new\\0old\\0`；用 NUL 切分后按状态位判断是否
    还要消费一段旧路径，避免把旧路径误当成下一条记录。
    """
    parts = data.split('\0')
    rows: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(parts):
        raw = parts[index]
        index += 1
        if not raw:
            continue
        code = raw[:2]
        path = raw[3:] if len(raw) > 3 else ''
        old = None
        if code and (code[0] in 'RC' or code[1] in 'RC'):
            if index < len(parts):
                old = parts[index]
                index += 1
        if path:
            rows.append((code, path, old))
    return rows


def _status(workspace: Path, ignore=DEFAULT_IGNORE) -> list[dict] | None:
    """工作区内的改动条目；路径统一为相对 workspace 的显示路径。"""
    root = repo_root(workspace)
    if root is None:
        return None
    proc = _git(['status', '--porcelain=v1', '-z', '--untracked-files=all'],
                workspace)
    if proc is None or proc.returncode != 0:
        return None
    entries = []
    for code, path, old in _split_porcelain(proc.stdout):
        if _is_ignored(path, ignore):
            continue
        absolute = (root / path).resolve()
        try:
            display = absolute.relative_to(workspace.resolve())
        except ValueError:
            continue   # 工作区只是仓库子目录：不展示目录外的改动
        old_display = None
        if old:
            try:
                old_display = str((root / old).resolve().relative_to(
                    workspace.resolve()))
            except ValueError:
                old_display = old
        entries.append({
            'code': code.strip() or '?',
            'path': str(display).replace('\\', '/'),
            'old': old_display.replace('\\', '/') if old_display else None,
            'abs': absolute,
        })
    return entries


def _signature(path: Path) -> tuple:
    try:
        stat = path.stat()
    except OSError:
        return (False, 0, 0, '')
    digest = ''
    if path.is_file() and stat.st_size <= MAX_FILE_BYTES:
        hasher = hashlib.sha1()
        try:
            with open(path, 'rb') as handle:
                for chunk in iter(lambda: handle.read(262_144), b''):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
        except OSError:
            digest = ''
    # 有内容 hash 时只看内容：`touch` / 等价重写不该算「本轮改过」。
    # 超大或读不了的文件退回 mtime（此时 hash 不可用）。
    return (True, stat.st_size, 0 if digest else stat.st_mtime_ns, digest)


def snapshot(workspace: Path, ignore=DEFAULT_IGNORE) -> dict[str, tuple] | None:
    """记录回合开始时的改动签名；不是 Git 仓库返回 None。

    只记录「当前已经脏/未跟踪」的文件。回合结束后同一路径的状态或内容
    签名有任何变化，就算本轮动过；本来就脏、本轮没碰的文件不会误报。
    """
    entries = _status(workspace, ignore)
    if entries is None:
        return None
    return {entry['path']: (entry['code'], _signature(entry['abs']))
            for entry in entries}


def changed_since(workspace: Path, baseline: dict[str, tuple],
                  ignore=DEFAULT_IGNORE) -> list[dict] | None:
    """相对基线发生变化的文件；非 Git 仓库返回 None。"""
    entries = _status(workspace, ignore)
    if entries is None:
        return None
    changes = []
    for entry in entries:
        before = baseline.get(entry['path'])
        after = (entry['code'], _signature(entry['abs']))
        if before != after:
            changes.append(entry)
    return changes


def pending_changes(workspace: Path, ignore=DEFAULT_IGNORE) -> list[dict] | None:
    """未提交条目；不是 Git 仓库时返回 None（供 /review 等调用方判定）。"""
    return _status(workspace, ignore)


def _colorize(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.startswith('diff --git') or line.startswith('index '):
            lines.append(f'\033[36m{line}\033[0m')
        elif line.startswith('@@'):
            lines.append(f'\033[36m{line}\033[0m')
        elif line.startswith('+++') or line.startswith('---'):
            lines.append(f'\033[1m{line}\033[0m')
        elif line.startswith('+'):
            lines.append(f'\033[32m{line}\033[0m')
        elif line.startswith('-'):
            lines.append(f'\033[31m{line}\033[0m')
        else:
            lines.append(line)
    return '\n'.join(lines)


def _file_diff(workspace: Path, entry: dict, budget: int) -> str:
    if entry['code'] == '??':
        proc = _git(['diff', '--no-index', '--no-color', '--no-textconv',
                     '--unified=3', '--', '/dev/null', entry['path']], workspace)
        # --no-index 有差异时返回 1，0 表示没有输出；其他返回码当失败。
        if proc is None or proc.returncode not in (0, 1):
            return ''
        text = proc.stdout
    else:
        proc = _git(['diff', 'HEAD', '--no-ext-diff', '--no-textconv',
                     '--no-color', '--unified=3', '--', entry['path']],
                    workspace)
        if proc is None or proc.returncode != 0:
            # 尚未产生 HEAD 的仓库：已暂存文件用 --cached 对空树比较
            proc = _git(['diff', '--cached', '--no-ext-diff', '--no-textconv',
                         '--no-color', '--unified=3', '--', entry['path']],
                        workspace)
        if proc is None or proc.returncode != 0:
            return ''
        text = proc.stdout
    if len(text) > budget:
        text = text[:budget] + f'\n… （该文件 diff 已截断，剩余 {len(text) - budget} 字符）\n'
    return text


def format_entries(workspace: Path, entries: list[dict],
                   *, max_chars: int = MAX_DIFF_CHARS,
                   color: bool = False) -> str:
    """渲染给定条目（状态 + 逐行 diff）；空列表返回空串。

    /review 需要先剔除敏感文件，再复用同一份渲染逻辑，避免两处格式漂移。
    """
    if not entries:
        return ''
    workspace = Path(workspace)
    status_lines = ['  文件状态：']
    for entry in entries:
        rename = f"{entry['old']} → {entry['path']}" if entry.get('old') else entry['path']
        status_lines.append(f"    {entry['code']:<2} {rename}")

    chunks: list[str] = []
    used = 0
    truncated = False
    for entry in entries:
        remaining = max_chars - used
        if remaining <= 200:
            truncated = True
            break
        text = _file_diff(workspace, entry, remaining)
        if not text.strip():
            continue
        chunks.append(text.rstrip('\n'))
        used += len(text)
    if truncated:
        chunks.append(f'  （diff 过长，已截断；共 {len(entries)} 个文件）')

    if not chunks:
        body = '\n'.join(status_lines) + '\n  已跟踪文件没有可显示的行级差异。'
    else:
        body = '\n\n'.join(status_lines + ['', '  逐行改动：'] + chunks)
    return _colorize(body) if color else body


def format_diff(workspace: Path, path: str = '',
                *, max_chars: int = MAX_DIFF_CHARS, color: bool = False,
                ignore=DEFAULT_IGNORE) -> str:
    """IDE 风格的逐行比较：文件状态 + 每个文件的统一 diff。"""
    workspace = Path(workspace)
    entries = pending_changes(workspace, ignore)
    if entries is None:
        return '  当前工作区不是 Git 仓库，无法显示 diff。'
    scope = (path or '').strip().replace('\\', '/').rstrip('/')
    if scope and scope != '.':
        entries = [entry for entry in entries
                   if entry['path'] == scope
                   or entry['path'].startswith(scope + '/')]
    if not entries:
        if scope and scope != '.':
            return f'  工作区干净，{scope} 没有未提交改动。'
        return '  工作区干净，没有未提交改动。'
    return format_entries(workspace, entries, max_chars=max_chars, color=color)


def turn_changes(workspace: Path, baseline: dict | None,
                 fallback: dict[str, str] | None = None,
                 ignore=DEFAULT_IGNORE) -> list[dict]:
    """回合改动条目；Git 基线不可用时回退到 checkpoint 记录。"""
    changes = changed_since(workspace, baseline, ignore) if baseline is not None else None
    if changes is None:
        changes = [{'code': status or 'M', 'path': name, 'old': None,
                    'abs': Path(workspace) / name}
                   for name, status in sorted((fallback or {}).items())
                   if not _is_ignored(name, ignore)]
    return changes


def format_changes(changes: list[dict], *, max_files: int = MAX_FILES) -> str:
    if not changes:
        return ''
    shown = changes[:max_files]
    lines = [f'  [改动] 本轮修改 {len(changes)} 个文件']
    for entry in shown:
        rename = f"{entry['old']} → {entry['path']}" if entry.get('old') else entry['path']
        lines.append(f"    {entry['code']:<2} {rename}")
    if len(changes) > len(shown):
        lines.append(f'    … 另有 {len(changes) - len(shown)} 个文件')
    lines.append('    /diff 查看逐行改动')
    return '\n'.join(lines)


def format_turn_summary(workspace: Path, baseline: dict | None,
                        fallback: dict[str, str] | None = None,
                        *, max_files: int = MAX_FILES,
                        ignore=DEFAULT_IGNORE) -> str:
    """回合结束时的改动文件清单；无改动返回空串。

    Git 可用时以基线比较为准（能看见 bash 造成的改动）；否则回退到
    checkpoint 记录的 write/edit/apply_patch 文件。
    """
    return format_changes(
        turn_changes(workspace, baseline, fallback, ignore),
        max_files=max_files)
