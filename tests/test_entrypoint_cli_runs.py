"""README 说 `python entrypoint.py` 就能起，且在任意目录都行。

pytest 的 `pythonpath = . src tests` 会**掩盖**这类问题：测试里 `import entrypoint`
成功，因为 pytest 已经替你把 `src` 放进搜索路径了；真从命令行起就 `ModuleNotFoundError`。

`harness.py` 与 `api.py` 都自己在文件头把 `src` 插进 `sys.path`，只有 `entrypoint.py`
没有——而它偏偏是 README 让人跑的那一个。
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_cli_help_works_without_pytest_pythonpath(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    result = subprocess.run(
        [sys.executable, str(ROOT / 'entrypoint.py'), '--help'],
        capture_output=True, text=True, cwd=tmp_path, env=env, timeout=120)

    assert 'ModuleNotFoundError' not in result.stderr, result.stderr[-800:]
    assert result.returncode == 0, result.stderr[-800:]
    assert '--doctor' in result.stdout
