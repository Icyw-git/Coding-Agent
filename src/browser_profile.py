"""浏览器 profile 目录：给 MCP 浏览器工具用的隔离登录态。

## 为什么需要单独一个目录

浏览器自动化的核心价值是「已经登录」。如果直接复用日常 Chrome/Edge 的 profile：

1. **cookies 与登录库落进用户日常目录**：一旦工作区指向那里，文件工具的
   「读开放」策略就能扫到（虽然 `checkpoints.DEFAULT_SECRETS` 已按文件名挡住
   Cookies/Login Data，但目录本身仍暴露大量浏览历史）；
2. **同一个 profile 不能同时被两个进程打开**：日常浏览器开着时自动化会启动失败，
   或者更糟——把用户的窗口踢掉；
3. **登录态和日常生活混在一起**：想「撤销 agent 的登录」就得清理日常浏览器。

所以这里给 agent 一个**独立 profile 目录**（默认
`<home>/.cache/browser-profile`），用户在其中登录一次，之后复用那份 cookie。
把它放在 `.cache` 下还有一个附带好处：`.cache` 在 `DEFAULT_IGNORE` 里，检索工具
默认跳过，不会成为「顺手翻出来的历史」。

## 边界

- 这个目录里**就是**登录凭据。不要把它的副本放进工作区，也不要提交到 git。
- 目录权限收紧到当前用户（POSIX 0700；Windows 继承父目录 ACL，不额外放宽）。
- 这里只负责「路径在哪、怎么建」，不负责启动浏览器——浏览器由外部 MCP server
  拉起，通过 `--user-data-dir` 指向这个目录。
"""
from __future__ import annotations

import os
from pathlib import Path

import paths

ENV = 'HARNESS_BROWSER_PROFILE'
DIRNAME = 'browser-profile'

# 我们**不会**替你创建、也不会默认使用的日常 profile 位置。
# 只用来说明「为什么不能直接用它们」以及给出提示。
COMMON_PROFILES = {
    'chrome': ('%LOCALAPPDATA%/Google/Chrome/User Data', None),
    'edge': ('%LOCALAPPDATA%/Microsoft/Edge/User Data', None),
    'firefox': (None, '~/.mozilla/firefox'),
}


class ProfileError(RuntimeError):
    """profile 路径不可用。"""


def profile_dir() -> Path:
    raw = (os.getenv(ENV) or '').strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return paths.cache_path(DIRNAME)


def exists() -> bool:
    return profile_dir().is_dir()


def ensure(*, create: bool = True) -> Path:
    """返回 profile 目录；``create=True`` 时按最紧的权限创建。

    已存在的问题（例如被塞进一个文件）明确报错，而不是让浏览器在后面某处
    神秘失败。
    """
    target = profile_dir()
    if target.exists():
        if not target.is_dir():
            raise ProfileError(f'browser profile path is not a directory: {target}')
        return target
    if not create:
        return target
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProfileError(f'cannot create browser profile: {exc}') from exc
    _harden(target)
    return target


def _harden(target: Path) -> None:
    """只收 POSIX 权限；Windows 上由父目录 ACL 继承，不做额外改动。"""
    if os.name == 'nt':
        return
    try:
        target.chmod(0o700)
    except OSError:
        pass


def is_inside_daily_profile(path=None) -> bool:
    """目标是否落在日常浏览器 profile 里（只做启发式提示，不做拦截）。"""
    target = Path(path) if path is not None else profile_dir()
    text = str(target).replace('\\', '/').casefold()
    markers = ('/google/chrome/user data', '/microsoft/edge/user data',
               '/mozilla/firefox', '/.mozilla/')
    return any(marker in text for marker in markers)


def status() -> dict:
    target = profile_dir()
    result = {
        'path': str(target),
        'exists': target.is_dir(),
        'inside_daily_profile': is_inside_daily_profile(target),
        'overridden': bool((os.getenv(ENV) or '').strip()),
    }
    return result


def describe() -> str:
    state = status()
    if state['inside_daily_profile']:
        return (f"浏览器 profile：{state['path']}（⚠ 指向日常浏览器 profile，"
                '登录态与生活共用，且可能与已开着的浏览器冲突）')
    if not state['exists']:
        return f"浏览器 profile：{state['path']}（未创建；首次使用浏览器工具时创建）"
    return f"浏览器 profile：{state['path']}"


def suggest_args() -> list[str]:
    """给 Chromium 系 MCP server 的 `--user-data-dir` 片段（提示用）。"""
    return [f'--user-data-dir={profile_dir()}']
