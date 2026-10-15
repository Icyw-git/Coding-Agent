"""Execution-local workspace, shared by the runtime and tool adapters."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


_workspace = ContextVar('workspace', default=None)


def current_workspace(default: Path) -> Path:
    return _workspace.get() or default


@contextmanager
def use_workspace(path: Path):
    token = _workspace.set(path)
    try:
        yield
    finally:
        _workspace.reset(token)


def resolve_workspace(path, *, base: Path, allowed_root: Path | None = None) -> Path:
    value = str(path).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    if not value.strip():
        raise ValueError('Workspace path must not be empty')
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    candidate = candidate.resolve()
    if not candidate.is_dir():
        raise ValueError(f'Workspace does not exist: {candidate}')
    if allowed_root is not None and not candidate.is_relative_to(allowed_root):
        raise ValueError(f'Workspace is outside the allowed root: {allowed_root}')
    return candidate
