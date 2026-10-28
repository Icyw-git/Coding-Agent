"""界面文案不得泄漏开发者视角。

为什么需要这一条
----------------
前面几轮手工扫描发现的问题有共同模式：**功能都对，但开发者视角直接暴露给
用户**。已经修过的例子：

- 审批面显示 `moves=[{'from': ...}]`（原始 Python dict）
- `/audit` 显示 `desktop_snapshot_failed`（代码里的事件名）
- `/rules` 说明写 `destructive / opaque_command / external_tool`（内部决策码）
- 台账显示 `指标：round=2`（内部计轮变量）
- TUI 里 74 处提示是纯英文（`Cannot rename while a request is running.`）
- `/help` 表头 `TUI commands:`（实现分层概念）
- `/improvements rollback <change_id>`（字段名当占位符）

这些测试都不会失败，因为**没人断言"这句话用户能读懂"**。这个文件补上。

判据
----
只检查**流向显示层**的字符串（`_notice` / `render` / Textual 组件），不看：
- 模块/函数 docstring（那是写给人读代码的）
- Textual 样式标记（`[bold]`、`#d78757`）
- 工具返回给**模型**的英文文案（模型侧英文更可靠，且不在本文件范围）
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / 'src'

#: 这些调用的字符串会出现在用户眼前。
DISPLAY_CALLS = frozenset({
    '_notice', 'render', 'Static', 'Label', 'Button', 'add_option',
})

_CJK = re.compile(r'[\u4e00-\u9fff]')
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]{1,}")
#: Textual 标记与颜色，不是文案。
_MARKUP = re.compile(r'\[/?[a-z][^\]]*\]|#[0-9a-fA-F]{6}|\b(?:bold|dim|italic|reverse|underline)\b')


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, 'body', [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _display_strings():
    """产出 ``(文件, 行号, 文本)``：流向显示层、且不含中文的字符串。"""
    for path in sorted(SRC.rglob('*.py')):
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except SyntaxError:
            continue
        skip = _docstring_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (getattr(node.func, 'attr', None)
                    or getattr(node.func, 'id', None))
            if name not in DISPLAY_CALLS:
                continue
            for child in ast.walk(node):
                if (not isinstance(child, ast.Constant)
                        or not isinstance(child.value, str)
                        or id(child) in skip):
                    continue
                text = child.value
                if not text.strip():
                    continue
                yield path, child.lineno, text


def test_no_english_only_user_messages():
    """用户可见提示不能整句是英文。

    TUI 其余文案都是中文，夹一句 ``Cannot rename while a request is running.``
    是在逼用户切换到开发者语言。文案统一为中文；需要英文的（工具返回给模型）
    不在本检查范围。
    """
    offenders = []
    for path, line, text in _display_strings():
        if _CJK.search(text):
            continue
        words = [w for w in _WORD.findall(_MARKUP.sub(' ', text))
                 if len(w) >= 2]
        # 3 个以上英文单词才算"一句话"；`/quit`、`Esc`、`ok` 这类不算
        if len(words) >= 3:
            offenders.append(f'{path.relative_to(SRC.parent)}:{line}  {text[:60]}')
    assert not offenders, (
        'user-facing messages must be Chinese; English-only:\n  '
        + '\n  '.join(offenders))


@pytest.mark.parametrize('pattern,why', [
    (re.compile(r'<[a-z][a-z0-9_]*>'),
     '字段名不能当占位符出现在用法里（用 <批次号> 而不是 <change_id>）'),
    (re.compile(r'\[\{'),
     '不能把原始 Python 容器打到界面上'),
    (re.compile(r'\b[a-z_]+=(?![\s]*[\'"])'),
     '不能让用户读 key=value 形式的原始参数'),
])
def test_no_developer_syntax_in_messages(pattern, why):
    """用户提示里不能出现字段名占位符、原始 repr、key=value。"""
    offenders = []
    for path, line, text in _display_strings():
        match = pattern.search(text)
        if match:
            offenders.append(
                f'{path.relative_to(SRC.parent)}:{line}  '
                f'{match.group(0)!r} in {text[:50]!r}')
    assert not offenders, f'{why}\n  ' + '\n  '.join(offenders)


def test_help_does_not_expose_internal_surfaces():
    """`/help` 不能出现 TUI / ported / line REPL 这类实现分层。"""
    import sys

    sys.path.insert(0, str(SRC))
    from ui.commands import help_text

    text = help_text()
    for banned in ('TUI commands', 'ported', 'line REPL', 'Skills ('):
        assert banned not in text, f'/help leaks {banned!r}'


def test_display_strings_are_gbk_safe():
    """用户可见文案必须能编码成 GBK。

    ``harden_stdio()`` 只把流设成 backslashreplace——**不崩，但会打印
    ``\u2713`` 这种转义串**。对每次工具调用都出现的活动行来说，这等于用户
    看到的是乱码。

    已经踩过两次：台账的 ✓ ✗ ◐、工具活动行的 • ✓ ✗。
    """
    bad = []
    for path, line, text in _display_strings():
        for char in set(text):
            try:
                char.encode('gbk')
            except UnicodeEncodeError:
                bad.append(f'{path.relative_to(SRC.parent)}:{line}  {char!r} '
                           f'(U+{ord(char):04X}) in {text[:40]!r}')
    assert not bad, ('user-facing text must be GBK-encodable; '
                     'use √ × · ~ instead of ✓ ✗ • ◐\n  ' + '\n  '.join(bad))
