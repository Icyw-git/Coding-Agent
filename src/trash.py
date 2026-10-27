"""可撤销删除：把「删掉」换成「挪进回收站」，让误删有救。

## 为什么值得单独一层

`bash` 里的 `del` / `rm` 是**不可逆**的。这是个人助手里唯一会造成真实数据损失的
操作：模型看错目标、路径拼错、正则多匹配一个文件，用户就没有第二次机会。

回收站是操作系统本来就提供的安全网，没有理由不用。

## 两条路径

- **Windows**：`SHFileOperationW` + `FOF_ALLOWUNDO`，走系统回收站。不引入依赖
  （`ctypes` 直接调 shell32）。带 `FOF_NOCONFIRMATION`，否则每个文件弹一个确认框
  会把非交互会话卡死。
- **其他平台**：`trash` CLI（`trash-put` / `gio trash`）。找不到就**明确失败**，
  而不是悄悄退回 `unlink`——静默降级等于把安全网换成了陷阱。

## 边界

- 只接受**已存在**的路径；不解析通配符（调用方先展开），避免「以为删一个文件，
  实际删了一个 glob」。
- 拒绝文件系统根与盘符根：这些进不了回收站，也不该被删。
- 返回值区分「删掉了」与「挪进回收站了」，让调用方和审计能如实描述。
- 这是**能力层**：能不能删由策略层（`exec_policy`）决定，本模块只回答「怎么删才
  可撤销」。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 20.0
# FO_DELETE | FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
_OPFLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI


class TrashError(RuntimeError):
    """删除失败或路径不可回收。"""


@dataclass(frozen=True)
class Trashed:
    path: str
    method: str          # 'recycle-bin' | 'trash-cli'

    @property
    def recoverable(self) -> bool:
        return True

    def describe(self) -> str:
        if self.method == 'recycle-bin':
            return f'{self.path} → Recycle Bin (restorable)'
        return f'{self.path} → system trash (restorable)'


def available() -> tuple[bool, str]:
    """当前平台能否回收站式删除；返回 (可用, 方法名)。"""
    if os.name == 'nt':
        try:
            import ctypes  # noqa: F401

            return True, 'recycle-bin'
        except Exception:
            return False, ''
    if _trash_cli() is not None:
        return True, 'trash-cli'
    return False, ''


def _trash_cli() -> str | None:
    for name in ('trash-put', 'trash', 'gio'):
        found = shutil.which(name)
        if found:
            return found
    return None


def _validate(path) -> Path:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise TrashError(f'cannot resolve path: {exc}') from exc
    if not resolved.exists() and not resolved.is_symlink():
        raise TrashError(f'path does not exist: {resolved}')
    # 根目录进不了回收站（Windows 会静默失败），也不该被删。
    if resolved.parent == resolved:
        raise TrashError('refusing to trash a filesystem root')
    if resolved.anchor and resolved == Path(resolved.anchor):
        raise TrashError('refusing to trash a drive root')
    return resolved


def _trash_windows(paths: list[Path]) -> None:
    """SHFileOperationW：路径必须是双 NUL 结尾的宽字符串列表。"""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ('hwnd', wintypes.HWND),
            ('wFunc', wintypes.UINT),
            ('pFrom', wintypes.LPCWSTR),
            ('pTo', wintypes.LPCWSTR),
            ('fFlags', ctypes.c_ushort),
            ('fAnyOperationsAborted', wintypes.BOOL),
            ('hNameMappings', ctypes.c_void_p),
            ('lpszProgressTitle', wintypes.LPCWSTR),
        ]

    # 每项以 \0 结尾，整个列表再补一个 \0
    joined = ''.join(f'{path}\0' for path in paths) + '\0'
    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = joined
    operation.pTo = None
    operation.fFlags = _OPFLAGS
    operation.fAnyOperationsAborted = False
    operation.hNameMappings = None
    operation.lpszProgressTitle = None

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0:
        raise TrashError(_shfileerror(result))
    if operation.fAnyOperationsAborted:
        raise TrashError('the operation was cancelled before completing')


_ERROR_TEXT = {
    0x71: 'the source and destination are the same file',
    0x74: 'the path is too long',
    0x7C: 'the path is invalid',
    0x2E: 'the file is in use by another process',
    0x5: 'access denied',
    0x20: 'a sharing violation occurred (file in use)',
    0x78: 'security settings prevented the operation',
    0x402: 'the item cannot be moved to the Recycle Bin',
}


def _shfileerror(code: int) -> str:
    hint = _ERROR_TEXT.get(code)
    base = f'SHFileOperation failed (code 0x{code:X})'
    return f'{base}: {hint}' if hint else base


def _trash_cli_delete(paths: list[Path], cli: str, timeout: float) -> None:
    if Path(cli).name.casefold().startswith('gio'):
        command = [cli, 'trash', '--', *[str(p) for p in paths]]
    else:
        command = [cli, '--', *[str(p) for p in paths]]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TrashError(f'trash command failed: {exc}') from exc
    if result.returncode != 0:
        stderr = (result.stderr or b'').decode('utf-8', 'replace').strip()
        raise TrashError(f'trash command failed: {stderr[-300:] or result.returncode}')


def trash(paths, *, timeout: float = DEFAULT_TIMEOUT) -> list[Trashed]:
    """把若干路径挪进回收站；任何一个失败都不静默吞掉。

    多个路径一次调用（Windows 上 SHFileOperationW 支持列表），比逐个删快，
    也让「部分成功」这种中间态没有机会出现。
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    items = list(paths)
    if not items:
        return []
    resolved = [_validate(item) for item in items]

    usable, method = available()
    if not usable:
        raise TrashError(
            'recycle bin is unavailable on this platform; install `trash-cli` '
            'or delete the file yourself (this tool will not do an '
            'unrecoverable delete)')

    if method == 'recycle-bin':
        _trash_windows(resolved)
    else:
        _trash_cli_delete(resolved, _trash_cli(), timeout)
    return [Trashed(path=str(path), method=method) for path in resolved]


def describe_status() -> str:
    usable, method = available()
    if not usable:
        return '可撤销删除：不可用（缺回收站/trash CLI）'
    label = 'Windows 回收站' if method == 'recycle-bin' else '系统 trash'
    return f'可撤销删除：可用（{label}）'
