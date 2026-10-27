"""Windows 低完整性（Low IL）OS 沙箱：约束 shell 进程只能写工作区。

策略层（exec_policy）回答「该不该做」，这里回答「内核允不允许」。两者独立：
``HARNESS_OS_SANDBOX=auto|require`` 启用，``off``（默认）保持重构前行为。

机制：给子进程一个 Low 完整性令牌。低完整性进程可以读 Medium 对象，但写不进去；
把工作区目录标为 Low 后，工作区内的读写放行，工作区外（用户目录、系统目录、
注册表等）的写入一律被内核拒绝。这不是完整的隔离：**文件读取和网络不受限制**，
所以不要把 read-only / never 档的安全承诺建立在这一层上。

启用时的一次性副作用（仅首次，按工作区缓存）：

- 工作区目录树被标记为 Low 完整性；标记在 SACL 上，需要目录的 WRITE_OWNER。
- 若当前用户没有 WRITE_OWNER，脚本会先给当前用户补一条继承的 ``(OI)(CI)(WO)``
  ACE（能改 DACL 即可，不需要管理员），再重试标记。补的是你自己的权限，不会
  把工作区开放给其他账户。此后任何 Low 完整性进程（例如浏览器渲染进程）也能
  修改工作区——这是 Low IL 方案的固有代价。
- 命令的 TEMP/TMP 指向 ``<工作区>/.cache/sandbox-tmp``，避免临时文件写不进去。

工作区在卷/文件系统上不支持完整性标签时，``auto`` 警告一次后退回普通 Popen，
``require`` 拒绝启动命令（fail-closed）。非 Windows 平台同理：只有 ``require``
会报错，因为没有可用的后端。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
from pathlib import Path

ENV = 'HARNESS_OS_SANDBOX'
MODES = ('off', 'auto', 'require')
# 为什么没有默认开启（2026-10-27 实测后回退）
#
# 试过把默认值改成 auto，在本仓库实测后回退。三个真实问题：
#
# 1. **把仓库当工作区会自锁**。`_prepare_workspace` 先 mkdir 再对整棵树
#    `/T` 打 Low；新目录继承 Low 之后，`icacls` 自己（以 Low 运行）就再也
#    改不动 `.cache/sandbox-tmp`，于是每次都降级回普通执行，还刷一行警告。
# 2. **打标持久且影响日常**。工作区根被打上 Low 后 `docs/` 也带了标签，
#    要恢复得先补 WRITE_OWNER 再 `/setintegritylevel (OI)(CI)M`。用户不该为了
#    试一个开关去修自己的目录权限。
# 3. **收益与风险不匹配**。性能代价实测确实不可测（环境块缓存后 off/auto
#    差值 −10% ~ +9%），但它只挡「工作区外写入」，不挡读取与网络。默认打开
#    会让用户误以为「安全了」，实际秘密文件仍只靠策略层。
#
# 想用就显式设 HARNESS_OS_SANDBOX=auto（或 require 强制）。另外工作区最好
# 指向专门的目录，而不是仓库根——那样第 1、2 条都不会发生。
DEFAULT_MODE = 'off'

_WINDOWS = os.name == 'nt'
_WARNED: frozenset[str] = frozenset()
_STATE: dict[str, tuple[bool, str]] = {}
_STATE_LOCK = threading.Lock()

_LOW_SID = 'S-1-16-4096'
_SE_GROUP_INTEGRITY = 0x20
_TOKEN_QUERY = 0x0008
_MAXIMUM_ALLOWED = 0x02000000
_TOKEN_USER = 1
_TOKEN_INTEGRITY_LEVEL = 25
_DISABLE_MAX_PRIVILEGE = 0x0001

_SE_FILE_OBJECT = 1
_SACL_SECURITY_INFORMATION = 0x10
_SYSTEM_MANDATORY_LABEL_ACE = 0x11

_CREATE_SUSPENDED = 0x0004
_CREATE_UNICODE_ENVIRONMENT = 0x0400
_STARTF_USESTDHANDLES = 0x0100
_HANDLE_FLAG_INHERIT = 0x0001
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ_WRITE = 0x00000003
_OPEN_EXISTING = 3
_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_ERROR_INSUFFICIENT_BUFFER = 122
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class SandboxUnavailable(RuntimeError):
    """沙箱无法建立（平台/标签/权限）；require 档必须拒绝执行。"""


if _WINDOWS:
    import ctypes.wintypes as w
    import msvcrt

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)

    PSID = ctypes.c_void_p
    PACL = ctypes.c_void_p
    HANDLE = w.HANDLE

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [('Sid', PSID), ('Attributes', w.DWORD)]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ('cb', w.DWORD), ('lpReserved', w.LPWSTR), ('lpDesktop', w.LPWSTR),
            ('lpTitle', w.LPWSTR), ('dwX', w.DWORD), ('dwY', w.DWORD),
            ('dwXSize', w.DWORD), ('dwYSize', w.DWORD),
            ('dwXCountChars', w.DWORD), ('dwYCountChars', w.DWORD),
            ('dwFillAttribute', w.DWORD), ('dwFlags', w.DWORD),
            ('wShowWindow', w.WORD), ('cbReserved2', w.WORD),
            ('lpReserved2', ctypes.c_void_p),
            ('hStdInput', HANDLE), ('hStdOutput', HANDLE), ('hStdError', HANDLE),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ('hProcess', HANDLE), ('hThread', HANDLE),
            ('dwProcessId', w.DWORD), ('dwThreadId', w.DWORD),
        ]

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [('nLength', w.DWORD), ('lpSecurityDescriptor', ctypes.c_void_p),
                    ('bInheritHandle', w.BOOL)]

    class _ACL(ctypes.Structure):
        _fields_ = [('AclRevision', w.BYTE), ('Sbz1', w.BYTE),
                    ('AclSize', w.WORD), ('AceCount', w.WORD), ('Sbz2', w.WORD)]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
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

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ('BasicLimitInformation', _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ('IoInfo', _IO_COUNTERS),
            ('ProcessMemoryLimit', ctypes.c_size_t),
            ('JobMemoryLimit', ctypes.c_size_t),
            ('PeakProcessMemoryUsed', ctypes.c_size_t),
            ('PeakJobMemoryUsed', ctypes.c_size_t),
        ]

    def _init_api() -> None:
        k, a = _kernel32, _advapi32
        k.GetCurrentProcess.restype = HANDLE
        k.CloseHandle.argtypes = [HANDLE]
        k.CloseHandle.restype = w.BOOL
        k.WaitForSingleObject.argtypes = [HANDLE, w.DWORD]
        k.WaitForSingleObject.restype = w.DWORD
        k.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(w.DWORD)]
        k.GetExitCodeProcess.restype = w.BOOL
        k.TerminateProcess.argtypes = [HANDLE, w.UINT]
        k.TerminateProcess.restype = w.BOOL
        k.ResumeThread.argtypes = [HANDLE]
        k.ResumeThread.restype = w.DWORD
        k.SetHandleInformation.argtypes = [HANDLE, w.DWORD, w.DWORD]
        k.SetHandleInformation.restype = w.BOOL
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                  w.DWORD, w.DWORD, HANDLE]
        k.CreateFileW.restype = HANDLE
        k.LocalFree.argtypes = [ctypes.c_void_p]
        k.LocalFree.restype = ctypes.c_void_p
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = HANDLE
        k.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.TerminateJobObject.argtypes = [HANDLE, w.UINT]
        k.TerminateJobObject.restype = w.BOOL
        k.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
        k.AssignProcessToJobObject.restype = w.BOOL

        a.OpenProcessToken.argtypes = [HANDLE, w.DWORD, ctypes.POINTER(HANDLE)]
        a.OpenProcessToken.restype = w.BOOL
        a.GetTokenInformation.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          w.DWORD, ctypes.POINTER(w.DWORD)]
        a.GetTokenInformation.restype = w.BOOL
        a.CreateRestrictedToken.argtypes = [
            HANDLE, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD,
            ctypes.c_void_p, w.DWORD, ctypes.c_void_p, ctypes.POINTER(HANDLE)]
        a.CreateRestrictedToken.restype = w.BOOL
        a.SetTokenInformation.argtypes = [HANDLE, ctypes.c_int,
                                          ctypes.c_void_p, w.DWORD]
        a.SetTokenInformation.restype = w.BOOL
        a.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, ctypes.POINTER(PSID)]
        a.ConvertStringSidToSidW.restype = w.BOOL
        a.ConvertSidToStringSidW.argtypes = [PSID, ctypes.POINTER(w.LPWSTR)]
        a.ConvertSidToStringSidW.restype = w.BOOL
        a.GetLengthSid.argtypes = [PSID]
        a.GetLengthSid.restype = w.DWORD
        a.GetNamedSecurityInfoW.argtypes = [
            w.LPWSTR, ctypes.c_int, w.DWORD, ctypes.POINTER(PSID),
            ctypes.POINTER(PSID), ctypes.POINTER(PACL), ctypes.POINTER(PACL),
            ctypes.POINTER(ctypes.c_void_p)]
        a.GetNamedSecurityInfoW.restype = w.DWORD
        a.GetSecurityDescriptorSacl.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(w.BOOL), ctypes.POINTER(PACL),
            ctypes.POINTER(w.BOOL)]
        a.GetSecurityDescriptorSacl.restype = w.BOOL
        a.GetAce.argtypes = [PACL, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        a.GetAce.restype = w.BOOL
        a.CreateProcessAsUserW.argtypes = [
            HANDLE, w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            w.BOOL, w.DWORD, ctypes.c_void_p, w.LPCWSTR,
            ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION)]
        a.CreateProcessAsUserW.restype = w.BOOL

    _init_api()


def configured_mode(environ=None) -> str:
    """读 HARNESS_OS_SANDBOX；非法值提示一次并按 off 处理（不静默假装已开启）。"""
    env = os.environ if environ is None else environ
    mode = (env.get(ENV) or '').strip().casefold()
    if not mode:
        return DEFAULT_MODE
    if mode not in MODES:
        _warn_once(f'unknown {ENV} {mode!r}, using {DEFAULT_MODE}')
        return DEFAULT_MODE
    return mode


def status_line() -> str:
    """给 /sandbox、/status 和 --doctor 的一行说明；不触发任何检测。"""
    mode = configured_mode()
    detail = {
        'off': '未启用，shell 直接运行',
        'auto': '可用时启用，失败退回普通执行并提示',
        'require': '必须启用，失败则拒绝执行',
    }[mode]
    return f'{mode}（{detail}）'


def spawn_shell(command: str, *, cwd, stdout, workspace=None):
    """按配置启动 ``cmd /c command``，返回 Popen 兼容对象。

    ``stdout`` 同时接收 stderr。``workspace`` 是要标记为 Low 的目录树根，
    默认取 cwd；工作区外写入会被沙箱拒绝。off 与 auto 回退时走原生的
    ``subprocess.Popen``，保持既有调用点和 monkeypatch 的行为不变。
    """
    mode = configured_mode()
    if not _WINDOWS:
        if mode == 'require':
            raise SandboxUnavailable('OS 沙箱不可用：仅支持 Windows 低完整性后端')
        if mode == 'auto':
            _warn_once('OS 沙箱仅支持 Windows，auto 档已退回普通执行')
        return _popen_shell(command, cwd, stdout)
    if mode == 'off':
        return _popen_shell(command, cwd, stdout)
    try:
        return _spawn_sandboxed(command, cwd, stdout, workspace)
    except (SandboxUnavailable, OSError) as exc:
        # 令牌/进程创建失败同样是「沙箱不可用」：require 必须拒绝，
        # auto 退回普通执行，而不是让一条 OSError 绕过降级语义。
        if mode == 'require':
            raise SandboxUnavailable(f'OS 沙箱不可用：{exc}') from exc
        _warn_once(f'OS 沙箱不可用（{exc}），命令未隔离运行')
        return _popen_shell(command, cwd, stdout)


def reset_cache() -> None:
    """测试与故障恢复用：清掉工作区就绪状态和已提示记录。"""
    global _WARNED
    with _STATE_LOCK:
        _STATE.clear()
        _WARNED = frozenset()
    with _ENV_BLOCK_LOCK:
        _ENV_BLOCK_CACHE.clear()


def _popen_shell(command: str, cwd, stdout):
    """默认路径：普通 Popen + 资源限额（不依赖 Low IL 沙箱）。

    限额是护栏不是安全边界，套不上就照常跑（``apply_to_process`` 失败返回
    None）。进程结束后关句柄，避免每个命令漏一个内核对象。
    """
    proc = subprocess.Popen(command, shell=True, cwd=cwd, stdout=stdout,
                            stderr=subprocess.STDOUT,
                            start_new_session=os.name != 'nt')
    try:
        import limits

        # proc 必须是真进程才能套限额：测试常用 Mock 替换 Popen，而 Mock 的
        # 任意属性都会自动生成，`getattr(proc, 'pid')` 拿到的是 Mock 而不是
        # 整数，assign 会失败或死循环。显式检查类型，不做猜测。
        if (isinstance(proc, subprocess.Popen)
                and limits.available() and limits.from_env().enabled):
            job = limits.apply_to_process(proc)
            if job is not None:
                proc._harness_job = job
    except Exception as exc:
        _warn_once(f'资源限额未能应用（{exc}），命令未受限额约束')
    return proc


def _warn_once(message: str) -> None:
    global _WARNED
    if message in _WARNED:
        return
    _WARNED = _WARNED | {message}
    print(f'[sandbox] {message}', file=sys.stderr)


# ============================================================
# 工作区准备：Low 完整性标签
# ============================================================

def _ensure_workspace(path: Path) -> tuple[bool, str]:
    key = str(path)
    with _STATE_LOCK:
        if key in _STATE:
            return _STATE[key]
        state = _prepare_workspace(path)
        _STATE[key] = state
        return state


def _prepare_workspace(path: Path) -> tuple[bool, str]:
    if not path.is_dir():
        return False, f'工作区不存在：{path}'
    temp_dir = path / '.cache' / 'sandbox-tmp'
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f'无法创建工作区临时目录 {temp_dir}：{exc}'
    if _low_label(path) and _low_label(temp_dir):
        return True, ''

    granted = False
    if not _low_label(path):
        _copy_low_label(path)
        if not _low_label(path):
            granted = _grant_write_owner(path)
            if granted:
                _copy_low_label(path)
    if not _low_label(path):
        return False, _label_help(path, granted)
    if not _low_label(temp_dir):
        _copy_low_label(temp_dir)
        if not _low_label(temp_dir):
            return False, f'无法把沙箱临时目录标记为 Low：{temp_dir}'
    if granted:
        _warn_once(f'已为当前用户补充工作区 WRITE_OWNER 并标记 Low 完整性：{path}')
    else:
        _warn_once(f'工作区已标记为 Low 完整性：{path}')
    return True, ''


def _label_help(path: Path, granted: bool) -> str:
    sid = _current_user_sid()
    grant = (f'icacls "{path}" /grant *{sid}:(OI)(CI)(WO)' if sid
             else f'icacls "{path}" /grant *<你的用户SID>:(OI)(CI)(WO)')
    prefix = '补充 WRITE_OWNER 后仍无法标记' if granted else '无法设置 Low 完整性标签'
    return (f'{prefix}：{path}。需要有该目录的 WRITE_OWNER；可在同一个终端执行：'
            f'{grant} && icacls "{path}" /setintegritylevel (OI)(CI)L /T /C /Q')


def _copy_low_label(path: Path) -> None:
    _run_icacls(path, '/setintegritylevel', '(OI)(CI)L', '/T', '/C', '/Q')


def _grant_write_owner(path: Path) -> bool:
    sid = _current_user_sid()
    if not sid:
        return False
    return _run_icacls(path, '/grant', f'*{sid}:(OI)(CI)(WO)') == 0


def _run_icacls(path: Path, *args: str) -> int:
    """icacls 部分失败时仍可能返回 0，所以调用方一律再用 SACL 复核。"""
    try:
        result = subprocess.run(['icacls', str(path), *args],
                                capture_output=True, text=True,
                                errors='replace', timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return -1
    return result.returncode


def _low_label(path: Path) -> bool:
    """读 SACL，判断对象是否带有 Low 完整性标签（S-1-16-4096）。"""
    sd = ctypes.c_void_p()
    sacl = PACL()
    rc = _advapi32.GetNamedSecurityInfoW(
        str(path), _SE_FILE_OBJECT, _SACL_SECURITY_INFORMATION,
        None, None, None, ctypes.byref(sacl), ctypes.byref(sd))
    if rc != 0 or not sd:
        return False
    try:
        return _sacl_is_low(sd)
    finally:
        _kernel32.LocalFree(sd)


def _sacl_is_low(sd) -> bool:
    present = w.BOOL()
    defaulted = w.BOOL()
    sacl = PACL()
    if not _advapi32.GetSecurityDescriptorSacl(
            sd, ctypes.byref(present), ctypes.byref(sacl), ctypes.byref(defaulted)):
        return False
    if not present or not sacl:
        return False
    acl = ctypes.cast(sacl, ctypes.POINTER(_ACL)).contents
    for index in range(acl.AceCount):
        ace = ctypes.c_void_p()
        if not _advapi32.GetAce(sacl, index, ctypes.byref(ace)) or not ace:
            continue
        header = ctypes.cast(ace, ctypes.POINTER(w.BYTE)).contents
        if header.value != _SYSTEM_MANDATORY_LABEL_ACE:
            continue
        sid = ctypes.cast(ace.value + 8, PSID)   # ACE_HEADER + ACCESS_MASK
        if _sid_text(sid) == _LOW_SID:
            return True
    return False


def _sid_text(sid) -> str:
    out = w.LPWSTR()
    if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(out)):
        return ''
    try:
        return out.value or ''
    finally:
        _kernel32.LocalFree(out)


def _current_user_sid() -> str:
    token = HANDLE()
    if not _advapi32.OpenProcessToken(
            _kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        return ''
    try:
        size = w.DWORD()
        _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or not size.value:
            return ''
        buf = ctypes.create_string_buffer(size.value)
        if not _advapi32.GetTokenInformation(
                token, _TOKEN_USER, buf, size, ctypes.byref(size)):
            return ''
        entry = ctypes.cast(buf, ctypes.POINTER(_SID_AND_ATTRIBUTES)).contents
        return _sid_text(entry.Sid)
    finally:
        _kernel32.CloseHandle(token)


# ============================================================
# 低完整性令牌与进程启动
# ============================================================

def _spawn_sandboxed(command: str, cwd, stdout, workspace):
    ws = Path(workspace or cwd).resolve()
    ready, reason = _ensure_workspace(ws)
    if not ready:
        raise SandboxUnavailable(reason)
    temp_dir = ws / '.cache' / 'sandbox-tmp'
    temp_dir.mkdir(parents=True, exist_ok=True)
    env_block = _environment_block(temp_dir)
    token = _low_token()
    try:
        return _create_process(token, command, cwd, stdout, env_block)
    finally:
        _kernel32.CloseHandle(token)


def _low_token():
    current = HANDLE()
    # 必须带 TOKEN_ADJUST_DEFAULT：CreateRestrictedToken 生成的新句柄权限
    # 取自源句柄；普通 DUPLICATE|QUERY|ASSIGN_PRIMARY 不足以设置完整性级别。
    if not _advapi32.OpenProcessToken(
            _kernel32.GetCurrentProcess(), _MAXIMUM_ALLOWED,
            ctypes.byref(current)):
        raise OSError(ctypes.get_last_error(), 'OpenProcessToken failed')
    try:
        low = HANDLE()
        if not _advapi32.CreateRestrictedToken(
                current, _DISABLE_MAX_PRIVILEGE, 0, None, 0, None, 0, None,
                ctypes.byref(low)):
            raise OSError(ctypes.get_last_error(), 'CreateRestrictedToken failed')
    finally:
        _kernel32.CloseHandle(current)
    try:
        _set_low_integrity(low)
    except Exception:
        _kernel32.CloseHandle(low)
        raise
    return low


def _set_low_integrity(token) -> None:
    low_sid = PSID()
    if not _advapi32.ConvertStringSidToSidW(_LOW_SID, ctypes.byref(low_sid)):
        raise OSError(ctypes.get_last_error(), 'ConvertStringSidToSidW failed')
    try:
        sid_len = _advapi32.GetLengthSid(low_sid)
        size = ctypes.sizeof(_SID_AND_ATTRIBUTES) + sid_len
        buf = ctypes.create_string_buffer(size)
        entry = ctypes.cast(buf, ctypes.POINTER(_SID_AND_ATTRIBUTES)).contents
        entry.Attributes = _SE_GROUP_INTEGRITY
        entry.Sid = ctypes.cast(ctypes.addressof(buf) + ctypes.sizeof(_SID_AND_ATTRIBUTES),
                                PSID)
        ctypes.memmove(entry.Sid, low_sid, sid_len)
        if not _advapi32.SetTokenInformation(
                token, _TOKEN_INTEGRITY_LEVEL, buf, size):
            raise OSError(ctypes.get_last_error(), 'SetTokenInformation failed')
    finally:
        _kernel32.LocalFree(low_sid)


# 环境块按 (TEMP 目录, environ 指纹) 缓存。实测这个函数占每次 spawn
# Python 侧开销的 90%（435µs / 490µs）：遍历 environ、大小写归一、排序、
# 拼字符串都是纯重复劳动，而环境变量在一个进程内几乎不变。
_ENV_BLOCK_CACHE: dict[tuple[str, int], object] = {}
_ENV_BLOCK_LOCK = threading.Lock()
_ENV_BLOCK_MAX = 4


def _environment_fingerprint() -> int:
    """环境变量集合的轻量指纹：只用键名，不复制值。"""
    return hash(frozenset(os.environ))


def _environment_block(temp_dir: Path):
    """继承父环境，只改 TEMP/TMP/TMPDIR；按 Windows 要求大小写不敏感排序。

    返回的缓冲区会被 CreateProcess 只读使用，缓存后重复 spawn 不再重建。
    缓存键带环境指纹：进程内改了环境变量（例如测试 setenv）会自动失效。
    """
    cache_key = (str(temp_dir), _environment_fingerprint())
    with _ENV_BLOCK_LOCK:
        cached = _ENV_BLOCK_CACHE.get(cache_key)
        if cached is not None:
            return cached
    values = {name.upper(): (name, value) for name, value in os.environ.items()
              if name and '=' not in name}
    for name in ('TEMP', 'TMP', 'TMPDIR'):
        values[name] = (name, str(temp_dir))
    text = ''.join(f'{name}={value}\0' for name, value in
                   sorted(values.values(), key=lambda item: item[0].upper()))
    block = ctypes.create_unicode_buffer(text + '\0')
    with _ENV_BLOCK_LOCK:
        if len(_ENV_BLOCK_CACHE) >= _ENV_BLOCK_MAX:
            _ENV_BLOCK_CACHE.clear()
        _ENV_BLOCK_CACHE[cache_key] = block
    return block


def _create_process(token, command: str, cwd, stdout, env_block):
    nul = _open_nul()
    out_handle = HANDLE(msvcrt.get_osfhandle(stdout.fileno()))
    inherited = False
    try:
        if not _kernel32.SetHandleInformation(
                out_handle, _HANDLE_FLAG_INHERIT, _HANDLE_FLAG_INHERIT):
            raise OSError(ctypes.get_last_error(), 'SetHandleInformation failed')
        inherited = True
        si = _STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        si.dwFlags = _STARTF_USESTDHANDLES
        si.hStdInput, si.hStdOutput, si.hStdError = nul, out_handle, out_handle
        comspec = os.environ.get('COMSPEC') or 'cmd.exe'
        cmdline = ctypes.create_unicode_buffer(f'{comspec} /c "{command}"')
        pi = _PROCESS_INFORMATION()
        if not _advapi32.CreateProcessAsUserW(
                token, None, cmdline, None, None, True,
                _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT,
                ctypes.cast(env_block, ctypes.c_void_p), str(cwd),
                ctypes.byref(si), ctypes.byref(pi)):
            raise OSError(ctypes.get_last_error(), 'CreateProcessAsUserW failed')
    finally:
        if inherited:
            _kernel32.SetHandleInformation(out_handle, _HANDLE_FLAG_INHERIT, 0)
        _kernel32.CloseHandle(nul)

    job = _make_job()
    if job and not _kernel32.AssignProcessToJobObject(job, pi.hProcess):
        _kernel32.CloseHandle(job)
        job = None
    if _kernel32.ResumeThread(pi.hThread) == 0xFFFFFFFF:
        error = ctypes.get_last_error()
        _kernel32.TerminateProcess(pi.hProcess, 1)
        _kernel32.CloseHandle(pi.hProcess)
        _kernel32.CloseHandle(pi.hThread)
        if job:
            _kernel32.CloseHandle(job)
        raise OSError(error, 'ResumeThread failed')
    _kernel32.CloseHandle(pi.hThread)
    return SandboxProcess(command, pi.dwProcessId, pi.hProcess, job)


def _open_nul():
    attrs = _SECURITY_ATTRIBUTES()
    attrs.nLength = ctypes.sizeof(attrs)
    attrs.bInheritHandle = True
    handle = _kernel32.CreateFileW(
        'NUL', _GENERIC_READ, _FILE_SHARE_READ_WRITE, ctypes.byref(attrs),
        _OPEN_EXISTING, 0, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), 'CreateFileW(NUL) failed')
    return handle


def _make_job():
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
            ctypes.sizeof(info)):
        _kernel32.CloseHandle(job)
        return None
    return job


class SandboxProcess:
    """Popen 的最小兼容层：pid / poll / wait / returncode，外加 kill_tree / close。

    Job Object 设了 KILL_ON_JOB_CLOSE，close() 之后仍未退出的后代进程会被一并
    终止，避免沙箱里留下无人管理的后台进程。
    """

    def __init__(self, command: str, pid: int, process, job=None) -> None:
        self._command = command
        self.pid = pid
        self._process = process
        self._job = job
        self._returncode = None

    @property
    def returncode(self):
        return self.poll()

    def poll(self):
        if self._process is None:
            return self._returncode
        if self._returncode is None:
            result = _kernel32.WaitForSingleObject(self._process, 0)
            if result == _WAIT_OBJECT_0:
                self._returncode = self._exit_code()
            elif result != _WAIT_TIMEOUT:
                raise OSError(ctypes.get_last_error(), 'WaitForSingleObject failed')
        return self._returncode

    def wait(self, timeout=None):
        if self._returncode is not None:
            return self._returncode
        if self._process is None:
            raise OSError('process handles are already closed')
        millis = _INFINITE if timeout is None else min(
            _INFINITE - 1, max(0, int(timeout * 1000)))
        result = _kernel32.WaitForSingleObject(self._process, millis)
        if result == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self._command, timeout)
        if result != _WAIT_OBJECT_0:
            raise OSError(ctypes.get_last_error(), 'WaitForSingleObject failed')
        self._returncode = self._exit_code()
        return self._returncode

    def _exit_code(self) -> int:
        code = w.DWORD()
        if not _kernel32.GetExitCodeProcess(self._process, ctypes.byref(code)):
            raise OSError(ctypes.get_last_error(), 'GetExitCodeProcess failed')
        return code.value

    def kill_tree(self) -> None:
        if self.poll() is None:
            killed = bool(self._job) and bool(
                _kernel32.TerminateJobObject(self._job, 1))
            if not killed:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(self.pid)],
                               capture_output=True, check=False, timeout=10)
            try:
                self.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kernel32.TerminateProcess(self._process, 1)
                self.wait(timeout=10)
        self.close()

    def close(self) -> None:
        if self._process is not None and self._returncode is None:
            if _kernel32.WaitForSingleObject(self._process, 0) == _WAIT_OBJECT_0:
                self._returncode = self._exit_code()
        if self._job:
            _kernel32.CloseHandle(self._job)
            self._job = None
        if self._process is not None:
            _kernel32.CloseHandle(self._process)
            self._process = None
