"""启动成本的守卫：重依赖不许在模块级导入。

为什么这条要用测量而不是"看代码"
----------------------------------
`import harness` 是本仓最贵的一次导入（约 0.35–0.39s）。其中 `yaml` 约 34ms——它只在
"解析配置"时用到，却跟着启动一起加载。

`dotenv` 不动：`load_dotenv()` 必须在 import 期跑完，否则 `.env` 里配的
`LLM_CONTEXT_WINDOW` 会在 `CONTEXT_LIMIT` 被算出来之前还用不上。

单纯把 `import yaml` 移进函数体也会"看起来对了"，但真正要守的是**成本**：
哪天有人把它加回去，代码审查看不出来，测量能。

所以这里量两件事：
1. 导入后 `sys.modules` 里不许有这两者（确定性，不受机器负载影响）；
2. 总耗时的上界（宽松，只防数量级退化）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _import_harness(*, modules_only: bool = False) -> subprocess.CompletedProcess:
    code = (
        'import sys, time\n'
        f'sys.path.insert(0, {str(ROOT / "src")!r})\n'
        f'sys.path.insert(0, {str(ROOT)!r})\n'
        'started = time.perf_counter()\n'
        'import harness\n'
        'elapsed = time.perf_counter() - started\n'
        'print(f"{elapsed:.3f}")\n'
        'print("loaded=" + "|".join(sorted(m for m in ("yaml",) '
        'if m in sys.modules)))\n'
    )
    return subprocess.run([sys.executable, '-c', code],
                          capture_output=True, text=True, timeout=180)


def test_yaml_is_not_imported_eagerly():
    """`yaml` 只在真解析配置时才导入。

    实测（`-X importtime`，取累计列）：`import harness` 里 yaml 占 33–34ms，
    总耗时 343–386ms。它在三个文件里都只在函数体内使用，模块级没有解析动作。
    """
    result = _import_harness()
    assert result.returncode == 0, result.stderr[-800:]
    elapsed_line, loaded_line = result.stdout.strip().splitlines()[:2]
    loaded_line = loaded_line.removeprefix('loaded=')

    assert loaded_line == '', (
        f'`{loaded_line}` 又被模块级导入了（只在解析配置时需要，'
        '见 docs/PERFORMANCE_PROFILE.md）')
    # 刻意**不**断言耗时：同一条 `import harness` 空载实测 0.35–0.48s、
    # 全量测试跑到后半段时实测 1.07s——这条阈值在满载下必然误报，而它想防的
    # 回归（yaml 被模块级导入）上面那句已经确定性地抓住了。
    assert float(elapsed_line) > 0, '耗时没量到'


def test_lazy_yaml_still_parses_frontmatter():
    """惰性化不能把功能弄丢：需要 YAML 的路径要自己把它导进来、并且解析正确。"""
    import sys

    import memory

    meta, body = memory._parse_frontmatter('---\nname: 演示\n---\n正文')
    assert meta == {'name': '演示'} and body == '正文'
    # 走到 YAML 的路径必须真的把包导进来（否则说明走的是"解析失败静默为空"）
    assert 'yaml' in sys.modules
