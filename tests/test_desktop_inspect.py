"""desktop_inspect / 元素级动作：解析、校验、文案与策略边界。

这一层不碰真实桌面：PowerShell 侧由 ``_run`` 注入，工具层由 monkeypatch 注入。
真实 UIA 行为靠 e2e 脚本验证（见 docs/DESKTOP_CONTROL_RESEARCH.md）。
"""
import pytest

import desktop_control_windows as desktop
import never_do
import tool_desktop
from exec_policy import ASK, DENY, Policy

IDENTITY = {'expected_pid': 123, 'expected_window_id': 'ABCD'}
FP = 'a1b2c3d4e5f60718'


def _state(**over):
    base = {
        'available': True, 'title': '设置', 'process': 'SystemSettings',
        'pid': 123, 'window_id': 'ABCD', 'class_name': 'WinUIDesktopWin32Window',
        'rect': [0, 0, 800, 600], 'focused_name': '', 'focused_control_type': '',
        'focused_is_password': False, 'sensitive_target': False,
    }
    base.update(over)
    return base


def _item(**over):
    base = {
        'id': 1, 'label': '按钮', 'name': '提交', 'value': '', 'password': False,
        'enabled': True, 'focusable': True, 'focused': False,
        'bounds': [10, 20, 30, 40], 'operations': ['invoke'],
    }
    base.update(over)
    return base


def _tree(*items, truncated=False, fingerprint=FP, state=None, duration_ms=12):
    return desktop.UiTree(
        items=tuple(items), truncated=truncated, fingerprint=fingerprint,
        state=desktop._state(state or _state()), duration_ms=duration_ms)


# ---------- 参数校验 ----------

def test_element_action_requires_window_identity():
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_element_action('invoke', 1, FP, expected_pid=None,
                                        expected_window_id='ABCD')
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_element_action('invoke', 1, FP, expected_pid=123)


def test_element_action_defaults_to_invoke():
    payload = desktop.validate_element_action(None, 3, FP, **IDENTITY)
    assert payload['operation'] == 'invoke'
    assert payload['element'] == 3
    assert payload['tree'] == FP


@pytest.mark.parametrize('operation', ['invoke', 'setvalue', 'toggle', 'select',
                                       'expand', 'collapse', 'focus'])
def test_element_action_accepts_known_operations(operation):
    text = 'x' if operation == 'setvalue' else None
    payload = desktop.validate_element_action(operation, 1, FP, text=text, **IDENTITY)
    assert payload['operation'] == operation


@pytest.mark.parametrize('operation', ['click', 'invoke;rm', 'invokee', 'drag'])
def test_element_action_rejects_unknown_operations(operation):
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_element_action(operation, 1, FP, **IDENTITY)


def test_element_action_treats_blank_operation_as_invoke():
    for blank in ('', '   ', None):
        payload = desktop.validate_element_action(blank, 1, FP, **IDENTITY)
        assert payload['operation'] == 'invoke'


@pytest.mark.parametrize('element', [0, -1, desktop.MAX_ELEMENT_INDEX + 1, 'x', None])
def test_element_action_rejects_bad_index(element):
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_element_action('invoke', element, FP, **IDENTITY)


@pytest.mark.parametrize('fingerprint', ['', 'abc', 'zzzzzzzzzzzz', 'a' * 65, None])
def test_element_action_requires_inspect_fingerprint(fingerprint):
    """没有指纹就拒绝：界面在「看一眼」和「点一下」之间变了，编号会指错控件。"""
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_element_action('invoke', 1, fingerprint, **IDENTITY)


def test_setvalue_requires_text_and_rejects_it_for_other_operations():
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_element_action('setvalue', 1, FP, **IDENTITY)
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_element_action('invoke', 1, FP, text='x', **IDENTITY)
    payload = desktop.validate_element_action('setvalue', 1, FP, text='你好', **IDENTITY)
    assert payload['text'] == '你好'


def test_setvalue_rejects_overlong_text():
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_element_action(
            'setvalue', 1, FP, text='x' * (desktop.MAX_TYPE_CHARS + 1), **IDENTITY)


# ---------- 与 PowerShell 的载荷 ----------

def test_element_action_sends_forbidden_patterns(monkeypatch):
    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return {'ok': True, 'action': 'element',
                'operation': 'invoke', 'element': 2,
                'target': _item(id=2, name='保存'), 'state_after': _state()}

    monkeypatch.setattr(desktop, '_run', fake)
    result = desktop.element_action('invoke', 2, FP, **IDENTITY,
                                    forbidden=['(?i)密码'])
    assert seen['action'] == 'element'
    assert seen['tree'] == FP
    assert seen['element'] == 2
    assert seen['forbidden'] == ['(?i)密码']
    assert result.action == 'element'
    assert result.target is not None and result.target.name == '保存'


def test_element_action_omits_forbidden_when_empty(monkeypatch):
    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return {'ok': True, 'action': 'element', 'operation': 'invoke',
                'element': 1, 'target': _item(), 'state_after': _state()}

    monkeypatch.setattr(desktop, '_run', fake)
    desktop.element_action('invoke', 1, FP, **IDENTITY, forbidden=[])
    assert 'forbidden' not in seen


def test_element_action_surfaces_refusal(monkeypatch):
    def fake(payload, **kwargs):
        raise desktop.DesktopControlError('must_do_self: only the user can do it')

    monkeypatch.setattr(desktop, '_run', fake)
    with pytest.raises(desktop.DesktopControlError):
        desktop.element_action('invoke', 1, FP, **IDENTITY)


# ---------- 解析 ----------

def test_inspect_parses_items(monkeypatch):
    def fake(payload, **kwargs):
        assert payload['action'] == 'inspect'
        return {'ok': True, 'action': 'inspect', 'state': _state(),
                'items': [_item(id=1), _item(id=2, label='输入框', name='搜索',
                                            value='wifi', bounds=None,
                                            operations=['setvalue'])],
                'total': 2, 'truncated': True, 'fingerprint': FP,
                'duration_ms': 42}

    monkeypatch.setattr(desktop, '_run', fake)
    tree = desktop.inspect()
    assert len(tree.items) == 2
    assert tree.items[1].value == 'wifi'
    assert tree.items[1].bounds is None
    assert tree.truncated is True
    assert tree.fingerprint == FP
    assert tree.state.title == '设置'


def test_inspect_survives_garbage_items(monkeypatch):
    monkeypatch.setattr(desktop, '_run', lambda payload, **kw: {
        'ok': True, 'state': _state(),
        'items': ['nonsense', _item(id=4, bounds=['x'], operations='invoke')],
        'fingerprint': FP})
    tree = desktop.inspect()
    assert len(tree.items) == 1
    item = tree.items[0]
    assert item.bounds is None
    assert item.operations == ()


def test_inspect_sends_limits_and_exclusions(monkeypatch):
    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return {'ok': True, 'state': _state(), 'items': [], 'fingerprint': FP}

    monkeypatch.setattr(desktop, '_run', fake)
    monkeypatch.setenv('HARNESS_DESKTOP_EXCLUDE_PROCESSES', 'Code.exe, Cursor.exe')
    desktop.inspect()
    assert seen['exclude'] == ['Code.exe', 'Cursor.exe']
    assert seen['max_nodes'] >= 5 and seen['max_depth'] >= 2 and seen['max_ms'] >= 200


def test_tree_limits_clamp_bad_values(monkeypatch):
    monkeypatch.setenv('HARNESS_DESKTOP_TREE_NODES', '100000')
    monkeypatch.setenv('HARNESS_DESKTOP_TREE_DEPTH', '1')
    monkeypatch.setenv('HARNESS_DESKTOP_TREE_MS', '1')
    limits = desktop.tree_limits()
    assert limits['max_nodes'] == desktop.MAX_ELEMENT_INDEX
    assert limits['max_depth'] == 2
    assert limits['max_ms'] == 200
    monkeypatch.setenv('HARNESS_DESKTOP_TREE_NODES', 'not-a-number')
    assert desktop.tree_limits()['max_nodes'] == desktop.DEFAULT_TREE_NODES


# ---------- 工具层文案 ----------

def _patch_tree(monkeypatch, tree):
    monkeypatch.setattr(desktop, 'inspect', lambda **kwargs: tree)


def test_inspect_text_lists_controls_with_fingerprint(monkeypatch):
    _patch_tree(monkeypatch, _tree(
        desktop._element_info(_item(id=1, name='提交'), 1),
        desktop._element_info(_item(id=2, label='输入框', name='搜索', value='wifi',
                                    operations=['setvalue']), 2)))
    text = tool_desktop.inspect()
    assert '#1 按钮 "提交"' in text
    assert '#2 输入框 "搜索"' in text
    assert '内容 "wifi"' in text
    assert text.count(FP) == 1
    assert '已截断' not in text
    text.encode('gbk')


def test_inspect_text_hides_password_value(monkeypatch):
    """密码框的值一个字符都不能出现在输出里。"""
    _patch_tree(monkeypatch, _tree(desktop._element_info(
        _item(label='输入框', name='密码', value='', password=True,
              operations=['setvalue']), 1)))
    text = tool_desktop.inspect()
    assert '已隐藏' in text
    assert '内容' not in text


def test_inspect_text_marks_disabled_and_focused(monkeypatch):
    _patch_tree(monkeypatch, _tree(
        desktop._element_info(_item(id=1, enabled=False), 1),
        desktop._element_info(_item(id=2, name='搜索', focused=True), 2)))
    text = tool_desktop.inspect()
    assert '不可用' in text
    assert '当前焦点' in text


def test_inspect_text_reports_truncation(monkeypatch):
    _patch_tree(monkeypatch, _tree(desktop._element_info(_item(), 1),
                                   truncated=True))
    assert '已按上限截断' in tool_desktop.inspect()


def test_inspect_text_explains_empty_tree(monkeypatch):
    _patch_tree(monkeypatch, _tree())
    text = tool_desktop.inspect()
    assert '没有可读的控件' in text
    assert '坐标' in text


def test_inspect_text_never_leaks_raw_containers(monkeypatch):
    """不能把原始 dict / repr 打到界面上。"""
    _patch_tree(monkeypatch, _tree(desktop._element_info(_item(), 1)))
    text = tool_desktop.inspect()
    for marker in ('{', '}', "'id'", 'operations:', 'bounds: ['):
        assert marker not in text


def test_inspect_failure_becomes_actionable_message(monkeypatch):
    def boom(**kwargs):
        raise desktop.DesktopControlError('no_foreground_window: no foreground window')

    monkeypatch.setattr(desktop, 'inspect', boom)
    text = tool_desktop.inspect()
    assert 'no foreground window' in text
    assert 'desktop_snapshot' in text


def test_inspect_strips_unprintable_control_names(monkeypatch):
    """控件名里真的有零宽字符（实测见过 U+200B），GBK 控制台会打成转义串。"""
    _patch_tree(monkeypatch, _tree(desktop._element_info(
        _item(name='保存\u200b按钮\U0001f600'), 1)))
    text = tool_desktop.inspect()
    text.encode('gbk')
    assert '\u200b' not in text
    assert '保存按钮' in text


def test_element_text_uses_chinese_verb(monkeypatch):
    def fake(*args, **kwargs):
        return desktop.ActionResult(
            action='element', state=desktop._state(_state()), duration_ms=5,
            target=desktop._element_info(_item(name='提交'), 1))

    monkeypatch.setattr(desktop, 'element_action', fake)
    text = tool_desktop.element_action(
        operation='invoke', element=1, tree=FP, expected_pid=123,
        expected_window_id='ABCD')
    assert '已完成点击「提交」' in text
    text.encode('gbk')


def test_element_text_passes_refusal_through(monkeypatch):
    def fake(*args, **kwargs):
        raise desktop.DesktopControlError('tree_changed: the window changed')

    monkeypatch.setattr(desktop, 'element_action', fake)
    text = tool_desktop.element_action(
        operation='invoke', element=1, tree=FP, expected_pid=123,
        expected_window_id='ABCD')
    assert 'tree_changed' in text


# ---------- 第四档：界面上的「只能你自己点」 ----------

@pytest.mark.parametrize('name', ['修改密码', '发送验证码', '立即支付', '转账',
                                  '以管理员身份运行', 'OTP', 'Enter OTP code',
                                  'Pay now', 'UAC prompt', 'Transfer funds'])
def test_ui_forbidden_patterns_cover_must_do_self(name):
    import re

    patterns = never_do.ui_forbidden_patterns()
    assert any(re.search(pattern, name) for pattern in patterns), name


def test_ui_patterns_contain_no_control_characters():
    """模式里不能有控制字符。

    踩过：heredoc 把 ``\\b`` 折叠成了退格符（0x08），于是 ``\\botp\\b`` 变成
    ``<BS>otp<BS>``——规则看起来还在，实际永远不匹配，测试也照样绿。
    """
    patterns = never_do.ui_forbidden_patterns()
    patterns += [pattern.pattern for _tool, pattern, _what in never_do._ARG_RULES]
    patterns += [pattern.pattern for pattern, _what in never_do._COMMAND_RULES]
    for pattern in patterns:
        assert not [ch for ch in pattern if ord(ch) < 32], repr(pattern)
        __import__('re').compile(pattern)


@pytest.mark.parametrize('name', ['保存', '取消', '搜索', '确定', '刷新'])
def test_ui_forbidden_patterns_do_not_fire_on_ordinary_buttons(name):
    import re

    patterns = never_do.ui_forbidden_patterns()
    assert not any(re.search(pattern, name) for pattern in patterns), name


def test_ui_forbidden_patterns_respect_switch(monkeypatch):
    monkeypatch.setenv('HARNESS_NEVER_DO', '0')
    assert never_do.ui_forbidden_patterns() == []


# ---------- 策略边界 ----------

@pytest.mark.parametrize('tool', ['desktop_inspect'])
def test_inspect_is_off_by_default(monkeypatch, tool):
    monkeypatch.delenv('HARNESS_DESKTOP_CONTROL', raising=False)
    assert Policy().evaluate(tool, {}).action == DENY


def test_inspect_requires_approval_when_enabled(monkeypatch):
    monkeypatch.setenv('HARNESS_DESKTOP_CONTROL', '1')
    decision = Policy().evaluate('desktop_inspect', {})
    assert decision.action == ASK
    assert decision.code == 'desktop_control'


def test_inspect_never_auto_allowed_by_tier(monkeypatch):
    monkeypatch.setenv('HARNESS_DESKTOP_CONTROL', '1')
    decision = Policy(sandbox_mode='danger-full-access',
                      approval_policy='never').evaluate('desktop_inspect', {})
    assert decision.action == DENY


def test_inspect_is_forbidden_in_scheduled_turns():
    import hooks

    assert 'desktop_inspect' in hooks.SCHEDULED_FORBIDDEN_TOOLS


def test_inspect_is_forbidden_for_subagents():
    import subagent

    source = open(subagent.__file__, encoding='utf-8').read()
    assert "'desktop_inspect'" in source


# ---------- 审计与 schema ----------

def test_audit_labels_exist():
    import audit

    assert 'desktop_inspect' in audit.EVENT_LABELS
    assert 'desktop_inspect_failed' in audit.EVENT_LABELS
    for event in ('desktop_inspect', 'desktop_inspect_failed'):
        assert event in audit.AUDIT_EVENTS


def test_audit_records_element_fields():
    import audit

    fields = audit.AUDIT_EVENTS['desktop_action']
    assert 'operation' in fields and 'element' in fields


def test_schema_exposes_inspect_and_element_action():
    import tool_schemas

    schemas = {entry['function']['name']: entry for entry in tool_schemas.TOOL_SCHEMAS}
    assert 'desktop_inspect' in schemas
    action = schemas['desktop_action']['function']['parameters']
    assert 'element' in action['properties']['action']['enum']
    for name in ('element', 'operation', 'tree'):
        assert name in action['properties']


def test_tool_registry_routes_element_action(monkeypatch):
    import tools

    for name in ('desktop_inspect', 'desktop_action'):
        assert name in tools.tool_registry
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return 'ok'

    monkeypatch.setattr(tools, '_desktop_element_tool', fake)
    monkeypatch.setattr(tools, '_desktop_action_tool',
                        lambda *a, **kw: pytest.fail('physical path must not run'))
    tools.run_desktop_action('element', element=2, operation='invoke', tree=FP,
                             expected_pid=123, expected_window_id='ABCD')
    assert seen['element'] == 2 and seen['operation'] == 'invoke'
    assert seen['tree'] == FP


def test_tool_registry_keeps_physical_action_path(monkeypatch):
    import tools

    seen = {}

    def fake(*args, **kwargs):
        seen.update(kwargs)
        return 'ok'

    monkeypatch.setattr(tools, '_desktop_action_tool', fake)
    tools.run_desktop_action('click', x=1, y=2, expected_pid=123,
                             expected_window_id='ABCD')
    assert seen['x'] == 1 and seen['y'] == 2
