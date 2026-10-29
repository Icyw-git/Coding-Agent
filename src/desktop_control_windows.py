"""Windows foreground-window and input control via PowerShell/user32.

This module is only the OS capability layer.  Policy, approval, scheduled-turn
and subagent restrictions live in hooks.py/exec_policy.py.  The runner is
injectable so tests never move the real mouse or send real keystrokes.

The PowerShell side answers with a single base64(UTF-8 JSON) blob on stdout:
console codepages (GBK on zh-CN hosts) must never touch the payload.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

# PowerShell 源码在 desktop_scripts（1622 行纯文本）；这里只剩 Python 逻辑。
from desktop_scripts import CONTROL_BODY, CONTROL_MAIN, CONTROL_SETUP

#: 独立进程模式的完整脚本。worker 模式在 worker_script() 里另外拼 PRELUDE + LOOP。
CONTROL_SCRIPT = CONTROL_SETUP + CONTROL_BODY + CONTROL_MAIN

DEFAULT_TIMEOUT = 10.0
MAX_TYPE_CHARS = 1000
MAX_SCROLL = 20
MAX_WINDOWS = 100
MAX_SEQUENCE_STEPS = 12
SUPPORTED_KEYS = frozenset({
    'enter', 'return', 'tab', 'escape', 'esc', 'backspace', 'delete',
    'home', 'end', 'left', 'up', 'right', 'down', 'space', 'pageup',
    'pagedown', 'insert',
})
SINGLE_ACTIONS = frozenset({'move', 'click', 'type', 'key', 'scroll'})
ELEMENT_OPERATIONS = frozenset({'invoke', 'setvalue', 'toggle', 'select',
                                'expand', 'collapse', 'scroll', 'focus'})
WAIT_TARGETS = frozenset({'control', 'window', 'foreground'})
WAIT_STATES = frozenset({'present', 'absent'})
# 注意别和序列步骤里的 MIN_WAIT_MS/MAX_WAIT_MS（50~3000）混用：那是单步
# 「等动画」的时长，这里是「等条件成立」的总预算，量级完全不同。
TOOL_WAIT_MIN_MS = 200
TOOL_WAIT_MAX_MS = 30000
TOOL_WAIT_DEFAULT_MS = 5000
MAX_WAIT_TEXT = 200
MAX_ELEMENT_INDEX = 400
#: 控件名片段的上限。它要显示在审批卡上，太长会挤掉别的信息。
MAX_ELEMENT_EXPECT_CHARS = 60
# 界面结构的默认预算。遍历是跨进程调用，元素多的应用（Office 套件、
# 浏览器）能到几千个节点，不设上限就会把一次工具调用拖成几秒。
DEFAULT_TREE_NODES = 120
DEFAULT_TREE_DEPTH = 12
DEFAULT_TREE_MS = 2500
STEP_FIELDS = {
    'move': frozenset({'action', 'x', 'y'}),
    'click': frozenset({'action', 'x', 'y', 'button', 'count'}),
    'type': frozenset({'action', 'text'}),
    'key': frozenset({'action', 'key'}),
    'scroll': frozenset({'action', 'amount'}),
    'wait': frozenset({'action', 'ms'}),
}


KEY_MODIFIERS = frozenset({'ctrl', 'alt', 'shift', 'win'})
CLICK_BUTTONS = frozenset({'left', 'right', 'middle'})
MIN_WAIT_MS = 50
MAX_WAIT_MS = 3000


class DesktopControlError(ValueError):
    """A desktop operation failed or was refused by the OS capability layer."""


@dataclass(frozen=True)
class WindowState:
    available: bool
    title: str = ''
    process: str = ''
    pid: int = 0
    window_id: str = ''
    class_name: str = ''
    rect: tuple[int, int, int, int] | None = None
    focused_name: str = ''
    focused_control_type: str = ''
    focused_is_password: bool = False
    sensitive_target: bool = False
    is_terminal: bool = False
    #: 窗口所在显示器的有效 DPI（96 = 100%）。0 表示读不到。
    dpi: int = 0
    error: str = ''


@dataclass(frozen=True)
class ActionResult:
    action: str
    state: WindowState
    duration_ms: int
    steps: tuple[dict, ...] = ()
    target: dict | None = None
    #: 元素动作之后的界面快照。展开/点击之后最常问的就是"现在有什么"，
    #: 顺手带回来能省掉一次 desktop_inspect（一次往返 + 一次审批）。
    fingerprint_before: str = ''
    fingerprint_after: str = ''
    items_after: tuple[ElementInfo, ...] = ()
    items_after_truncated: bool = False
    items_added: tuple[ElementInfo, ...] = ()
    items_removed: int = 0
    #: 同一个控件在动作之后的样子（勾选/展开状态有没有变）。
    target_after: ElementInfo | None = None
    #: 动作的补充说明（例如"滚动的是外层容器 X"）。
    note: str = ''
    operation: str = ''
    #: 动作之前的窗口状态。工具层用它算「到底变了什么」——只报动作已发出，
    #: 模型无法判断这一步是否生效。
    before: WindowState | None = None


@dataclass(frozen=True)
class ElementInfo:
    """一个可交互控件的快照。"""

    id: int
    label: str
    name: str = ''
    value: str = ''
    password: bool = False
    enabled: bool = True
    focusable: bool = False
    focused: bool = False
    bounds: tuple[int, int, int, int] | None = None
    operations: tuple[str, ...] = ()
    #: checked / unchecked / indeterminate / expanded / collapsed /
    #: partially_expanded / selected / readonly
    state: tuple[str, ...] = ()


@dataclass(frozen=True)
class WaitResult:
    """一次等待的结果。``matched`` 为假表示超时，调用方仍能拿到当前状态。"""

    matched: bool
    target: str
    desired: str
    elapsed_ms: int
    polls: int
    state: WindowState
    element: ElementInfo | None = None
    window: dict | None = None
    fingerprint: str = ''
    elements: int = 0
    windows: int = 0
    truncated: bool = False
    #: 命中的控件是否出现在「完整预算」的那次遍历里。为假说明它落在预算之外，
    #: 编号不能直接拿去做动作（要先 desktop_inspect）。
    authoritative: bool = True


@dataclass(frozen=True)
class UiTree:
    """前台窗口的裁剪控件清单。``fingerprint`` 用于动作前发现结构变化。"""

    items: tuple[ElementInfo, ...]
    truncated: bool
    fingerprint: str
    state: WindowState
    duration_ms: int = 0

    @property
    def total(self) -> int:
        return len(self.items)


# The input payload is base64 in an environment variable.  This avoids quoting
# and console-codepage problems for Chinese text and key names.


def _powershell_exe() -> str | None:
    override = (os.getenv('HARNESS_POWERSHELL') or '').strip()
    return override or shutil.which('powershell') or shutil.which('pwsh')


def run_powershell(script: str, env: dict, timeout: float):
    executable = _powershell_exe()
    if executable is None:
        raise DesktopControlError('desktop control requires Windows PowerShell')
    import ps_worker

    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    # 脚本本身走标准输入：桌面能力的脚本超过 Windows 的 32767 字符命令行上限
    # （实测 WinError 206），而且 stdin 顺带免掉了引号与编码两道坑。
    payload = base64.b64encode(script.encode('utf-8')).decode('ascii') + chr(10)
    return subprocess.run(
        [executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-WindowStyle', 'Hidden', '-Command', ps_worker.BOOTSTRAP],
        input=payload, capture_output=True, text=True, encoding='utf-8',
        timeout=timeout, env={**os.environ, **env}, creationflags=flags,
    )


def _state(value) -> WindowState:
    value = value if isinstance(value, dict) else {}
    rect = value.get('rect')
    try:
        rect = tuple(int(item) for item in rect) if rect is not None else None
    except (TypeError, ValueError):
        rect = None
    return WindowState(
        available=bool(value.get('available')),
        title=str(value.get('title') or ''), process=str(value.get('process') or ''),
        pid=int(value.get('pid') or 0),
        window_id=str(value.get('window_id') or ''),
        class_name=str(value.get('class_name') or ''),
        rect=rect, focused_name=str(value.get('focused_name') or ''),
        focused_control_type=str(value.get('focused_control_type') or ''),
        focused_is_password=bool(value.get('focused_is_password')),
        sensitive_target=bool(value.get('sensitive_target')),
        is_terminal=bool(value.get('is_terminal')),
        dpi=int(value.get('dpi') or 0),
        error=str(value.get('error') or ''),
    )


def tree_limits(environ=None) -> dict:
    """界面结构的预算（节点数 / 深度 / 毫秒）。越界收敛到边界。"""
    import settings

    return {
        'max_nodes': int(settings.number(
            'HARNESS_DESKTOP_TREE_NODES', DEFAULT_TREE_NODES,
            minimum=5, maximum=MAX_ELEMENT_INDEX, integer=True, environ=environ)),
        'max_depth': int(settings.number(
            'HARNESS_DESKTOP_TREE_DEPTH', DEFAULT_TREE_DEPTH,
            minimum=2, maximum=40, integer=True, environ=environ)),
        'max_ms': int(settings.number(
            'HARNESS_DESKTOP_TREE_MS', DEFAULT_TREE_MS,
            minimum=200, maximum=20000, integer=True, environ=environ)),
    }


def excluded_processes(environ=None) -> list[str]:
    """读无障碍树时直接跳过的进程名（病灶 provider 会让遍历卡住）。"""
    import settings

    return settings.csv('HARNESS_DESKTOP_EXCLUDE_PROCESSES', environ=environ)


def _element_info(value, index: int) -> ElementInfo:
    value = value if isinstance(value, dict) else {}
    bounds = value.get('bounds')
    try:
        bounds = tuple(int(part) for part in bounds) if bounds else None
    except (TypeError, ValueError):
        bounds = None
    operations = value.get('operations')
    operations = (tuple(str(item) for item in operations)
                  if isinstance(operations, (list, tuple)) else ())
    state = value.get('state')
    state = (tuple(str(item) for item in state)
             if isinstance(state, (list, tuple)) else ())
    try:
        element_id = int(value.get('id') or index)
    except (TypeError, ValueError):
        element_id = index
    return ElementInfo(
        id=element_id,
        label=str(value.get('label') or '控件'),
        name=str(value.get('name') or ''),
        value=str(value.get('value') or ''),
        password=bool(value.get('password')),
        enabled=bool(value.get('enabled', True)),
        focusable=bool(value.get('focusable')),
        focused=bool(value.get('focused')),
        bounds=bounds,
        operations=operations,
        state=state,
    )


def worker_script() -> str:
    """常驻 worker 用的完整脚本：prelude + 装配 + 请求循环。

    装配（Add-Type 编译）只在进程启动时做一次，之后每次请求只跑
    Invoke-HarnessOp——这是把 882ms/次 降到 0.2ms/次 的关键。
    """
    import ps_worker

    return (ps_worker.PRELUDE + CONTROL_SETUP
            + CONTROL_BODY + ps_worker.LOOP)


def _run_worker(payload: dict, *, timeout: float) -> dict:
    import ps_worker

    reply = ps_worker.run('desktop', worker_script, payload,
                          timeout=max(timeout, ps_worker.DEFAULT_TIMEOUT))
    return reply


def _run(payload: dict, *, runner=None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    # 显式注入 runner（测试）或关闭 worker 时走「每次起进程」的老路径。
    if runner is None and _worker_enabled():
        try:
            return _run_worker(payload, timeout=timeout)
        except ps_worker_error_types() as exc:
            if not _worker_fallback_allowed():
                raise DesktopControlError(str(exc)) from exc
            # 结构化失败（窗口变了、坐标越界）不是 worker 故障：重跑一次独立进程
            # 只会得到同样的结果，还会把可读的 message 换成「unexpected output」。
            # 只有 worker 真的不可用（启动失败/超时/协议错）才降级。
            if _is_worker_failure(exc):
                print(f'  [33m[desktop] worker unavailable, falling back: {exc}[0m')
            else:
                raise DesktopControlError(str(exc)) from exc
    return _run_standalone(payload, runner=runner, timeout=timeout)


def ps_worker_error_types():
    """worker 层可能抛出的异常类型（延迟 import，避免模块级循环）。"""
    import ps_worker

    return ps_worker.WorkerError


# 这些关键字说明是 worker 本身不可用，而不是「请求被正确拒绝」。
_WORKER_FAILURE_HINTS = (
    'not available', 'cannot start', 'did not become ready', 'closed unexpectedly',
    'stdin closed', 'non-JSON', 'not running', 'is unavailable', 'timed out',
    'exited during startup',
)


def _is_worker_failure(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(hint in text for hint in _WORKER_FAILURE_HINTS)


def _worker_enabled() -> bool:
    return (os.getenv('HARNESS_PS_WORKER') or '').strip().casefold() not in (
        '0', 'false', 'off', 'no')


def _worker_fallback_allowed() -> bool:
    return (os.getenv('HARNESS_PS_WORKER_FALLBACK') or '').strip().casefold() not in (
        '0', 'false', 'off', 'no')


def _run_standalone(payload: dict, *, runner=None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    data = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode('ascii')
    runner = runner or run_powershell
    try:
        result = runner(CONTROL_SCRIPT, {'HARNESS_DESKTOP_PAYLOAD': data}, timeout)
    except subprocess.TimeoutExpired as exc:
        raise DesktopControlError(f'desktop control timed out after {timeout:g}s') from exc
    except DesktopControlError:
        raise
    except OSError as exc:
        raise DesktopControlError(f'desktop control unavailable: {exc}') from exc
    stdout = getattr(result, 'stdout', b'') or b''
    if isinstance(stdout, bytes):
        stdout = stdout.decode('ascii', 'replace')
    stderr = getattr(result, 'stderr', b'') or b''
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', 'replace')
    record = None
    try:
        record = json.loads(base64.b64decode(str(stdout).strip(), validate=True).decode('utf-8'))
    except (TypeError, ValueError):
        record = None
    if not isinstance(record, dict):
        raise DesktopControlError(f'unexpected desktop control output: {str(stderr).strip()[-300:]}')
    if getattr(result, 'returncode', 0) or not record.get('ok'):
        raise DesktopControlError(str(record.get('message') or record.get('error') or 'desktop control failed'))
    return record


def snapshot(*, runner=None, timeout: float = DEFAULT_TIMEOUT) -> WindowState:
    return _state(_run({'action': 'snapshot'}, runner=runner, timeout=timeout).get('state'))


def list_windows(*, limit: int = 20, runner=None,
                 timeout: float = DEFAULT_TIMEOUT) -> dict:
    """列出可见顶层窗口。返回 ``{'total': n, 'windows': [...]}``。

    只读，不改变任何状态——给模型一个「先看看有哪些目标」的入口，而不是
    必须靠用户手动切到某个窗口再猜。
    """
    try:
        limit = max(1, min(int(limit), MAX_WINDOWS))
    except (TypeError, ValueError) as exc:
        raise DesktopControlError('limit must be an integer') from exc
    reply = _run({'action': 'list', 'limit': limit}, runner=runner,
                 timeout=timeout)
    windows = []
    for item in reply.get('windows') or []:
        if not isinstance(item, dict):
            continue
        rect = item.get('rect')
        try:
            rect = tuple(int(v) for v in rect) if rect else None
        except (TypeError, ValueError):
            rect = None
        windows.append({
            'title': str(item.get('title') or ''),
            'process': str(item.get('process') or ''),
            'pid': int(item.get('pid') or 0),
            'window_id': str(item.get('window_id') or ''),
            'foreground': bool(item.get('foreground')),
            'minimized': bool(item.get('minimized')),
            'rect': rect,
        })
    return {'total': int(reply.get('total') or len(windows)),
            'windows': windows}


def focus_window(*, expected_pid=None, expected_window_id=None, runner=None,
                 timeout: float = DEFAULT_TIMEOUT) -> WindowState:
    """把指定窗口切到前台。仍需 expected_pid 双因子，避免打到刚被复用的窗口 ID。"""
    payload = validate_action('focus', expected_pid=expected_pid,
                              expected_window_id=expected_window_id)
    reply = _run(payload, runner=runner, timeout=timeout)
    return _state(reply.get('state_after'))


def _validated_key(key) -> str:
    """Normalize one key or a modifier combo such as ``ctrl+shift+s``."""
    if not isinstance(key, str) or not key.strip() or len(key) > 30:
        raise DesktopControlError('key requires a short key name or combo')
    parts = [part.strip().casefold() for part in key.strip().split('+')]
    if any(not part for part in parts):
        raise DesktopControlError('key combo has an empty part')
    *modifiers, base = parts
    if len(modifiers) > 4:
        raise DesktopControlError('key combo allows at most four modifiers')
    for modifier in modifiers:
        if modifier not in KEY_MODIFIERS:
            raise DesktopControlError(f'unsupported modifier: {modifier}')
    if len(set(modifiers)) != len(modifiers):
        raise DesktopControlError('key combo repeats a modifier')
    if base not in SUPPORTED_KEYS and not (
            len(base) == 1 and base.isascii() and base.isalnum()) and not (
            base.startswith('f') and base[1:].isdigit()
            and 1 <= int(base[1:]) <= 12):
        raise DesktopControlError('unsupported key name')
    return '+'.join([*modifiers, base])


def _validated_click(x_value, y_value, button, count) -> dict:
    try:
        coords = {'x': int(x_value), 'y': int(y_value)}
    except (TypeError, ValueError) as exc:
        raise DesktopControlError('click requires integer x and y') from exc
    normalized = {'action': 'click', **coords}
    button = 'left' if button is None else str(button).strip().casefold()
    if button not in CLICK_BUTTONS:
        raise DesktopControlError('button must be left, right, or middle')
    if button != 'left':
        normalized['button'] = button
    count = 1 if count is None else count
    try:
        count = int(count)
    except (TypeError, ValueError) as exc:
        raise DesktopControlError('click count must be 1 or 2') from exc
    if count not in (1, 2):
        raise DesktopControlError('click count must be 1 or 2')
    if count != 1:
        normalized['count'] = count
    return normalized


def _validated_step(step) -> dict:
    """Validate one sequence step; returns the normalized payload for it."""
    if not isinstance(step, dict):
        raise DesktopControlError('each sequence step must be an object')
    action = str(step.get('action') or '').strip().casefold()
    if action not in STEP_FIELDS:
        raise DesktopControlError(
            'each step needs action: move, click, type, key, scroll, or wait')
    unknown = set(step) - STEP_FIELDS[action]
    if unknown:
        raise DesktopControlError(
            f"{action} step has unknown fields: {', '.join(sorted(unknown))}")
    if action == 'click':
        return _validated_click(step.get('x'), step.get('y'),
                                step.get('button'), step.get('count'))
    if action == 'move':
        try:
            coords = {'x': int(step['x']), 'y': int(step['y'])}
        except (KeyError, TypeError, ValueError) as exc:
            raise DesktopControlError('move step requires integer x and y') from exc
        return {'action': action, **coords}
    if action == 'type':
        text = step.get('text')
        if not isinstance(text, str) or len(text) > MAX_TYPE_CHARS:
            raise DesktopControlError(
                f'type step text must be a string of at most {MAX_TYPE_CHARS} characters')
        return {'action': action, 'text': text}
    if action == 'key':
        return {'action': action, 'key': _validated_key(step.get('key'))}
    if action == 'wait':
        try:
            ms = int(step.get('ms'))
        except (TypeError, ValueError) as exc:
            raise DesktopControlError('wait step requires integer ms') from exc
        if not MIN_WAIT_MS <= ms <= MAX_WAIT_MS:
            raise DesktopControlError(
                f'wait ms must be between {MIN_WAIT_MS} and {MAX_WAIT_MS}')
        return {'action': action, 'ms': ms}
    try:
        amount = int(step.get('amount'))
    except (TypeError, ValueError) as exc:
        raise DesktopControlError(
            'scroll step requires a non-zero integer amount') from exc
    if amount == 0 or abs(amount) > MAX_SCROLL:
        raise DesktopControlError(
            f'scroll amount must be between -{MAX_SCROLL} and {MAX_SCROLL}, excluding 0')
    return {'action': action, 'amount': amount}


def _validated_identity(expected_pid, expected_window_id) -> tuple[int, str]:
    """pid + 窗口句柄双因子。

    只要动作会改变某个窗口的状态，就必须同时说清是哪个进程的哪个窗口：
    单靠 pid 会在多窗口应用里打错窗口，单靠标题对同标题窗口完全无效。
    """
    try:
        pid = int(expected_pid)
    except (TypeError, ValueError) as exc:
        raise DesktopControlError(
            'expected_pid is required; get it from desktop_snapshot') from exc
    if pid <= 0:
        raise DesktopControlError('expected_pid must be a positive process id')
    if not isinstance(expected_window_id, str) or not expected_window_id.strip():
        raise DesktopControlError(
            'expected_window_id is required; get it from desktop_snapshot')
    window_id = expected_window_id.strip().upper()
    if len(window_id) > 32 or any(ch not in '0123456789ABCDEF' for ch in window_id):
        raise DesktopControlError(
            'expected_window_id must be the hexadecimal id from desktop_snapshot')
    return pid, window_id


def validate_element_action(operation, element, tree, *,
                            expected_pid=None, expected_window_id=None,
                            text=None, expect=None, amount=None,
                            allow_terminal=None) -> dict:
    """校验元素级动作的参数。

    ``tree`` 是 ``desktop_inspect`` 给的结构指纹，**必填**：界面在「看一眼」
    和「点一下」之间变了，旧编号会指向另一个控件，宁可让模型重新看一眼。

    ``expect`` 是可选的控件名片段，做两件事：审批卡上写清楚「要点的是哪个控件」
    （否则用户只看到一个编号，等于没有知情同意）；执行前在 PowerShell 侧核对
    实际控件名，对不上就拒绝——指纹只保证结构没变，保证不了编号没被写错。
    """
    expected = ' '.join(str(expect or '').split())
    if len(expected) > MAX_ELEMENT_EXPECT_CHARS:
        raise DesktopControlError(
            f'expect must be at most {MAX_ELEMENT_EXPECT_CHARS} characters')
    pid, window_id = _validated_identity(expected_pid, expected_window_id)
    op = str(operation or '').strip().casefold() or 'invoke'
    if op not in ELEMENT_OPERATIONS:
        raise DesktopControlError(
            'operation must be invoke, setvalue, toggle, select, expand, '
            'collapse, scroll, or focus')
    try:
        index = int(element)
    except (TypeError, ValueError) as exc:
        raise DesktopControlError(
            'element must be the number from desktop_inspect') from exc
    if index < 1 or index > MAX_ELEMENT_INDEX:
        raise DesktopControlError('element must be between 1 and '
                                  f'{MAX_ELEMENT_INDEX}')
    fingerprint = str(tree or '').strip().lower()
    if len(fingerprint) < 8 or len(fingerprint) > 64 or any(
            ch not in '0123456789abcdef' for ch in fingerprint):
        raise DesktopControlError(
            'tree must be the fingerprint desktop_inspect returned')
    # 遍历预算必须和 desktop_inspect 完全一致：指纹是「编号 -> 类型/名字/位置」
    # 的哈希，节点数或深度不同哈希就不同。不带这几个值的话，用户调过
    # HARNESS_DESKTOP_TREE_NODES 之后，动作会一直误报 tree_changed。
    # 上限值随载荷下发：PS 侧的钳位不再写死 20，避免"两处各有一个 20"。
    payload = {'action': 'element', 'expected_pid': pid,
               'expected_window_id': window_id, 'tree': fingerprint,
               'element': index, 'operation': op, 'max_scroll': MAX_SCROLL,
               **tree_limits()}
    excluded = excluded_processes()
    if excluded:
        payload['exclude'] = excluded
    if expected:
        payload['expect'] = expected
    # 和物理输入用同一个显式开关：`setvalue` 在 terminal 类窗口上同样会被拦。
    # 判据是「窗口属于 powershell/cmd 进程」，所以用脚本起的 GUI 程序也会命中，
    # 没有这个开关就完全没法往里面写值。
    payload['allow_terminal'] = bool(allow_terminal)
    if op == 'setvalue':
        if not isinstance(text, str) or not text:
            raise DesktopControlError('setvalue requires text')
        if len(text) > MAX_TYPE_CHARS:
            raise DesktopControlError(
                f'setvalue text must be at most {MAX_TYPE_CHARS} characters')
        payload['text'] = text
    elif text is not None:
        raise DesktopControlError('text is only valid with operation=setvalue')
    if op == 'scroll':
        try:
            steps = int(amount)
        except (TypeError, ValueError) as exc:
            raise DesktopControlError(
                'operation=scroll requires amount (positive scrolls down)') from exc
        if steps == 0 or abs(steps) > MAX_SCROLL:
            raise DesktopControlError(
                f'scroll amount must be between -{MAX_SCROLL} and {MAX_SCROLL}, '
                'excluding 0')
        payload['amount'] = steps
    elif amount is not None:
        raise DesktopControlError('amount is only valid with operation=scroll')
    return payload


def validate_action(action: str, *, expected_pid=None, expected_window_id=None,
                    x=None, y=None, text=None, key=None, amount=None,
                    button=None, count=None, steps=None,
                    allow_terminal=None) -> dict:
    action = str(action or '').strip().casefold()
    expected_pid, expected_window_id = _validated_identity(
        expected_pid, expected_window_id)
    if action == 'sequence':
        if any(value is not None for value in (x, y, text, key, amount, button, count)):
            raise DesktopControlError(
                'sequence takes steps only; put x/y/text/key/amount/button/count '
                'inside each step')
        if not isinstance(steps, (list, tuple)) or not steps:
            raise DesktopControlError('sequence requires a non-empty steps list')
        if len(steps) > MAX_SEQUENCE_STEPS:
            raise DesktopControlError(f'sequence is limited to {MAX_SEQUENCE_STEPS} steps')
        return {'action': 'sequence', 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id,
                'steps': [_validated_step(step) for step in steps]}
    if steps is not None:
        raise DesktopControlError("steps is only valid with action='sequence'")
    if action == 'focus':
        # 切窗口只需要双因子身份，没有坐标/文本参数。
        return {'action': 'focus', 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id}
    if action not in SINGLE_ACTIONS:
        raise DesktopControlError(
            'action must be move, click, type, key, scroll, sequence, or focus')
    # 显式放行终端输入必须由调用方逐次声明，不能隐式带上。
    terminal_ok = bool(allow_terminal)
    if action != 'click' and (button is not None or count is not None):
        raise DesktopControlError('button and count are only valid with click')
    if action == 'move':
        try:
            coords = {'x': int(x), 'y': int(y)}
        except (TypeError, ValueError) as exc:
            raise DesktopControlError('move requires integer x and y') from exc
        return {'action': action, 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id,
                'allow_terminal': terminal_ok, **coords}
    if action == 'click':
        return {'expected_pid': expected_pid,
                'expected_window_id': expected_window_id,
                **_validated_click(x, y, button, count)}
    if action == 'type':
        if not isinstance(text, str) or len(text) > MAX_TYPE_CHARS:
            raise DesktopControlError(f'type text must be a string of at most {MAX_TYPE_CHARS} characters')
        return {'action': action, 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id,
                'allow_terminal': terminal_ok, 'text': text}
    if action == 'key':
        return {'action': action, 'expected_pid': expected_pid,
                'expected_window_id': expected_window_id,
                'key': _validated_key(key)}
    try:
        amount = int(amount)
    except (TypeError, ValueError) as exc:
        raise DesktopControlError('scroll requires a non-zero integer amount') from exc
    if amount == 0 or abs(amount) > MAX_SCROLL:
        raise DesktopControlError(f'scroll amount must be between -{MAX_SCROLL} and {MAX_SCROLL}, excluding 0')
    return {'action': action, 'expected_pid': expected_pid,
            'expected_window_id': expected_window_id,
            'allow_terminal': terminal_ok, 'amount': amount}


def action(action: str, *, expected_pid=None, expected_window_id=None, x=None,
           y=None, text=None, key=None, amount=None, button=None, count=None,
           steps=None, allow_terminal=None, runner=None,
           timeout: float = DEFAULT_TIMEOUT) -> ActionResult:
    payload = validate_action(action, expected_pid=expected_pid,
                              expected_window_id=expected_window_id, x=x, y=y,
                              text=text, key=key, amount=amount,
                              button=button, count=count, steps=steps,
                              allow_terminal=allow_terminal)
    if payload['action'] == 'sequence':
        wait_s = sum(step.get('ms', 0) for step in payload['steps']) / 1000.0
        timeout = min(60.0, max(
            timeout, DEFAULT_TIMEOUT + 2.0 * len(payload['steps']) + wait_s))
    started = time.monotonic()
    record = _run(payload, runner=runner, timeout=timeout)
    step_records = tuple(item for item in (record.get('steps') or [])
                         if isinstance(item, dict))
    before = record.get('state_before')
    return ActionResult(
        action=payload['action'], state=_state(record.get('state_after')),
        steps=step_records,
        duration_ms=int((time.monotonic() - started) * 1000),
        before=_state(before) if isinstance(before, dict) else None,
    )


def inspect(*, runner=None, timeout: float | None = None) -> UiTree:
    """读取前台窗口的裁剪控件清单（只读，不改变任何状态）。"""
    limits = tree_limits()
    payload = {'action': 'inspect', **limits}
    excluded = excluded_processes()
    if excluded:
        payload['exclude'] = excluded
    if timeout is None:
        # 遍历自带毫秒预算，外层超时要比它宽，否则先被 Python 掐断、
        # 拿不到「已采集部分」这个有意义的结果。
        timeout = max(DEFAULT_TIMEOUT, limits['max_ms'] / 1000.0 + 5.0)
    reply = _run(payload, runner=runner, timeout=timeout)
    raw_items = reply.get('items')
    items = tuple(_element_info(item, index)
                  for index, item in enumerate(raw_items or (), 1)
                  if isinstance(item, dict))
    return UiTree(
        items=items,
        truncated=bool(reply.get('truncated')),
        fingerprint=str(reply.get('fingerprint') or ''),
        state=_state(reply.get('state')),
        duration_ms=int(reply.get('duration_ms') or 0),
    )


def element_action(operation, element, tree, *, expected_pid=None,
                   expected_window_id=None, text=None, expect=None,
                   amount=None, allow_terminal=None, forbidden=None, runner=None,
                   timeout: float = DEFAULT_TIMEOUT) -> ActionResult:
    """按编号操作控件（Invoke/SetValue/...），不移动鼠标。

    ``forbidden`` 是控件名模式表（由策略层给，例如第四档 MUST_DO_SELF）：
    命中就拒绝，且不进审批——这类事只能用户自己点。
    """
    payload = validate_element_action(
        operation, element, tree, expected_pid=expected_pid,
        expected_window_id=expected_window_id, text=text, expect=expect,
        amount=amount, allow_terminal=allow_terminal)
    if forbidden is None:
        # 默认从策略层取，而不是「调用方忘了传就当没有」：这一档是「只能用户
        # 自己做」，漏掉的代价比多拦一次大得多。显式传 [] 才表示「策略说没有」。
        import never_do

        forbidden = never_do.ui_forbidden_patterns()
    if forbidden:
        payload['forbidden'] = [str(item)[:200] for item in forbidden][:20]
    started = time.monotonic()
    reply = _run(payload, runner=runner, timeout=timeout)
    target = reply.get('target')
    after_items = tuple(
        _element_info(item, index)
        for index, item in enumerate(reply.get('items_after') or (), 1)
        if isinstance(item, dict))
    before = reply.get('state_before')
    return ActionResult(
        action='element',
        state=_state(reply.get('state_after')),
        duration_ms=int((time.monotonic() - started) * 1000),
        target=_element_info(target, 0) if isinstance(target, dict) else None,
        before=_state(before) if isinstance(before, dict) else None,
        fingerprint_before=str(reply.get('fingerprint_before') or ''),
        fingerprint_after=str(reply.get('fingerprint_after') or ''),
        items_after=after_items,
        items_after_truncated=bool(reply.get('items_after_truncated')),
        items_added=tuple(
            _element_info(item, index)
            for index, item in enumerate(reply.get('items_added') or (), 1)
            if isinstance(item, dict)),
        items_removed=int(reply.get('items_removed') or 0),
        target_after=(_element_info(reply['target_after'], 0)
                      if isinstance(reply.get('target_after'), dict) else None),
        note=str(reply.get('note') or ''),
        operation=str(reply.get('operation') or ''),
    )


def validate_wait(target='control', text=None, state='present', timeout_ms=None,
                  *, expected_pid=None, expected_window_id=None) -> dict:
    """校验等待条件。没有任何条件可等的「等」会白烧时间，直接拒绝。"""
    kind = str(target or 'control').strip().casefold()
    if kind not in WAIT_TARGETS:
        raise DesktopControlError(
            "target must be 'control', 'window', or 'foreground'")
    desired = str(state or 'present').strip().casefold()
    if desired not in WAIT_STATES:
        raise DesktopControlError("state must be 'present' or 'absent'")
    needle = ' '.join(str(text or '').split())
    if not needle:
        raise DesktopControlError('text is required: say what to wait for')
    if len(needle) > MAX_WAIT_TEXT:
        raise DesktopControlError(
            f'text must be at most {MAX_WAIT_TEXT} characters')
    try:
        wait_ms = int(timeout_ms if timeout_ms is not None
                      else TOOL_WAIT_DEFAULT_MS)
    except (TypeError, ValueError) as exc:
        raise DesktopControlError('timeout_ms must be an integer') from exc
    if wait_ms < TOOL_WAIT_MIN_MS or wait_ms > TOOL_WAIT_MAX_MS:
        raise DesktopControlError(
            f'timeout_ms must be between {TOOL_WAIT_MIN_MS} and {TOOL_WAIT_MAX_MS}')
    payload = {'action': 'wait', 'target': kind, 'state': desired,
               'text': needle, 'timeout_ms': wait_ms, **tree_limits()}
    if expected_pid is not None or expected_window_id is not None:
        pid, window_id = _validated_identity(expected_pid, expected_window_id)
        payload['expected_pid'] = pid
        payload['expected_window_id'] = window_id
    if kind == 'control':
        excluded = excluded_processes()
        if excluded:
            payload['exclude'] = excluded
    return payload


def wait(target='control', text=None, state='present', timeout_ms=None, *,
         expected_pid=None, expected_window_id=None, runner=None,
         timeout: float | None = None) -> WaitResult:
    """轮询等待一个条件成立（控件出现/消失、窗口出现/消失）。

    轮询在 PowerShell 侧完成：一次工具调用顶之前「sleep + 重新快照」的好几轮
    往返，而且每轮的证据都是同一套裁剪逻辑产出的，判据和 desktop_inspect 一致。
    """
    payload = validate_wait(target, text, state, timeout_ms,
                            expected_pid=expected_pid,
                            expected_window_id=expected_window_id)
    if timeout is None:
        timeout = payload['timeout_ms'] / 1000.0 + 8.0
    reply = _run(payload, runner=runner, timeout=timeout)
    result = reply.get('result')
    result = result if isinstance(result, dict) else {}
    element = result.get('element')
    window = result.get('window')
    return WaitResult(
        matched=bool(result.get('matched')),
        target=str(result.get('target') or payload['target']),
        desired=payload['state'],
        elapsed_ms=int(result.get('elapsed_ms') or 0),
        polls=int(result.get('polls') or 0),
        state=_state(result.get('state')),
        element=_element_info(element, 0) if isinstance(element, dict) else None,
        window=window if isinstance(window, dict) else None,
        fingerprint=str(result.get('fingerprint') or ''),
        elements=int(result.get('elements') or 0),
        windows=int(result.get('windows') or 0),
        authoritative=bool(result.get('authoritative', True)),
        truncated=bool(result.get('tree_truncated')),
    )


@dataclass(frozen=True)
class DisplayInfo:
    """一块显示器的物理矩形与 DPI。"""

    name: str
    primary: bool
    bounds: tuple[int, int, int, int]
    work: tuple[int, int, int, int]
    dpi_x: int = 96
    dpi_y: int = 96

    @property
    def scale(self) -> float:
        return round(self.dpi_x / 96.0, 2)


@dataclass(frozen=True)
class DisplaysResult:
    displays: tuple[DisplayInfo, ...]
    virtual_screen: tuple[int, int, int, int] | None
    foreground_index: int
    foreground_dpi: int
    state: WindowState


def _rect(value) -> tuple[int, int, int, int] | None:
    try:
        parts = tuple(int(part) for part in value)
    except (TypeError, ValueError):
        return None
    return parts if len(parts) == 4 else None


def displays(*, runner=None, timeout: float = DEFAULT_TIMEOUT) -> DisplaysResult:
    """每块显示器的物理矩形 / 工作区 / DPI，以及前台窗口落在哪块上。

    进程是 DPI-aware 的，所以这里的矩形是**物理像素**，与截图和
    ``desktop_action`` 同一套坐标；不带 DPI 感知的进程看到的会是逻辑像素
    （150% 缩放下差 1.5 倍），那正是点偏位置最常见的成因。
    """
    reply = _run({'action': 'displays'}, runner=runner, timeout=timeout)
    items = []
    for entry in reply.get('displays') or []:
        if not isinstance(entry, dict):
            continue
        bounds = _rect(entry.get('bounds'))
        work = _rect(entry.get('work')) or bounds
        if bounds is None:
            continue
        items.append(DisplayInfo(
            name=str(entry.get('name') or f'DISPLAY{len(items) + 1}'),
            primary=bool(entry.get('primary')),
            bounds=bounds, work=work,
            dpi_x=int(entry.get('dpi_x') or 96),
            dpi_y=int(entry.get('dpi_y') or 96),
        ))
    return DisplaysResult(
        displays=tuple(items),
        virtual_screen=_rect(reply.get('virtual_screen')),
        foreground_index=int(reply.get('foreground_index') or 0),
        foreground_dpi=int(reply.get('foreground_dpi') or 0),
        state=_state(reply.get('foreground')),
    )


def state_dict(state: WindowState) -> dict:
    return {
        'available': state.available, 'title': state.title, 'process': state.process,
        'pid': state.pid, 'window_id': state.window_id,
        'class_name': state.class_name,
        'rect': list(state.rect) if state.rect is not None else None,
        'focused_name': state.focused_name,
        'focused_control_type': state.focused_control_type,
        'focused_is_password': state.focused_is_password,
        'sensitive_target': state.sensitive_target,
        'is_terminal': state.is_terminal,
        'dpi': state.dpi,
        **({'error': state.error} if state.error else {}),
    }
