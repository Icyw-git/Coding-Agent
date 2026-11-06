"""代码读的每个环境变量都要在文档里出现。

为什么需要：`/status` 与 `feishu.env.example` 是用户发现开关的唯一入口。一个开关
只活在代码里，等于只有读源码的人知道它存在。

上一轮的检查只覆盖了 `HARNESS_*` 前缀（88/88 全中），于是 `FEISHU_BINDINGS` 和
`FEISHU_STREAM_TRACE` 两个真开关一直没人知道——一个改绑定文件路径、一个开流式
延迟诊断。这次改成**不按前缀**，把所有真实读取点都扫出来。

排除 Windows 自带的（`COMSPEC` / `PROGRAMFILES*`）：那是系统给的，不属于我们的
配置面。
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 操作系统自己提供、不需要我们文档化的。
OS_PROVIDED = {'COMSPEC', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'SYSTEMROOT',
               'WINDIR', 'PATHEXT'}

#: 文档只在运行时才拼出来的名字没法静态扫，单独列出（目前没有）。
DYNAMIC = set()


def _read_env_names() -> set[str]:
    names: set[str] = set()
    sources = list((ROOT / 'src').rglob('*.py')) + [
        ROOT / 'harness.py', ROOT / 'entrypoint.py']
    for path in sources:
        text = path.read_text(encoding='utf-8')
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                if name.endswith(('getenv', 'environ.get', 'environ.pop')) and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        names.add(arg.value)
            elif isinstance(node, ast.Subscript) and 'environ' in ast.unparse(node.value):
                key = node.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    names.add(key.value)
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if (isinstance(target, ast.Name) and target.id.endswith('_ENV')
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    names.add(node.value.value)
    return names


def _documented() -> str:
    text = ''
    for path in list((ROOT / 'docs').glob('*.md')) + [
            ROOT / 'README.md', ROOT / 'feishu.env.example']:
        text += path.read_text(encoding='utf-8')
    return text


def test_every_env_var_is_documented():
    missing = sorted(name for name in _read_env_names()
                     if name not in _documented()
                     and name not in OS_PROVIDED
                     and name not in DYNAMIC
                     and re.fullmatch(r'[A-Z][A-Z_0-9]*', name))
    assert not missing, (
        '这些环境变量在代码里读取，但任何文档都没提：' + ', '.join(missing))
