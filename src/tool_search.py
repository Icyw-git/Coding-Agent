"""只读搜索工具（glob / grep / ls）：路径边界、秘密文件过滤与 ripgrep 快路径。

路径解析（`resolve`）与无头审批门（`gate`）由 tools.py 注入；本模块不读取
工作区全局状态，可脱离工具注册表单独测试。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from fnmatch import fnmatchcase
from pathlib import Path

import checkpoints
from exec_policy import is_secret_path
from tool_text import BinaryFileError, decode_text

MAX_SEARCH_BYTES = 2 * 1024 * 1024
SEARCH_IGNORE = frozenset(checkpoints.DEFAULT_IGNORE) | {
    'node_modules', '.venv', 'venv', '.tox', '.mypy_cache'}
RG_TIMEOUT = 30.0
RG_GLOB_IGNORES = tuple(sorted(SEARCH_IGNORE))


def search_root(path: str, cwd: str | None, *, gate, resolve):
    """只读搜索工具的根路径：显式秘密路径先拒，其余与 read 共用同一解析。"""
    denied = gate('read', path, cwd)
    if denied:
        return None, denied
    try:
        return resolve(path, cwd), None
    except Exception as exc:
        return None, f'Error: {exc}'


def _secret_file(path: Path) -> bool:
    """按最终目标判断：名为 notes.txt 的链接指向 .env 时，同样不能搜内容。"""
    if is_secret_path(path, checkpoints.DEFAULT_SECRETS):
        return True
    if path.is_symlink():
        try:
            return is_secret_path(path.resolve(), checkpoints.DEFAULT_SECRETS)
        except OSError:
            return True
    return False


def _search_files(root: Path):
    """遍历搜索根；跳过忽略目录、指向目录的链接与秘密文件。"""
    if root.is_file():
        if not _secret_file(root):
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SEARCH_IGNORE
            and not (Path(dirpath) / name).is_symlink())
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            if candidate.is_file() and not _secret_file(candidate):
                yield candidate


def _match_search_glob(path: Path, root: Path, pattern: str | None) -> bool:
    if not pattern:
        return True
    if '/' in pattern or '\\' in pattern:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        return fnmatchcase(rel, pattern.replace('\\', '/'))
    return fnmatchcase(path.name, pattern)


def _rg_binary() -> str | None:
    return shutil.which('rg')


def _grep_with_ripgrep(pattern: str, root: Path, glob: str | None,
                       case_insensitive: bool, limit: int, *,
                       rg_binary=None) -> str | None:
    """用 ripgrep 搜目录；不可用时返回 None，交给 Python 回退。

    Python 正则与 rust-regex 语义不同、GBK 需要逐文件探测编码，所以
    非 ASCII 模式直接走回退；rg 输出出现替换符也视为编码不适配。
    `rg_binary` 由 tools 注入，便于测试在工具层替换查找函数。
    """
    if not pattern.isascii():
        return None
    binary = (rg_binary or _rg_binary)()
    if not binary:
        return None
    command = [
        binary, '--no-heading', '--line-number', '--color', 'never',
        '-H', '--hidden', '--no-ignore', '--no-messages',
        '--max-columns', '1000', '--max-columns-preview',
        '--regexp', pattern,
    ]
    if case_insensitive:
        command.append('-i')
    for name in RG_GLOB_IGNORES:
        command += ['--glob', f'!{name}/**']
    for secret in checkpoints.DEFAULT_SECRETS:
        command += ['--glob', f'!{secret}']
    command += ['--', '.']
    timed_out = []

    def stop():
        timed_out.append(True)
        try:
            proc.kill()
        except OSError:
            pass

    try:
        proc = subprocess.Popen(
            command, cwd=str(root), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
            errors='replace')
    except OSError:
        return None
    timer = threading.Timer(RG_TIMEOUT, stop)
    timer.start()
    hits = []
    fallback = False
    truncated = False
    try:
        for raw in proc.stdout:
            line = raw.rstrip('\r\n')
            if not line:
                continue
            rel, _, rest = line.partition(':')
            lineno, _, text = rest.partition(':')
            rel = rel.replace('\\', '/')
            if rel.startswith('./'):
                rel = rel[2:]
            candidate = root / rel
            if not lineno.isdigit():
                fallback = True
                break
            try:
                if candidate.stat().st_size > MAX_SEARCH_BYTES:
                    continue
            except OSError:
                continue
            if _secret_file(candidate):
                continue
            if not _match_search_glob(candidate, root, glob):
                continue
            if '\ufffd' in text:
                fallback = True
                break
            if len(text) > 400:
                text = text[:400] + '…'
            hits.append(f'{rel}:{lineno}: {text}')
            if len(hits) >= limit:
                truncated = True
                break
    finally:
        timer.cancel()
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    if fallback:
        return None
    if timed_out:
        note = f'\n(search timed out after {RG_TIMEOUT:g}s)'
        return ('\n'.join(hits) + note) if hits else '(no matches)' + note
    if truncated:
        return '\n'.join(hits) + f'\n(truncated at {limit} matches)'
    if proc.returncode not in (0, 1) and not hits:
        return None
    return '\n'.join(hits) if hits else "(no matches)"


def glob_files(pattern: str, path: str | None, cwd: str | None, *,
               gate, resolve) -> str:
    root, error = search_root(path or '.', cwd, gate=gate, resolve=resolve)
    if error:
        return error
    try:
        if not root.exists():
            return f'Error: path not found: {path}'
        matches = [p for p in root.glob(pattern) if p.is_file()]
        results = sorted(str(p.relative_to(root)) for p in matches)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def grep_files(pattern: str, path: str, glob: str | None, *,
               gate, resolve, case_insensitive: bool = False,
               max_results: int = 100, cwd: str | None = None,
               backend=None) -> str:
    """按正则搜索文件内容，返回 `path:line: text`。

    只读工具：根路径走 read 的分区判定；目录遍历跳过秘密文件与常见忽略目录。
    二进制与超过 MAX_SEARCH_BYTES 的文件不搜，避免把一次检索变成整盘扫描。
    目录搜索优先走 ripgrep；不可用或模式含非 ASCII 时回退到 Python 逐文件解码。
    """
    backend = backend or _grep_with_ripgrep
    try:
        regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as exc:
        return f'Error: invalid pattern: {exc}'
    root, error = search_root(path, cwd, gate=gate, resolve=resolve)
    if error:
        return error
    if not root.exists():
        return f'Error: path not found: {path}'
    limit = max(1, min(int(max_results), 1000))
    if root.is_dir():
        fast = backend(pattern, root, glob, case_insensitive, limit)
        if fast is not None:
            return fast
    hits: list[str] = []
    try:
        for candidate in _search_files(root):
            if not _match_search_glob(candidate, root, glob):
                continue
            try:
                if candidate.stat().st_size > MAX_SEARCH_BYTES:
                    continue
                data = candidate.read_bytes()
            except OSError:
                continue
            try:
                text, _encoding = decode_text(data)
            except BinaryFileError:
                continue
            try:
                display = str(candidate.relative_to(root)) \
                    if root.is_dir() else str(root)
            except ValueError:
                display = str(candidate)
            for lineno, line in enumerate(text.splitlines(), 1):
                if not regex.search(line):
                    continue
                if len(line) > 400:
                    line = line[:400] + '…'
                hits.append(f'{display}:{lineno}: {line}')
                if len(hits) >= limit:
                    return '\n'.join(hits) + f'\n(truncated at {limit} matches)'
    except Exception as e:
        return f'Error: {e}'
    return '\n'.join(hits) if hits else "(no matches)"


def ls_dir(path: str, show_all: bool, cwd: str | None, *,
           gate, resolve) -> str:
    """列目录（只读）。隐藏项需要 show_all；路径可以在工作区外。"""
    root, error = search_root(path, cwd, gate=gate, resolve=resolve)
    if error:
        return error
    try:
        if not root.exists():
            return f'Error: path not found: {path}'
        if root.is_file():
            return f'f  {root.name}  {root.stat().st_size} B'
        if not root.is_dir():
            return f'Error: not a file or directory: {path}'
        rows = []
        for item in sorted(root.iterdir(),
                           key=lambda p: (not p.is_dir(), p.name.lower())):
            if not show_all and item.name.startswith('.'):
                continue
            try:
                if item.is_dir():
                    rows.append(f'd  {item.name}/')
                else:
                    rows.append(f'f  {item.name}  {item.stat().st_size} B')
            except OSError:
                rows.append(f'?  {item.name}')
        return "\n".join(rows) if rows else "(empty)"
    except Exception as e:
        return f"Error: {e}"
