"""每个「拒绝」决策都要带一条可执行提示。

`hooks.deny_message()` 会按 `decision.code` 从 `_DENY_HINTS` 取一行"接下来怎么办"
追加到拒绝语后面。缺了这行，模型拿到的只有 `Error: …`——它不知道是**策略拒绝**
（重试几次都没用）还是**暂时失败**（换个写法就行），于是往往原地重试，白烧回合。

方向只查一边：`deny 码 ⊆ 提示表`。反向（提示表里有、暂时没有 deny 产出）是允许的，
那几条是留给将来某个码从 ask 变 deny 时兜底的。
"""

import ast
import re
from pathlib import Path

import hooks

SRC = Path(__file__).resolve().parent.parent / 'src'


def _deny_codes() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob('*.py')):
        text = path.read_text(encoding='utf-8')
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func).split('.')[-1] != 'Decision':
                continue
            if len(node.args) < 2:
                continue
            action = ast.unparse(node.args[0]).strip("'\"").upper()
            code = node.args[1]
            if action == 'DENY' and isinstance(code, ast.Constant):
                found.setdefault(code.value, []).append(f'{path.name}:{node.lineno}')
        # 也有 `Decision('deny', 'x', …)` 之外的写法（拼字符串），一并兜住
        for match in re.finditer(r"Decision\(\s*DENY\s*,\s*'([a-z_]+)'", text):
            found.setdefault(match.group(1), []).append(f'{path.name}:行')
    return found


def test_every_deny_code_has_a_hint():
    hints = set(hooks._DENY_HINTS)
    missing = {code: where for code, where in _deny_codes().items()
               if code not in hints}
    assert not missing, f'这些拒绝码没有可执行提示：{missing}'


def test_hints_are_english_and_single_line():
    """提示是给模型看的（数据通道族），英文、单行。"""
    for code, hint in hooks._DENY_HINTS.items():
        assert hint.strip(), code
        assert '\n' not in hint, code
        assert not re.search(r'[\u4e00-\u9fff]', hint), f'{code} 的提示混了中文'
