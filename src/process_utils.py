"""Terminate an owned shell and its child processes."""

import os
import signal
import subprocess


def kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        result = subprocess.run(
            ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
            capture_output=True, check=False, timeout=10,
        )
        if result.returncode != 0 and proc.poll() is None:
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, getattr(signal, 'SIGKILL', 9))
        except ProcessLookupError:
            pass
    proc.wait(timeout=10)
