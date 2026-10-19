"""Terminate an owned shell and its child processes."""

import os
import signal
import subprocess


def kill_process_tree(proc) -> None:
    if proc.poll() is not None:
        return
    from sandbox import SandboxProcess  # 局部导入：避免与 task_manager 的导入顺序耦合
    if isinstance(proc, SandboxProcess):
        proc.kill_tree()
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


def close_process(proc) -> None:
    """Release sandbox handles (kills any remaining descendants); Popen needs none."""
    from sandbox import SandboxProcess
    if isinstance(proc, SandboxProcess):
        proc.close()
