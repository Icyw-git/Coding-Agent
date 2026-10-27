"""架构守卫：模块级循环依赖与分层约束。

为什么要有这组测试
------------------
`tools` ↔ `hooks` 曾经是一对**顶层**循环依赖，靠导入顺序碰巧能跑。本轮加记忆工具
时踩到了真实故障：

    import tools                          # 从这行开始
    → tool_tasks → import hooks
    → hooks → from tools import tool_registry
    → tools 还没执行到 `tool_registry = {...}`
    → ImportError: cannot import name 'tool_registry' from partially initialized module

修法是把 `hooks` 对 `tools` 的顶层依赖改成调用期惰性导入。这种结构性问题不会自己
回来，但很容易被下一个人重新引入——所以用测试钉住。

这里只检查**模块顶层**的 import：函数体内的惰性导入是刻意的设计（用于打破环），
不算违规。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / 'src'


def _local_modules() -> dict[str, Path]:
    return {p.stem: p for p in SRC.glob('*.py')}


def _top_level_imports(path: Path, known: set[str]) -> set[str]:
    """模块顶层（class/def 之外）引用的本项目模块名。"""
    try:
        tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in tree.body:          # 只看树顶，不进函数体
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name.split('.')[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module.split('.')[0]]
        for name in names:
            if name in known and name != path.stem:
                found.add(name)
    return found


def _cycles() -> list[tuple[str, str]]:
    modules = _local_modules()
    known = set(modules)
    graph = {name: _top_level_imports(path, known)
             for name, path in modules.items()}
    pairs = set()
    for a, deps in graph.items():
        for b in deps:
            if a in graph.get(b, set()):
                pairs.add(tuple(sorted((a, b))))
    return sorted(pairs)


def test_no_module_level_import_cycles():
    """顶层 import 必须无环——有环就意味着"导入顺序碰巧能跑"。"""
    cycles = _cycles()
    assert not cycles, (
        'module-level import cycle(s) detected: '
        + ', '.join(f'{a} <-> {b}' for a, b in cycles)
        + '. Break them with a call-time import or by moving shared code to a '
          'lower-level module.'
    )


def test_tools_and_hooks_import_independently():
    """两个曾经互相依赖的模块，各自都必须能先导入。

    这是 `test_no_module_level_import_cycles` 的具体回归：当时 `import tools`
    直接 ImportError，而 `import hooks` 恰好能用——顺序一换就炸。

    **不能用 `sys.modules.pop` 来测**：那会把模块对象从字典里拿掉，别处（如
    `tools._gate()` 缓存之外的引用、其他用例的 `monkeypatch.setattr(hooks, ...)`）
    持有的旧对象与新建的不是同一个，造成隔用例污染。这里用**子进程**测真实的
    “先导入谁”行为，主进程的模块表不受影响。
    """
    import subprocess
    import sys

    src = str(SRC)
    for first in ('tools', 'hooks'):
        code = f'import sys; sys.path.insert(0, {src!r}); import {first}; print("ok")'
        result = subprocess.run([sys.executable, '-c', code],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, (
            f'importing {first} first failed:\n{result.stderr[-800:]}')
        assert 'ok' in result.stdout


def test_registry_check_does_not_need_top_level_tools():
    """`hooks.is_tool_hook` 只在调用期取 registry，顶层不 import tools。"""
    source = (SRC / 'hooks.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in tree.body:
        names = []
        if isinstance(node, ast.ImportFrom) and node.module == 'tools':
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names if alias.name == 'tools']
        assert not names, f'hooks.py must not import tools at module level: {names}'


def test_settings_is_a_leaf_module():
    """配置读取层不该依赖任何本项目模块，否则它会重新变成依赖环的中转站。"""
    modules = _local_modules()
    known = set(modules) - {'settings'}
    deps = _top_level_imports(SRC / 'settings.py', known)
    assert not deps, f'settings.py should stay dependency-free, imports: {sorted(deps)}'


@pytest.mark.parametrize('name', ['paths', 'file_lock', 'console'])
def test_low_level_utils_stay_low(name):
    """路径/文件锁/终端这类原语不该反过来依赖策略层或工具层。"""
    modules = _local_modules()
    known = set(modules) - {name}
    deps = _top_level_imports(SRC / f'{name}.py', known)
    forbidden = deps & {'hooks', 'tools', 'harness_api', 'feishu_gateway',
                        'exec_policy', 'approval'}
    assert not forbidden, f'{name}.py must not depend on {sorted(forbidden)}'


#: 入口适配层：职责上属于入口，顶层依赖根目录 harness 是合理的。
#: 详见 docs/coding-standards.md 的「与 harness 的关系」。
_ENTRY_ADAPTERS = {'headless'}


def test_only_entry_adapters_import_harness_at_module_level():
    """`src/` 里顶层 `import harness` 只允许出现在入口适配层。

    这条钉住的是规范与实现的一致性：规范说"src/ 不 import harness"，实际有大量
    模块靠 `llm_route.harness()` 延迟取（允许，但必须在函数体内）。顶层 import
    会直接造成循环依赖，且让 harness.py 的任何重构波及全仓。
    """
    offenders = []
    for path in SRC.glob('*.py'):
        if path.stem in _ENTRY_ADAPTERS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
        except SyntaxError:
            continue
        for node in tree.body:
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if 'harness' in names:
                offenders.append(path.name)
    assert not offenders, (
        'module-level `import harness` is only allowed in entry adapters '
        f'({sorted(_ENTRY_ADAPTERS)}); found: {sorted(set(offenders))}. '
        'Use `llm_route.harness()` inside the function instead.'
    )
