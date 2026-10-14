"""Compatibility entrypoint for ``uvicorn api:create_app --factory``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from harness_api import create_app

__all__ = ["create_app"]
