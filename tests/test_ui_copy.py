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


#: 内部字段名被当成标签用的典型写法（`'recurring' if ...`、`f'exit={x}'`）。
_FIELD_AS_LABEL = re.compile(r"['\"](recurring|allow_side_effects|deliver_to|"
                             r"idle_after_s|exit_code|session_id|req_id)['\"]")
_KV_IN_TEXT = re.compile(r'\b(exit|type|mode|scope|kind|status)=')
#: 这些模块专门产用户可见文案。
COPY_MODULES = ('feishu_cron_card.py', 'feishu_task_card.py',
                'feishu_domain_card.py', 'feishu_sessions.py',
                'terminal_views.py', 'status_report.py', 'approval_view.py')


@pytest.mark.parametrize('name', COPY_MODULES)
def test_field_names_are_not_used_as_labels(name):
    """内部字段名不能直接当显示标签。

    已修过：``kind = 'recurring' if …``（菜单元字段名）、``exit={code}``
    （kv 形式）、``home=`` / ``change_id`` 占位符。这些不该出现在用户眼前。
    """
    path = SRC / name
    if not path.is_file():
        pytest.skip(f'{name} not present')
    problems = []
    for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith('#'):
            continue
        # 这一行本身含中文，说明它是「字段名 -> 中文标签」的翻译表，
        # 不是在把字段名当标签显示（`'session_id': '会话'`）。
        if _CJK.search(line):
            continue
        match = _FIELD_AS_LABEL.search(line) or _KV_IN_TEXT.search(line)
        if not match:
            continue
        # 字段名作为 dict 键 / 参数名 / 日志前缀不算泄漏
        if any(token in line for token in (
                "get(", "['", '["', "']", '"]', 'print(', 'logging',
                f'{match.group(1)}=', 'as ', 'def ', 'import ')):
            continue
        problems.append(f'{name}:{lineno}  {stripped[:70]}')
    assert not problems, ('internal field name used as a user-facing label:\n  '
                          + '\n  '.join(problems))


#: 审批面会把 detail() 的每一行**直接打给用户**（控制台提示 + 飞书卡片）。
#: 这里逐个工具给一份代表性参数，验证渲染出来的文案是给人看的中文。
#: 一个真实形状的补丁，好走「摘要」而不是「原文预览」分支。
_PATCH = chr(10).join([
    '*** Begin Patch', '*** Update File: a.txt', '@@', '-x', '+y',
    '*** End Patch'])

_DETAIL_CASES = {
    'bash': {'command': 'rm -rf build'},
    'write': {'path': 'a.txt', 'content': 'x'},
    'edit': {'path': 'a.txt', 'old_text': 'a', 'new_text': 'b'},
    'apply_patch': {'patch': _PATCH},
    'read': {'path': 'a.txt'},
    'glob': {'pattern': '*.py'},
    'grep': {'pattern': 'x'},
    'ls': {'path': '.'},
    'web_fetch': {'url': 'https://example.com'},
    'delete_paths': {'paths': ['a.txt', 'b.txt']},
    'move_paths': {'moves': [{'from': 'a.txt', 'to': 'b.txt'}]},
    'undo_moves': {'batch_id': 'b1'},
    'remind': {'text': '喝水', 'when': '明天 9 点'},
    'remember': {'type': 'preference', 'name': 'n', 'text': 'x'},
    'forget': {'name': 'n'},
    'recall': {'query': 'x'},
    'screen_capture': {'screen': 'all', 'region': '0,0,10,10', 'delay_ms': 100},
    'screen_ocr': {'path': 'x.png', 'lang': 'zh-Hans-CN'},
    'clipboard_read': {},
    'clipboard_write': {'text': 'abc'},
    'desktop_snapshot': {},
    'desktop_inspect': {},
    'desktop_displays': {},
    'desktop_wait': {'text': '保存', 'state': 'present'},
    'desktop_windows': {'limit': 5},
    'desktop_action': {'action': 'click', 'x': 1, 'y': 2,
                       'expected_pid': 9, 'expected_window_id': 'AB'},
    'desktop_action#sequence': {'action': 'sequence', 'expected_pid': 9,
                                'expected_window_id': 'AB',
                                'steps': [{'action': 'click', 'x': 1, 'y': 2},
                                          {'action': 'type', 'text': 'hi'}]},
    'desktop_action#element': {'action': 'element', 'element': 3,
                               'operation': 'invoke', 'expect': '提交',
                               'expected_pid': 9, 'expected_window_id': 'AB'},
    'task': {'description': 'x'},
    'schedule_cron': {'schedule': '* * * * *', 'prompt': 'x'},
    'create_task': {'subject': 'x', 'description': 'y'},
    'cancel_cron': {'id': 'j1'},
    'checkpoint': {'label': 'l'},
    'compact': {},
    'check_inbox': {},
    'list_crons': {},
    'revoke_permissions': {},
    'send_message': {'to': 'a', 'message': 'b'},
    'spawn_teammate': {'name': 'n', 'task': 't'},
    'create_worktree': {'name': 'n'},
    'rename_session': {'title': 't'},
    'find_sessions': {'query': 'q'},
    'load_skill': {'name': 'n'},
    'todo_write': {'todos': [{'content': 'x', 'status': 'pending'}]},
    'todo_read': {},
}

#: 这些工具的 detail 本来就是内容本身（命令、路径），不要求中文。
_RAW_DETAIL = {'bash', 'read', 'glob', 'grep', 'ls', 'write'}


@pytest.mark.parametrize('key', sorted(_DETAIL_CASES))
def test_approval_detail_is_human_readable_chinese(key):
    """审批提示必须是给用户看的中文，不能是英文 + key=value。

    踩过：`_desktop_detail` / `_screen_detail` / `_clipboard_detail` /
    `_delete_detail` 一直是 `action=type, foreground pid=1, text length=3`
    这种形态。前几轮的守卫扫的是「传给 _notice/render 的字符串」，
    而 detail() 返回的是函数里的字面量，一直漏到现在。
    """
    import approval_view

    tool, _, variant = key.partition('#')
    text = approval_view.detail(tool, dict(_DETAIL_CASES[key]))
    # 空白的审批提示等于让用户盲签：必须写出「会做什么」。
    assert text.strip(), f'{tool} 的审批提示是空的'
    text.encode('gbk')          # GBK 不安全会在真实控制台打成转义串
    if tool in _RAW_DETAIL and not variant:
        return
    assert re.search(r'[一-鿿]', text), f'{tool} 的审批文案没有中文: {text!r}'
    leftover = re.findall(r'[a-z_]{2,}=(?=\S)', text)
    assert not leftover, f'{tool} 的审批文案还在用 key=value: {leftover} in {text!r}'


@pytest.mark.parametrize('key', sorted(_DETAIL_CASES))
def test_approval_detail_accepts_display_forms(key):
    """detail() 必须能用「中文标签」或「标签 (原名)」调用。

    踩过：飞书审批卡把 `tool_label()` 的结果传给了 `detail()`，而 detail() 按
    原始工具名分支渲染——于是卡片上显示的是 `action=click, x=10, y=20`
    这种参数对，前十二轮的中文化全被绕过去。现在三种形态都能还原。
    """
    import approval_view

    tool = key.partition('#')[0]
    args = dict(_DETAIL_CASES[key])
    raw = approval_view.detail(tool, args)
    assert approval_view.detail(approval_view.tool_label(tool), args) == raw
    label = approval_view._TOOL_LABELS.get(tool)
    if label:   # 没登记标签的工具（如 checkpoint）只有原名一种形态
        assert approval_view.detail(label, args) == raw


def test_approval_detail_shows_which_control_by_name():
    """按编号操作时必须显示控件名：只给编号等于没有知情同意。"""
    import approval_view

    text = approval_view.detail('desktop_action', {
        'action': 'element', 'element': 3, 'operation': 'invoke',
        'expect': '提交按钮', 'expected_pid': 9, 'expected_window_id': 'AB'})
    assert '#3' in text and '提交按钮' in text


def test_cli_and_tui_command_sets_match():
    """CLI 补全与 TUI 命令面板必须是同一套命令。

    实测漂移过：TUI 有 48 个、CLI 补全只有 34 个，`/memories` `/improvements`
    `/usage` `/undo-move` 这些新命令**在 CLI 里补不出来**——用户只能靠记忆敲
    全名，敲错还没有提示（`/memori` 不会补成 `/memories`）。

    两份实现（`cli_commands.COMMANDS` 与 `ui.commands.COMMANDS`）是历史形成的，
    没有共同的注册点，只能靠断言钉住。
    """
    import sys

    sys.path.insert(0, str(SRC))
    import cli_commands
    import ui.commands

    cli = {c.name.lstrip('/') for c in cli_commands.COMMANDS}
    cli |= {alias.lstrip('/') for c in cli_commands.COMMANDS for alias in c.aliases}
    tui = {spec.name for spec in ui.commands.COMMANDS}

    missing = sorted(tui - cli)
    assert not missing, (
        'these commands exist in the TUI palette but not in CLI completion '
        f'(users cannot tab-complete them): {missing}')
