"""Execution-local workspace, shared by the runtime and tool adapters."""

import os
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


def workspace_roots(primary=None, extra=None) -> list[Path]:
    """Startup-authorized roots: primary + HARNESS_WORKSPACE_ROOTS.

    ``extra`` defaults to the environment variable (os.pathsep separated).
    Reading it here keeps every adapter (API, tasks, runtime) on one list.
    """
    values = []
    if primary:
        values.append(str(primary))
    if extra is None:
        extra = os.getenv('HARNESS_WORKSPACE_ROOTS', '')
    if isinstance(extra, str):
        values.extend(part for part in extra.split(os.pathsep) if part.strip())
    else:
        values.extend(str(part) for part in extra if str(part).strip())
    roots = []
    seen = set()
    for value in values:
        candidate = Path(value).expanduser().resolve()
        key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            roots.append(candidate)
    return roots


def is_within_roots(path, roots) -> bool:
    if not roots:
        return True
    try:
        candidate = Path(path).expanduser().resolve()
    except OSError:
        return False
    for root in roots:
        try:
            if candidate.is_relative_to(Path(root).expanduser().resolve()):
                return True
        except OSError:
            continue
    return False


def resolve_workspace(path, *, base: Path, allowed_root: Path | None = None,
                     allowed_roots=None) -> Path:
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
    roots = []
    if allowed_root is not None:
        roots.append(Path(allowed_root).expanduser().resolve())
    roots.extend(Path(root).expanduser().resolve() for root in (allowed_roots or ()))
    if roots and not is_within_roots(candidate, roots):
        shown = roots[0] if len(roots) == 1 else roots
        raise ValueError(f'Workspace is outside the allowed root: {shown}')
    return candidate
