"""Cross-process exclusive file locks for small read-modify-write stores.

The lock is advisory and released by the OS when the process exits. Callers
must still keep critical sections short: the lock serializes writers across
processes, not readers.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:   # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:   # POSIX
    msvcrt = None

DEFAULT_TIMEOUT = 30.0
_POLL_SECONDS = 0.02


class FileLockTimeout(TimeoutError):
    """Raised when a lock cannot be acquired before the deadline."""


def _ensure_lock_byte(handle) -> None:
    """msvcrt locks byte ranges; make sure byte 0 exists before locking it."""
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b'\0')
        handle.flush()
    handle.seek(0)


def _try_acquire(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif msvcrt is not None:
        _ensure_lock_byte(handle)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:   # pragma: no cover - both primitives exist on supported platforms
        raise RuntimeError('no file locking API available')


def _release(handle) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        # Closing the handle below releases the OS lock as well.
        pass


@contextmanager
def exclusive_file_lock(path, *, timeout: float = DEFAULT_TIMEOUT):
    """Hold an exclusive lock on ``path`` for the duration of the block."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open('a+b')
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                _try_acquire(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise FileLockTimeout(f'timed out waiting for lock: {lock_path}') from None
                time.sleep(_POLL_SECONDS)
        try:
            yield
        finally:
            _release(handle)
    finally:
        handle.close()
