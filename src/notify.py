"""Windows 本地 Toast：飞书断线时的兜底提醒。

通知属于「输出」而不是「行动」，因此不给模型工具入口，只在服务层调用。
所有失败都收敛成返回值，绝不抛给回合/网关主流程。限频按「尝试」计：
10 秒内最多一次，避免网关批量失败时刷屏。

实现走 ``powershell -EncodedCommand``：命令用 UTF-16LE + base64，Toast XML 也
先 base64 再嵌进脚本，绕开 Windows PowerShell 5.1 在 GBK 控制台下解析中文/引号的
问题。``HARNESS_NOTIFY=0`` 可整体关闭。
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import threading
import time
from xml.sax.saxutils import escape

ENV = 'HARNESS_NOTIFY'
PLATFORM = sys.platform
MIN_INTERVAL = 10.0
TIMEOUT = 15.0
MAX_TITLE = 80
MAX_BODY = 400

# 已在本机验证可用的 AUMID：无包身份的传统桌面进程借用 Windows PowerShell 的标识
AUMID = ('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}'
         r'\WindowsPowerShell\v1.0\powershell.exe')

_OFF = {'0', 'false', 'no', 'off'}
_SILENT_LEVELS = {'info', 'success'}
_LEVELS = {'info', 'success', 'warn', 'error'}

_lock = threading.Lock()
_last_attempt: float | None = None


def _unavailable() -> str | None:
    raw = (os.getenv(ENV) or '').strip().casefold()
    if raw in _OFF:
        return 'disabled'
    if PLATFORM != 'win32':
        return 'unsupported'
    return None


def enabled() -> bool:
    return _unavailable() is None


def reset() -> None:
    """清空限频状态（测试用）。"""
    global _last_attempt
    with _lock:
        _last_attempt = None


def _clean(text, limit: int) -> str:
    # 先丢弃代理项，否则 XML 1.0 遇到孤立 surrogate 会解析失败
    raw = str(text or '').encode('utf-8', 'replace').decode('utf-8')
    raw = ''.join(ch for ch in raw if ch in '\t\n' or ord(ch) >= 0x20)
    raw = raw.strip()
    if len(raw) > limit:
        raw = raw[:limit - 1] + '…'
    return raw


# 只接受这几类启动目标：飞书（https）、本机 http（控制台/本地页面）。
# 刻意不做「任意 scheme」——file://、ms-* 或自定义协议都可能被滥用成打开
# 本地程序，而这条路径的输入来自错误信息，不可信。
_ALLOWED_PREFIXES = ('https://', 'http://localhost', 'http://127.0.0.1')
_CONTROL_CHARS = frozenset(chr(code) for code in (0, 10, 13))


def _safe_launch(url: str | None) -> str | None:
    """校验并归一化 Toast 的启动目标；不合法返回 None（退化成不可点击）。

    ``&`` 在 XML 属性里必须转义，否则整条 Toast 失效——这是最常见的坑，
    所以这里走 ``escape()`` 而不是拼字符串。
    """
    text = str(url or '').strip()
    if not text:
        return None
    if not any(text.casefold().startswith(prefix) for prefix in _ALLOWED_PREFIXES):
        return None
    if any(ch in _CONTROL_CHARS for ch in text):
        return None
    return text


def _toast_xml(title: str, body: str, level: str,
               launch: str | None = None) -> str:
    target = _safe_launch(launch)
    # activationType=protocol 让点击「打开链接」而不是「打开某个应用」。
    # 没有合法目标时不写这两个属性，Toast 退化成纯提示。
    head = (f'<toast activationType="protocol" launch="{escape(target)}">'
            if target else '<toast>')
    parts = [
        head,
        '<visual><binding template="ToastGeneric">',
        f'<text>{escape(title)}</text>',
    ]
    if body:
        parts.append(f'<text>{escape(body)}</text>')
    parts.append('</binding></visual>')
    if level in _SILENT_LEVELS:
        parts.append('<audio silent="true"/>')
    parts.append('</toast>')
    return ''.join(parts)


def _build_script(xml_text: str) -> str:
    payload = base64.b64encode(xml_text.encode('utf-8')).decode('ascii')
    return '\n'.join([
        "$ErrorActionPreference = 'Stop'",
        '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, '
        'ContentType = WindowsRuntime] | Out-Null',
        '[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, '
        'ContentType = WindowsRuntime] | Out-Null',
        '[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, '
        'ContentType = WindowsRuntime] | Out-Null',
        f"$xmlText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))",
        '$xml = New-Object Windows.Data.Xml.Dom.XmlDocument',
        '$xml.LoadXml($xmlText)',
        '$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)',
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{AUMID}').Show($toast)",
        "'ok'",
    ])


def _encode(script: str) -> str:
    return base64.b64encode(script.encode('utf-16-le')).decode('ascii')


def _run(command: list[str], timeout: float):
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    return subprocess.run(command, capture_output=True, encoding='utf-8',
                          errors='replace', timeout=timeout, check=False, **kwargs)


def _command(script: str) -> list[str]:
    return ['powershell.exe', '-NoProfile', '-NonInteractive',
            '-EncodedCommand', _encode(script)]


def notify(title, body='', *, level='info', url=None,
           runner=None) -> dict:
    """弹一条本地 Toast；返回结果字典，不抛异常。

    ``runner(command, timeout)`` 可注入，需返回带 ``returncode``/``stdout``/
    ``stderr`` 的对象（类似 ``subprocess.CompletedProcess``）。
    """
    level = str(level or 'info').strip().casefold()
    if level not in _LEVELS:
        level = 'info'
    title = _clean(title, MAX_TITLE) or 'Harness Agent'
    body = _clean(body, MAX_BODY)

    reason = _unavailable()
    if reason is not None:
        return {'sent': False, 'reason': reason, 'level': level}

    global _last_attempt
    now = time.monotonic()
    with _lock:
        if _last_attempt is not None and now - _last_attempt < MIN_INTERVAL:
            return {'sent': False, 'reason': 'rate_limited', 'level': level,
                    'retry_in': round(MIN_INTERVAL - (now - _last_attempt), 1)}
        _last_attempt = now

    command = _command(_build_script(_toast_xml(title, body, level, url)))
    runner = runner or _run
    try:
        completed = runner(command, TIMEOUT)
    except subprocess.TimeoutExpired:
        return {'sent': False, 'reason': 'timeout', 'level': level}
    except OSError as exc:
        return {'sent': False, 'reason': 'failed', 'level': level,
                'error': f'{type(exc).__name__}: {exc}'[:200]}

    code = getattr(completed, 'returncode', 1)
    if code != 0:
        error = str(getattr(completed, 'stderr', '') or '').strip()[-200:]
        return {'sent': False, 'reason': 'failed', 'level': level,
                'exit_code': code, 'error': error or f'exit {code}'}
    return {'sent': True, 'level': level}
