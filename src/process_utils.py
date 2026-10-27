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
    """Release sandbox/job handles; Popen itself needs none.

    资源限额的 Job Object 句柄挂在 proc 上（见 sandbox._popen_shell）：这里
    统一关掉，避免每个命令漏一个内核对象。关句柄不会杀进程——限额层刻意
    没设 KILL_ON_JOB_CLOSE。
    """
    from sandbox import SandboxProcess
    if isinstance(proc, SandboxProcess):
        proc.close()
    # 只处理真实写入的 job 句柄。测试用 Mock 替换 Popen 时，`getattr` 对任意
    # 属性都返回一个新 Mock（不会 AttributeError），把它当内核句柄传给
    # CloseHandle 会栈溢出。因此从 __dict__ 里取，并校验是整数句柄。
    job = getattr(proc, '__dict__', {}).get('_harness_job')
    if isinstance(job, int) and job:
        try:
            import limits
            limits.close(job)
        except Exception:
            pass
    try:
        proc.__dict__.pop('_harness_job', None)
    except (AttributeError, TypeError):
        pass
