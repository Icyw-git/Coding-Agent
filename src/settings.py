"""配置读取的唯一入口。

为什么需要这一层
----------------
代码里有 79 个 ``HARNESS_*`` 变量、138 处 ``os.getenv`` 调用。问题不是"散"——
参数在调用时读取是**有意为之**的语义（见下），问题在于：

1. **默认值写了两遍**：模块里一次、``docs/ENVIRONMENT.md`` 一次（人肉核对，会漂移）。
2. **非法值处理各写一遍**：每个模块自己 ``try/except ValueError``，收敛规则不一致
   （有的回默认、有的报错、有的静默截断）。
3. **没有统一的"改配置"接口**：将来真要接配置文件，得改 138 个调用点。

这一层只解决这三件事，**不改变读取机制**。

为什么不缓存
------------
``/sandbox`` 换档靠 ``set_tier()`` 直接写 ``os.environ``，``load_policy()`` 即读即用，
所以改完下一条工具调用就生效。任何"启动时加载一次配置对象"的设计都会破坏这个
语义。因此这里的函数**每次调用都读环境**——比 dict 查找只慢几微秒，但保住了正确性。

安全档位不在这里
----------------
``HARNESS_SANDBOX_MODE`` / ``HARNESS_APPROVAL_POLICY`` / ``HARNESS_APPROVAL_ALL``
**故意不提供 setter**，也**不提供从 .env 读取的便利**：项目根的 ``.env`` 就在工作区
里，``write`` 在默认档免审，Agent 改一行就能把下次启动的权限降到 never。它们的
可信来源是进程启动环境，由 ``harness.enforce_trusted_policy_env()`` 保证。本模块
只提供**只读**的 ``policy_env()``。
"""
from __future__ import annotations

import os
from typing import Mapping

#: 布尔真值表。大小写不敏感。
_TRUE = frozenset({'1', 'true', 'yes', 'on', 'enable', 'enabled'})
_FALSE = frozenset({'0', 'false', 'no', 'off', 'disable', 'disabled'})

#: 只认进程启动环境的键（不能来自 .env / yaml）。
POLICY_KEYS = frozenset({
    'HARNESS_SANDBOX_MODE',
    'HARNESS_APPROVAL_POLICY',
    'HARNESS_APPROVAL_ALL',
})


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _raw(name: str, environ: Mapping[str, str] | None) -> str:
    return str(_env(environ).get(name) or '').strip()


def flag(name: str, default: bool = False, *,
         environ: Mapping[str, str] | None = None) -> bool:
    """布尔开关。

    无法识别的值**回落到 ``default``** 而不是报错：配置写错不该让一个已经批准的
    操作失败。需要"写错必须提示"的地方自行比较 ``_raw`` 后打印（见 ``exec_policy``）。
    """
    text = _raw(name, environ).casefold()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def number(name: str, default: float, *, minimum: float | None = None,
           maximum: float | None = None, integer: bool = False,
           environ: Mapping[str, str] | None = None) -> float | int:
    """数值参数。越界**收敛到边界**，非数字回落 ``default``。

    收敛而不是报错：``HARNESS_APPROVAL_TIMEOUT=0`` 这类手误应该退回默认档并在
    stderr 提示一次（调用方负责提示），而不是让服务起不来。
    """
    text = _raw(name, environ)
    if not text:
        value = float(default)
    else:
        try:
            value = int(text) if integer else float(text)
        except ValueError:
            value = float(default)
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return int(value) if integer else value


def text(name: str, default: str = '', *,
         environ: Mapping[str, str] | None = None) -> str:
    """字符串参数。空串视为未设置（环境变量设成空等于没设，与既有语义一致）。"""
    value = _raw(name, environ)
    return value if value else default


def csv(name: str, *, environ: Mapping[str, str] | None = None) -> list[str]:
    """逗号分隔列表，去空去重保序。"""
    raw = _raw(name, environ)
    if not raw:
        return []
    seen: list[str] = []
    for part in raw.replace(';', ',').split(','):
        item = part.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def policy_env(name: str, *, environ: Mapping[str, str] | None = None) -> str:
    """只读安全档位。

    刻意没有对应的 setter：档位由 ``exec_policy.set_tier()``（运行时命令）或进程
    启动环境决定，后者经 ``harness`` 快照保护。绕过这两条路改档位等于提权。
    """
    if name not in POLICY_KEYS:
        raise ValueError(f'{name} is not a policy key')
    return _raw(name, environ)


def describe(names) -> list[dict]:
    """给 ``/status`` 与排查用：列出某个前缀下实际生效的值。

    只返回**已设置**的项——把 79 个默认值全铺出来很难看出哪一项被改过。
    """
    environ = os.environ
    out = []
    for name in names:
        value = _raw(name, environ)
        if value:
            out.append({'name': name, 'value': value})
    return out
