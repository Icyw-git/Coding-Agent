"""Compatibility entrypoint for ``uvicorn api:create_app --factory``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import console
from harness_api import create_app

# 早于任何日志/回合执行：GBK 控制台或重定向日志遇到 emoji 不能抛异常。
console.harden_stdio()

__all__ = ["create_app"]
