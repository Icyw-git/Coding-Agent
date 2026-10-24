"""Agent 数据目录：把「agent home」与「工作区」分开。

问题背景：``.cache/sessions`` 曾经跟着 ``Path.cwd()`` 走，于是在 A 目录启动的 agent
看不到在 B 目录启动时保存的会话，记忆、快照、工具结果同样各复制一份。工具在哪个
目录里跑（workspace）和 agent 把历史存在哪里（home）是两件事。

- home：``HARNESS_HOME``，默认是仓库根（``harness.py`` 所在目录）；
- workspace：仍然是 ``Path.cwd()`` / ``use_workspace``，只影响工具执行与项目技能；
- 派生缓存（sessions/memories/checkpoints/…）一律放在 ``<home>/.cache``。

显式传 ``HARNESS_HOME`` 可以让多个工作区共用一个 home，或者反过来做隔离。
"""
from __future__ import annotations

import os
from pathlib import Path

ENV = 'HARNESS_HOME'


def app_dir() -> Path:
    """harness 安装/检出目录（含 src、harness.py）。"""
    return Path(__file__).resolve().parents[1]


def home_dir() -> Path:
    raw = (os.getenv(ENV) or '').strip()
    base = Path(raw).expanduser() if raw else app_dir()
    return base.resolve()


def cache_dir() -> Path:
    return home_dir() / '.cache'


def cache_path(*parts: str) -> Path:
    return cache_dir().joinpath(*parts)


def describe() -> str:
    """诊断用的一行路径说明（--doctor / 启动提示）。"""
    source = 'HARNESS_HOME' if (os.getenv(ENV) or '').strip() else 'repo root'
    return f'home={home_dir()} ({source}) · cache={cache_dir()}'
