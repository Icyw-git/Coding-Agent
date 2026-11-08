"""脚本必须能在任意目录跑。

两条判据：

1. **不能在 `sys.path` 里塞相对路径**。`sys.path.insert(0, 'src')` / `('.' )`
   只在 cwd 恰好是仓库根时成立；换个目录就 `ModuleNotFoundError`
   （`entrypoint.py` 与 `scripts/accept_tool_discovery.py` 各踩过一次）。

2. **基准脚本的 `--help` 要能用**。原来 `int(sys.argv[1])` 直接把 `--help`
   喂给 `int()`，得到一个 traceback；而这种脚本的第一次运行往往就是 `--help`。
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'scripts'


def _relative_path_inserts() -> list[str]:
    offenders = []
    for path in sorted(SCRIPTS.glob('*.py')):
        text = path.read_text(encoding='utf-8')
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            if name not in ('sys.path.insert', 'sys.path.append'):
                continue
            if not node.args:
                continue
            arg = node.args[-1]
            # 只关心字面量字符串：'src' / '.' / './src' 这类相对路径
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                value = arg.value
                if not Path(value).is_absolute() and value in ('.', 'src', './src', '..'):
                    offenders.append(f'{path.name}:{node.lineno} {value!r}')
    return offenders


def test_no_script_uses_a_cwd_relative_path_insert():
    offenders = _relative_path_inserts()
    assert not offenders, (
        '这些脚本把相对路径塞进 sys.path，只能在仓库根跑：' + ', '.join(offenders))


def test_bench_scripts_respond_to_help_from_another_directory(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    for name in ('bench_history', 'bench_layout', 'bench_ui'):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / f'{name}.py'), '--help'],
            capture_output=True, text=True, cwd=tmp_path, env=env, timeout=120)
        assert result.returncode == 0, f'{name}: {result.stderr[-600:]}'
        assert 'usage:' in result.stdout, f'{name}: {result.stdout[:200]}'
