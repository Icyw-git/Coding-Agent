"""shell、后台任务与联网抓取。

`run_bash` 需要入口注入工作区与无头审批门，超时常量也由入口传入，方便测试按
`tools.BASH_TIMEOUT` 打补丁；`run_web_fetch` 只注入审批门；后台任务直接复用
background_task 的状态与锁。
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import sandbox
import web_access
from exec_policy import resolve_tool_path
from process_utils import close_process, kill_process_tree


def bash_timeout(timeout, *, default: float, background: float,
                 maximum: float) -> float:
    import background_task
    if timeout is None:
        return (background if background_task.current_background_id.get()
                else default)
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        raise ValueError('timeout must be a number of seconds') from None
    if value <= 0 or value > maximum:
        raise ValueError(f'timeout must be between 0 and {maximum} seconds')
    return value


def bash(command: str, cwd: str = None, timeout=None,
         run_in_background: bool = False, *, workdir, gate,
         default_timeout: float, background_timeout: float,
         max_timeout: float) -> str:
    # 判定只有一处（策略层），第三份黑名单已删：teammate / 后台 worker / 任务 runner
    # 绕过 PreToolUse hook 直接走到这里，凭据缺失时按无头路径审批（远程队列，超时拒绝）。
    try:
        base_path = (resolve_tool_path(cwd, workspace=workdir)
                     if cwd else Path(workdir).resolve())
    except Exception as exc:
        return f'Error: {exc}'
    if not base_path.is_dir():
        return f'Error: cwd does not exist: {base_path}'
    base = str(base_path)
    denied = gate('bash', {'command': command, 'cwd': base})
    if denied:
        return denied
    try:
        limit = bash_timeout(timeout, default=default_timeout,
                             background=background_timeout,
                             maximum=max_timeout)
    except ValueError as exc:
        return f'Error: {exc}'
    proc = None
    try:
        # A descendant can keep a PIPE open after cmd.exe exits. A temporary
        # output file lets the timeout bound execution without waiting for EOF.
        with tempfile.TemporaryFile() as output:
            # cwd 同时是 OS 沙箱的可写根：teammate 在 worktree 里执行时，
            # 可写范围就是那个 worktree，而不是主工作区。
            proc = sandbox.spawn_shell(command, cwd=base, stdout=output,
                                       workspace=base)
            import background_task
            background_task.register_process(proc)
            try:
                proc.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                kill_process_tree(proc)
                return (f'Error: Command timeout({limit:g}s); '
                        'process termination requested.')
            except BaseException:
                kill_process_tree(proc)
                raise
            output.seek(0)
            raw = output.read(200000)
        try:
            out = raw.decode("utf-8")
        except UnicodeDecodeError:
            out = raw.decode("gbk", errors="replace")
        out = out.strip()
        # PS Get-Content 误读 UTF-8 产生的替换符，GBK 控制台打印会崩
        out = out.replace('\ufffd', '?')
        return out[:50000] if out else "No output"
    except sandbox.SandboxUnavailable as e:
        return f'Error: {e}'
    except subprocess.TimeoutExpired:
        return 'Error: Timed out while terminating command; process state unknown.'
    except (FileNotFoundError, OSError) as e:
        return f'Error:{e}'
    finally:
        if proc is not None:
            try:
                import background_task
                background_task.unregister_process(proc)
            except Exception:
                pass
        close_process(proc)


def run_background_output(bg_id: str, wait: bool = False,
                          timeout: float = 30) -> str:
    """查看后台任务输出；wait=True 时最多阻塞 timeout 秒。"""
    import background_task
    if not isinstance(bg_id, str) or not bg_id.strip():
        return 'Error: bg_id must be a non-empty string'
    try:
        wait_seconds = float(timeout if timeout is not None else 30)
    except (TypeError, ValueError):
        return 'Error: timeout must be a number of seconds'
    if wait_seconds < 0:
        return 'Error: timeout must be non-negative'
    wait_seconds = min(wait_seconds, 600)
    task = (background_task.wait_background(bg_id, wait_seconds)
            if wait else background_task.background_snapshot(bg_id))
    if task is None:
        return f'Error: unknown background task {bg_id}'
    status = task['status']
    if status in ('running', 'queued'):
        return (f'[{bg_id}] {status}: {task.get("command", "")}\n'
                'call again with wait=true to block for completion')
    task, output = background_task.read_background_result(bg_id)
    if task is None:
        return f'Error: unknown background task {bg_id}'
    text = str(output if output is not None else task.get('result', ''))
    if len(text) > 50000:
        text = text[:50000] + '\n... (truncated)'
    return (f'[{bg_id}] {status}\nCommand: {task.get("command", "")}\n'
            f'Output:\n{text or "(no output)"}')


def run_kill_background(bg_id: str) -> str:
    import background_task
    if not isinstance(bg_id, str) or not bg_id.strip():
        return 'Error: bg_id must be a non-empty string'
    task = background_task.kill_background(bg_id)
    if task is None:
        return f'Error: unknown background task {bg_id}'
    status = task['status']
    if status in ('completed', 'failed', 'cancelled'):
        return f'[{bg_id}] already {status}'
    return f'[{bg_id}] cancel requested; use background_output to confirm'


def fetch_url(url: str, max_chars: int = 20000, *, gate) -> str:
    # 私网/本机地址在审批之前就拒掉：不给「批准一次 localhost 探测」的机会。
    try:
        web_access.check_url(url)
    except web_access.WebAccessError as exc:
        return f'Error: {exc}'
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        return 'Error: max_chars must be an integer'
    max_chars = max(1, min(max_chars, 100000))
    denied = gate('web_fetch', {'url': url})
    if denied:
        return denied
    try:
        result = web_access.fetch(url)
    except web_access.WebAccessError as exc:
        return f'Error: {exc}'
    text = result['text']
    header = (f'URL: {result["final_url"]}\n'
              f'Content-Type: {result["content_type"]}\n\n')
    if not text:
        return header + '(empty response)'
    if len(text) > max_chars:
        omitted = len(text) - max_chars
        text = text[:max_chars] + f'\n... (truncated; {omitted} chars omitted)'
    if result['truncated']:
        text += '\n... (response exceeded 2 MiB and was truncated)'
    return header + text
