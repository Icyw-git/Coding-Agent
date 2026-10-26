"""常驻 PowerShell worker：把「每次调用起一个进程」变成「复用同一个会话」。

## 问题

`screen_capture` / `clipboard_*` / `desktop_*` 的实现都是「拼一段 PowerShell →
`subprocess.run`」。每次调用要付两笔固定成本：

- 进程启动 + 运行时初始化：实测 263 ms（空脚本就要这么多）；
- `Add-Type -AssemblyName` / `Add-Type -TypeDefinition`（C# 编译）：
  首次 2.2 s，之后每次仍要重跑一遍脚本头的 376 ms。

桌面控制走的是「快照 → 动作」两步，序列最多 12 步。按每次 882 ms 算，一个
10 步序列要 9 秒，其中 8.8 秒是重复的启动成本。

## 做法

常驻一个 PowerShell 进程，标准输入喂 JSON 请求、标准输出读 JSON 响应，
`Add-Type` 只在进程启动时做一次。实测每次调用 **0.2 ms**（含一次
`GetForegroundWindow`）。

协议刻意极简，因为这里出错的代价是「工具全挂」：

    请求：一行 JSON  {"op": "...", ...}\n
    响应：一行 JSON  {"ok": true, ...}\n        或 {"ok": false, "error": "..."}\n

- 一行一个请求/响应，不搞长度前缀：PowerShell 侧 `ReadLine` 最不容易写错。
- 输出必须是单行 JSON（`ConvertTo-Json -Compress` 会自带换行，所以先 trim）。
- 任何未捕获异常都转成 `{"ok": false}` 并**不让进程退出**——一个坏请求不能
  把后面所有调用拖死。
- stdout 只放协议数据，诊断一律走 stderr。

## 生命周期与失败策略

- 首次使用时懒启动；`shutdown()` 在 atexit 注册。
- 进程死掉（崩溃、被杀、超时）时自动重启一次再重试该请求；连续失败则熔断，
  退回「每次起进程」的旧路径（`run()` 的 `fallback`），保证功能不会整体失效。
- worker 是**每进程单例**：多线程调用加锁串行。桌面控制本身就是 CLI/单用户
  交互场景，串行不会成为瓶颈，而并发写同一个 stdin 一定会把协议搞乱。
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import threading
import time

BOOT_TIMEOUT = 20.0
DEFAULT_TIMEOUT = 15.0
MAX_CONSECUTIVE_FAILURES = 3

# 每个 worker 自己带脚本：这里只放公共装配（错误策略 + 输出约定），
# 具体能力（截屏 / 剪贴板 / 桌面）在各自模块的 WORKER_SCRIPT 里定义。
PRELUDE = r'''
$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Write-Reply($obj) {
    $json = $obj | ConvertTo-Json -Depth 12 -Compress
    [Console]::Out.Write($json + "`n")
    [Console]::Out.Flush()
}

function Read-Request {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { return $null }
    $line = $line.Trim()
    if (-not $line) { return @{} }
    return $line | ConvertFrom-Json
}
'''

LOOP = r'''
while ($true) {
    try {
        $req = Read-Request
        if ($null -eq $req) { break }
        if ($req.op -eq '__ping__') { Write-Reply @{ ok = $true; pong = $true }; continue }
        if ($req.op -eq '__quit__') { break }
        $result = Invoke-HarnessOp $req
        if ($null -eq $result) { $result = @{ ok = $true } }
        # OrderedDictionary 没有 ContainsKey 方法（只有 IDictionary 接口），
        # 用键索引判断即可；两种容器都能走这条路径。
        if ($null -eq $result['ok']) { $result['ok'] = $true }
        Write-Reply $result
    } catch {
        $message = $_.Exception.Message
        # 能力模块用这个哨兵把「已经构造好的失败对象」交回来：回包要带
        # state/step/steps_done，不能压成一句 error。
        if ($message -eq '__harness_failure__' -and $null -ne $script:HarnessFailure) {
            try { Write-Reply $script:HarnessFailure } catch { }
            $script:HarnessFailure = $null
            continue
        }
        if (-not $message) { $message = 'unknown error' }
        try {
            Write-Reply @{ ok = $false; error = "$message" }
        } catch {
            [Console]::Error.WriteLine($message)
        }
    }
}
'''


class WorkerError(RuntimeError):
    """worker 不可用或响应不合法。"""


def _powershell_exe() -> str | None:
    override = (os.getenv('HARNESS_POWERSHELL') or '').strip()
    return override or shutil.which('powershell') or shutil.which('pwsh')


class Worker:
    """一个常驻 PowerShell 进程；线程安全，串行处理请求。"""

    def __init__(self, name: str, script: str, *, boot_timeout: float = BOOT_TIMEOUT):
        self.name = name
        self.script = script
        self.boot_timeout = boot_timeout
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._failures = 0
        self._broken = False
        self.stats = {'calls': 0, 'restarts': 0, 'errors': 0, 'total_ms': 0.0}

    # ---------- 生命周期 ----------

    def _start(self) -> None:
        exe = _powershell_exe()
        if exe is None:
            raise WorkerError('PowerShell is not available')
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        try:
            self._proc = subprocess.Popen(
                [exe, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                 '-WindowStyle', 'Hidden', '-Command', self.script],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding='utf-8',
                errors='replace', bufsize=1, creationflags=flags,
            )
        except OSError as exc:
            raise WorkerError(f'cannot start PowerShell worker: {exc}') from exc

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.stdin.write(json.dumps({'op': '__quit__'}) + '\n')
                proc.stdin.flush()
                proc.wait(timeout=3)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except (OSError, AttributeError):
                    pass

    def _ensure(self) -> None:
        if self._broken:
            raise WorkerError(f'{self.name} worker is unavailable')
        if self._proc is None or self._proc.poll() is not None:
            self._stop()
            self._start()
            self.stats['restarts'] += 1
            self._wait_ready()

    def _wait_ready(self) -> None:
        """等 worker 能响应 ping；脚本头编译（Add-Type）在这段时间里完成。"""
        deadline = time.monotonic() + self.boot_timeout
        last = ''
        while time.monotonic() < deadline:
            try:
                reply = self._roundtrip({'op': '__ping__'}, timeout=min(
                    2.0, max(0.2, deadline - time.monotonic())))
            except WorkerError as exc:
                last = str(exc)
                if self._proc is None or self._proc.poll() is not None:
                    raise WorkerError(
                        f'{self.name} worker exited during startup: {self._stderr_tail()}') from exc
                continue
            if reply.get('pong'):
                return
        raise WorkerError(f'{self.name} worker did not become ready: {last}')

    def _stderr_tail(self, limit: int = 400) -> str:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return ''
        try:
            import select

            if select.select([proc.stderr], [], [], 0)[0]:
                return proc.stderr.read(limit).strip()
        except (OSError, ValueError, ImportError):
            pass
        return ''

    # ---------- 请求 ----------

    def _roundtrip(self, request: dict, *, timeout: float) -> dict:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise WorkerError(f'{self.name} worker is not running')
        payload = json.dumps(request, ensure_ascii=False) + '\n'
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise WorkerError(f'{self.name} worker stdin closed: {exc}') from exc
        result: dict = {}

        def read() -> None:
            try:
                line = proc.stdout.readline()
                if line:
                    result['line'] = line
            except (OSError, ValueError):
                pass

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            # 卡住的 worker 不能留：下次请求会读到上一条的响应。
            self._stop()
            raise WorkerError(f'{self.name} worker timed out after {timeout:g}s')
        line = result.get('line') or ''
        if not line.strip():
            self._stop()
            raise WorkerError(f'{self.name} worker closed unexpectedly: {self._stderr_tail()}')
        try:
            reply = json.loads(line)
        except ValueError as exc:
            self._stop()
            raise WorkerError(
                f'{self.name} worker returned non-JSON: {line[:200]}') from exc
        if not isinstance(reply, dict):
            raise WorkerError(f'{self.name} worker returned {type(reply).__name__}')
        return reply

    def call(self, request: dict, *, timeout: float = DEFAULT_TIMEOUT) -> dict:
        """发一个请求并返回响应；不检查 ``ok``（调用方决定怎么处理）。"""
        with self._lock:
            started = time.perf_counter()
            try:
                self._ensure()
                reply = self._roundtrip(request, timeout=timeout)
            except WorkerError:
                self._failures += 1
                self.stats['errors'] += 1
                if self._failures >= MAX_CONSECUTIVE_FAILURES:
                    self._broken = True
                raise
            self._failures = 0
            self.stats['calls'] += 1
            self.stats['total_ms'] += (time.perf_counter() - started) * 1000
            return reply

    def healthy(self) -> bool:
        return not self._broken and self._proc is not None \
            and self._proc.poll() is None

    def summary(self) -> str:
        calls = self.stats['calls']
        avg = (self.stats['total_ms'] / calls) if calls else 0.0
        return (f'{self.name}: calls={calls} avg={avg:.2f}ms '
                f'restarts={self.stats["restarts"]} errors={self.stats["errors"]}'
                + (' [broken]' if self._broken else ''))


# ---------- 进程级单例 ----------

_workers: dict[str, Worker] = {}
_workers_lock = threading.Lock()


def get_worker(name: str, script) -> Worker:
    """按名字复用 worker；``script`` 只在首次创建时使用。"""
    with _workers_lock:
        worker = _workers.get(name)
        if worker is None:
            worker = Worker(name, script() if callable(script) else script)
            _workers[name] = worker
        return worker


def shutdown() -> None:
    with _workers_lock:
        workers = list(_workers.values())
        _workers.clear()
    for worker in workers:
        worker._stop()


atexit.register(shutdown)


def stats() -> dict[str, str]:
    with _workers_lock:
        return {name: worker.summary() for name, worker in _workers.items()}


def run(name: str, script, request: dict, *, fallback=None,
        timeout: float = DEFAULT_TIMEOUT, fail_on_error: bool = True):
    """走常驻 worker；worker 不可用时退回 ``fallback()``。

    ``fail_on_error`` 为真时，``{"ok": false}`` 会抛 ``WorkerError``——
    调用方（各能力模块）本来就把失败转成给模型看的错误文案，抛异常让这条
    路径保持单一出口。
    """
    try:
        worker = get_worker(name, script)
        reply = worker.call(request, timeout=timeout)
    except WorkerError:
        if fallback is not None:
            return fallback()
        raise
    if fail_on_error and not reply.get('ok'):
        # 失败回包带的不只是 error 代码：能力模块还会给 message（含 step i/total
        # 之类的重试提示）与 steps_done。只把代码抛给上层，模型就只能看到
        # 「out_of_window」这种没法照做的词，所以这里把可读信息拼进去。
        raise WorkerError(_failure_text(reply))
    return reply


def _failure_text(reply: dict) -> str:
    message = str(reply.get('message') or '').strip()
    code = str(reply.get('error') or '').strip()
    if message and code and code not in message:
        return f'{code}: {message}'
    return message or code or 'worker reported failure'
