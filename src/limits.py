"""进程资源限额：用 Job Object 给命令加内存/进程数/CPU 上限。

## 为什么单独一层

`bash` 只有超时（时间维度）。一个 `npm install` 或者失控的脚本可以把整机内存
吃光，让桌面卡死——超时管不到这件事，因为它是「慢慢变卡」而不是「卡住不动」。

Job Object 是 Windows 内核提供的资源容器：进程一旦加入，它的**所有后代**都受
同一份限制约束。这正是需要的语义——杀父进程容易，问题是子孙进程会分叉。

## 与 sandbox.py 的关系

`src/sandbox.py` 的 Job Object 只在 Low IL 沙箱路径创建，而沙箱默认 `off`。
这里做的是**独立的限额层**，不依赖完整性标签，默认就生效：

    sandbox（要不要隔离写入）  ─┐
                                ├→ 各自独立，可单独开关
    limits（能用多少资源）    ─┘

刻意不复用 `sandbox._make_job`：那个函数设的是 `KILL_ON_JOB_CLOSE`，语义不同
（防遗留进程），而且沙箱路径有自己的进程创建逻辑。混在一起会让两边都难改。

## 限额项

- **内存**：`JOB_OBJECT_LIMIT_PROCESS_MEMORY`（单进程）+ `JOB_OBJECT_LIMIT_JOB_MEMORY`
  （整组合计）。超出时分配失败，进程通常以「内存不足」退出。
- **进程数**：`ActiveProcessLimit`。挡住 fork 炸弹这类。
- **CPU 时间**：`PerJobUserTimeLimit`。这是**累计用户态时间**，不是墙钟——
  超时已经管墙钟了，这里管「跑了很久但一直在算」的情况。

## 失败语义

限额**不该让命令跑不起来**。任何一步失败（CreateJobObject 失败、结构体设置
失败、AssignProcessToJobObject 失败）都只是放弃限额，照常执行命令，并记录原因。
个人单机上「命令能跑」比「限额一定生效」更重要；而限额本身是护栏，不是安全边界。
"""
from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

_WINDOWS = os.name == 'nt'

# JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
# 限额标志位（winnt.h）
_LIMIT_PROCESS_MEMORY = 0x0100
_LIMIT_JOB_MEMORY = 0x0200
_LIMIT_ACTIVE_PROCESS = 0x0008
_LIMIT_JOB_TIME = 0x0004
# 100ns 单位（FILETIME 语义）
_100NS_PER_SECOND = 10_000_000

ENV_MEMORY_MB = 'HARNESS_LIMIT_MEMORY_MB'
ENV_ACTIVE_PROCESSES = 'HARNESS_LIMIT_PROCESSES'
ENV_CPU_SECONDS = 'HARNESS_LIMIT_CPU_SECONDS'

# 默认：单进程 2 GiB、整组 4 GiB、64 个进程、不限制 CPU 时间。
# 取值依据：2 GiB 能过 `npm install` / 大型编译的单进程峰值，又能挡住
# 「一次分配几十 G」的失控；整组 4 GiB 给并发子进程留余量。
DEFAULT_MEMORY_MB = 2048
DEFAULT_ACTIVE_PROCESSES = 64
DEFAULT_CPU_SECONDS = 0          # 0 = 不限制


@dataclass(frozen=True)
class Limits:
    memory_mb: int = DEFAULT_MEMORY_MB
    active_processes: int = DEFAULT_ACTIVE_PROCESSES
    cpu_seconds: int = DEFAULT_CPU_SECONDS

    @property
    def enabled(self) -> bool:
        return self.memory_mb > 0 or self.active_processes > 0 or self.cpu_seconds > 0

    def describe(self) -> str:
        if not self.enabled:
            return '资源限额：关'
        parts = []
        if self.memory_mb:
            parts.append(f'单进程 {self.memory_mb} MB / 整组 {self.memory_mb * 2} MB')
        if self.active_processes:
            parts.append(f'进程数 {self.active_processes}')
        if self.cpu_seconds:
            parts.append(f'CPU {self.cpu_seconds}s')
        return '资源限额：' + ' · '.join(parts)


def _parse_int(raw, default: int, *, minimum: int = 0,
               maximum: int = 10 ** 7) -> int:
    text = str(raw or '').strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    if value < minimum or value > maximum:
        return default
    return value


def from_env(environ=None) -> Limits:
    """从环境解析限额；非法值静默回落默认（护栏不该阻止启动）。"""
    env = os.environ if environ is None else environ
    return Limits(
        memory_mb=_parse_int(env.get(ENV_MEMORY_MB), DEFAULT_MEMORY_MB,
                             minimum=0, maximum=1024 * 1024),
        active_processes=_parse_int(env.get(ENV_ACTIVE_PROCESSES),
                                    DEFAULT_ACTIVE_PROCESSES,
                                    minimum=0, maximum=100_000),
        cpu_seconds=_parse_int(env.get(ENV_CPU_SECONDS), DEFAULT_CPU_SECONDS,
                               minimum=0, maximum=86400),
    )


if _WINDOWS:  # pragma: no branch - 平台相关
    from ctypes import wintypes as w

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ('PerProcessUserTimeLimit', ctypes.c_longlong),
            ('PerJobUserTimeLimit', ctypes.c_longlong),
            ('LimitFlags', w.DWORD),
            ('MinimumWorkingSetSize', ctypes.c_size_t),
            ('MaximumWorkingSetSize', ctypes.c_size_t),
            ('ActiveProcessLimit', w.DWORD),
            ('Affinity', ctypes.c_size_t),
            ('PriorityClass', w.DWORD),
            ('SchedulingClass', w.DWORD),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ('BasicLimitInformation', _BASIC_LIMIT),
            ('IoInfo', _IO_COUNTERS),
            ('ProcessMemoryLimit', ctypes.c_size_t),
            ('JobMemoryLimit', ctypes.c_size_t),
            ('PeakProcessMemoryUsed', ctypes.c_size_t),
            ('PeakJobMemoryUsed', ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = w.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int,
                                                  ctypes.c_void_p, w.DWORD]
    _kernel32.SetInformationJobObject.restype = w.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = w.BOOL
    _kernel32.CloseHandle.argtypes = [w.HANDLE]
    _kernel32.CloseHandle.restype = w.BOOL
    _kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    _kernel32.OpenProcess.restype = w.HANDLE

    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001


class LimitError(RuntimeError):
    """限额对象创建失败；调用方应放弃限额而不是放弃命令。"""


def available() -> bool:
    return _WINDOWS


def create(limits: Limits | None = None):
    """创建一个带限额的 Job Object；失败抛 ``LimitError``。

    调用方负责关闭句柄（``close``）。返回的句柄在关闭时会连带终止组内进程吗？
    **不会**——这里刻意不设 `KILL_ON_JOB_CLOSE`：限额是护栏，不是生命周期管理，
    关句柄不该杀进程。
    """
    if not _WINDOWS:
        raise LimitError('resource limits need Windows Job Objects')
    limits = limits or from_env()
    if not limits.enabled:
        raise LimitError('all limits are disabled')
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        raise LimitError(
            f'CreateJobObjectW failed (error {ctypes.get_last_error()})')
    info = _EXTENDED_LIMIT()
    flags = 0
    if limits.memory_mb:
        info.ProcessMemoryLimit = limits.memory_mb * 1024 * 1024
        info.JobMemoryLimit = limits.memory_mb * 2 * 1024 * 1024
        flags |= _LIMIT_PROCESS_MEMORY | _LIMIT_JOB_MEMORY
    if limits.active_processes:
        info.BasicLimitInformation.ActiveProcessLimit = limits.active_processes
        flags |= _LIMIT_ACTIVE_PROCESS
    if limits.cpu_seconds:
        info.BasicLimitInformation.PerJobUserTimeLimit = (
            limits.cpu_seconds * _100NS_PER_SECOND)
        flags |= _LIMIT_JOB_TIME
    info.BasicLimitInformation.LimitFlags = flags
    ok = _kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
        ctypes.sizeof(info))
    if not ok:
        error = ctypes.get_last_error()
        _kernel32.CloseHandle(job)
        raise LimitError(f'SetInformationJobObject failed (error {error})')
    return job


def assign(job, pid: int) -> None:
    """把已存在的进程加入 job；失败抛 ``LimitError``。"""
    if not _WINDOWS:
        raise LimitError('resource limits need Windows Job Objects')
    handle = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
                                   False, int(pid))
    if not handle:
        raise LimitError(
            f'OpenProcess({pid}) failed (error {ctypes.get_last_error()})')
    try:
        if not _kernel32.AssignProcessToJobObject(job, handle):
            raise LimitError(
                f'AssignProcessToJobObject failed (error '
                f'{ctypes.get_last_error()})')
    finally:
        _kernel32.CloseHandle(handle)


def close(job) -> None:
    if job and _WINDOWS:
        _kernel32.CloseHandle(job)


def apply_to_process(proc, limits: Limits | None = None):
    """给一个已经 spawn 的进程套上限额；失败返回 None 而不是抛错。

    返回值是 job 句柄，调用方要在进程结束后 ``close()``。失败时返回 None，
    调用方照常等进程结束——限额不该让命令跑不起来。
    """
    if not _WINDOWS:
        return None
    try:
        job = create(limits)
    except LimitError:
        return None
    try:
        assign(job, proc.pid)
    except LimitError:
        close(job)
        return None
    return job


def status_line(limits: Limits | None = None) -> str:
    limits = limits or from_env()
    if not _WINDOWS and limits.enabled:
        return '资源限额：不可用（仅支持 Windows）'
    return limits.describe()
