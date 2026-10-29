"""desktop_inspect / 元素级动作：解析、校验、文案与策略边界。

这一层不碰真实桌面：PowerShell 侧由 ``_run`` 注入，工具层由 monkeypatch 注入。
真实 UIA 行为靠 e2e 脚本验证（见 docs/DESKTOP_CONTROL_RESEARCH.md）。
"""
import re

import pytest

import desktop_control_windows as desktop
import never_do
import tool_desktop
from exec_policy import ASK, DENY, Policy

IDENTITY = {'expected_pid': 123, 'expected_window_id': 'ABCD'}


def _ps_function(script: str, name: str) -> str:
    """取出 PowerShell 里某个函数的函数体。

    这类断言本来可以直接切字符串（``script.split("if ($payload.action -eq ...")``），
    但那样一重构就碎——把 element 分支抽成函数时，五条测试同时失效。
    按函数名取，位置怎么挪都不影响。
    """
    marker = f'function {name}'
    assert marker in script, f'{name} 不在脚本里'
    body = script.split(marker, 1)[1]
    return body.split(chr(10) + 'function ', 1)[0]
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


def test_element_action_passes_expect_down(monkeypatch):
    """expect 要送到 PowerShell 侧核对，也要能显示在审批卡上。"""
    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return {'ok': True, 'action': 'element', 'operation': 'invoke',
                'element': 3, 'target': _item(id=3, name='提交按钮'),
                'state_after': _state()}

    monkeypatch.setattr(desktop, '_run', fake)
    desktop.element_action('invoke', 3, FP, **IDENTITY, expect='  提交按钮 ')
    assert seen['expect'] == '提交按钮'


def test_element_action_omits_expect_when_blank(monkeypatch):
    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return {'ok': True, 'action': 'element', 'operation': 'invoke',
                'element': 1, 'target': _item(), 'state_after': _state()}

    monkeypatch.setattr(desktop, '_run', fake)
    desktop.element_action('invoke', 1, FP, **IDENTITY, expect='   ')
    assert 'expect' not in seen


def test_element_action_rejects_overlong_expect():
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_element_action(
            'invoke', 1, FP, expect='x' * (desktop.MAX_ELEMENT_EXPECT_CHARS + 1),
            **IDENTITY)


def test_powershell_verifies_expect_against_the_actual_control():
    """expect 的核对必须在 PowerShell 侧（模型给的名字不能自己说了算）。"""
    script = desktop.worker_script()
    assert 'element_mismatch' in script
    assert '编号和名字对不上' in script


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

def test_element_action_sends_excluded_processes(monkeypatch):
    """病灶进程名单对**所有**会遍历无障碍树的动作生效。

    只对 inspect/wait 生效等于留后门：element 动作照样会进那个进程的树。
    """
    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return {'ok': True, 'action': 'element', 'operation': 'invoke',
                'element': 1, 'target': _item(), 'state_after': _state()}

    monkeypatch.setattr(desktop, '_run', fake)
    monkeypatch.setenv('HARNESS_DESKTOP_EXCLUDE_PROCESSES', 'Code.exe')
    desktop.element_action('invoke', 1, FP, **IDENTITY)
    assert seen['exclude'] == ['Code.exe']


def test_fingerprint_excludes_coordinates_and_values():
    """指纹只覆盖「编号 -> 类型/名字」。

    实测过：把 bounds 算进哈希，窗口一移动/缩放/列表一滚动，全部编号同时失效，
    而元素动作是按 UIA 编号定位的、根本不用坐标——等于给最常见的操作加了个
    假故障。value / enabled 同理：输入框内容变化、按钮从灰变亮都不该让编号失效。
    """
    script = desktop.worker_script()
    fn = script.split('function Get-TreeFingerprint', 1)[1].split('function ', 1)[0]
    code = chr(10).join(line for line in fn.splitlines()
                        if not line.lstrip().startswith('#'))
    assert 'bounds' not in code
    assert 'value' not in code
    assert 'enabled' not in code
    assert 'item.label' in code and 'item.name' in code and 'item.id' in code


def test_every_tree_walking_action_checks_the_exclusion_list():
    script = desktop.worker_script()
    # 三处会遍历无障碍树的入口，都必须先过病灶进程名单
    for name in ('Invoke-HarnessOp', 'Invoke-ElementAction'):
        assert 'Test-ExcludedProcess' in _ps_function(script, name), name


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


def test_element_action_defaults_to_the_policy_patterns(monkeypatch):
    """不传 forbidden 时默认从策略层取，而不是「没有规则」。

    直接调能力层的调用方（将来的子代理、脚本）不该因为少传一个参数，
    就把「改密码/验证码/支付/提权只能用户自己点」这条丢掉了。
    """
    import never_do

    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return {'ok': True, 'action': 'element', 'operation': 'invoke',
                'element': 1, 'target': _item(), 'state_after': _state()}

    monkeypatch.setattr(desktop, '_run', fake)
    monkeypatch.setenv('HARNESS_NEVER_DO', '1')
    desktop.element_action('invoke', 1, FP, **IDENTITY)
    assert seen['forbidden'] == never_do.ui_forbidden_patterns()


def test_approval_detail_flags_missing_control_name():
    import approval_view

    text = approval_view.detail('desktop_action', {
        'action': 'element', 'element': 3, 'operation': 'invoke'})
    assert '未注明' in text


def test_element_action_omits_forbidden_when_empty(monkeypatch):
    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return {'ok': True, 'action': 'element', 'operation': 'invoke',
                'element': 1, 'target': _item(), 'state_after': _state()}

    monkeypatch.setattr(desktop, '_run', fake)
    desktop.element_action('invoke', 1, FP, **IDENTITY, forbidden=[])
    assert 'forbidden' not in seen


def test_element_action_sends_the_same_tree_limits_as_inspect(monkeypatch):
    """元素动作必须用和 inspect 完全一致的遍历预算。

    指纹是「编号 -> 类型/名字/位置」的哈希，节点数或深度不同哈希就不同。
    不带这几个值时，用户调过 `HARNESS_DESKTOP_TREE_NODES` 之后，动作会一直
    误报 tree_changed。
    """
    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return {'ok': True, 'action': 'element', 'operation': 'invoke',
                'element': 1, 'target': _item(), 'state_after': _state()}

    monkeypatch.setattr(desktop, '_run', fake)
    monkeypatch.setenv('HARNESS_DESKTOP_TREE_NODES', '40')
    desktop.element_action('invoke', 1, FP, **IDENTITY)
    limits = desktop.tree_limits()
    for key, value in limits.items():
        assert seen[key] == value, key
    assert seen['max_nodes'] == 40


def test_element_action_needs_explicit_allow_terminal(monkeypatch):
    """terminal 类窗口上 setvalue 同样要显式放行——用脚本起的 GUI 也会被判成终端。"""
    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return {'ok': True, 'action': 'element', 'operation': 'setvalue',
                'element': 1, 'target': _item(), 'state_after': _state()}

    monkeypatch.setattr(desktop, '_run', fake)
    desktop.element_action('setvalue', 1, FP, text='x', **IDENTITY)
    assert seen['allow_terminal'] is False
    seen.clear()
    desktop.element_action('setvalue', 1, FP, text='x', allow_terminal=True, **IDENTITY)
    assert seen['allow_terminal'] is True


def test_allow_terminal_is_reachable_from_the_schema():
    """错误文案会说「re-send with allow_terminal=true」，那它必须在 schema 里。

    踩过：物理输入路径支持 allow_terminal，但 schema 与 tools 层都没这个参数，
    模型照文案重试时参数被丢掉，只能卡死。
    """
    import tool_schemas
    import tools

    schema = next(item for item in tool_schemas.TOOL_SCHEMAS
                  if item['function']['name'] == 'desktop_action')
    assert 'allow_terminal' in schema['function']['parameters']['properties']
    import inspect as pyinspect

    params = pyinspect.signature(tools.run_desktop_action).parameters
    assert 'allow_terminal' in params


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


@pytest.mark.parametrize('operations,mark', [
    (['setvalue'], '可写入'),
    (['toggle'], '可切换'),
    (['select'], '可选中'),
    (['expand'], '可展开'),
    (['collapse'], '可收起'),
    ([], '只能按坐标点'),
])
def test_inspect_text_shows_the_available_operations(monkeypatch, operations, mark):
    """要写出「能做什么」，否则模型只能靠试错。

    一次 `operation_unsupported` 比这几个字贵得多：多一轮往返 + 一次审批。
    """
    _patch_tree(monkeypatch, _tree(desktop._element_info(
        _item(id=1, operations=operations), 1)))
    assert mark in tool_desktop.inspect()


def test_inspect_text_stays_quiet_for_plain_buttons(monkeypatch):
    """纯按钮的「点击」是默认假定，写出来只是烧 token。"""
    _patch_tree(monkeypatch, _tree(desktop._element_info(
        _item(id=1, name='提交', operations=['invoke']), 1)))
    text = tool_desktop.inspect()
    for mark in ('可点击', '可写入', '只能按坐标点'):
        assert mark not in text


def test_only_the_deepest_element_is_marked_focused():
    """只有最深的那一层算「当前焦点」。

    UIA 的 HasKeyboardFocus 在祖先链上也会为真：实测 WindowsTerminal 给
    「窗口 / 面板 / 文本」三行都打了标记。三行都写"当前焦点"，模型就不知道
    该往哪儿输入。
    """
    script = desktop.worker_script()
    walk = _ps_function(script, 'Get-UiTree')
    assert '$lastFocused' in walk
    # 前序：祖先先出、后代后出，所以保留最后一个
    assert walk.index('$lastFocused = $i') < walk.index('$items[$i].focused = $false')


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


@pytest.mark.parametrize('control_type,label', [
    ('Thumb', '滚动滑块'), ('ScrollBar', '滚动条'), ('TitleBar', '标题栏'),
    ('AppBar', '应用栏'), ('Hyperlink', '链接'), ('TabItem', '选项卡'),
    ('ListItem', '列表项'), ('Document', '文档'), ('MenuItem', '菜单项'),
])
def test_common_control_types_have_chinese_labels(control_type, label):
    """没登记的类型会原样输出英文程序名（`Thumb`），模型读不懂。"""
    script = desktop.worker_script()
    section = script.split('$script:CONTROL_LABELS', 1)[1].split('}', 1)[0]
    assert f"'{control_type}'=" in section
    assert label in section


@pytest.mark.parametrize('state,mark', [
    ('checked', '已勾选'), ('unchecked', '未勾选'), ('indeterminate', '部分勾选'),
    ('expanded', '已展开'), ('collapsed', '已收起'), ('selected', '已选中'),
    ('readonly', '只读'),
])
def test_inspect_text_shows_control_state(monkeypatch, state, mark):
    """勾没勾、开没开、选中了哪个——不说就只能靠截图判断。"""
    _patch_tree(monkeypatch, _tree(desktop._element_info(
        _item(id=1, state=[state]), 1)))
    assert mark in tool_desktop.inspect()


def test_scroll_pattern_is_in_the_cache_request():
    """ScrollPattern 必须在 CacheRequest 里，否则「可滚动」永远不显示。

    踩过：探针用 `Get-CachedPattern`，而缓存请求里没登记 ScrollPattern，
    于是恒为空；动作侧用的是 live 探测又能滚——变成"能用但看不见"，
    模型永远不知道该调它。
    """
    script = desktop.worker_script()
    cache_section = script.split('function New-TreeCacheRequest', 1)[1]
    cache_section = cache_section.split('function ', 1)[0]
    assert 'ScrollPattern' in cache_section


def test_scroll_uses_the_amount_parameter():
    """回归：滚动分支曾用 `$step`（不存在的变量），恒为 0 → 永远只滚一步。"""
    script = desktop.worker_script()
    branch = script.split("$operation -eq 'scroll'", 1)[1].split('function ', 1)[0]
    assert '$amount' in branch
    # 代码行（去掉注释）里不能再出现 $step
    code = [l for l in branch.splitlines() if not l.lstrip().startswith('#')]
    # $steps 是合法变量，要查的是「正好叫 $step」的那个。用运行期拼串构造正则，
    # 免得反斜杠转义在编辑器/heredoc 里被吃掉一次（这个坑已经踩过好几回）。
    token = chr(36) + 'step'
    pattern = re.escape(token) + '(?![A-Za-z0-9_])'
    assert not [line for line in code if re.search(pattern, line)]


def test_select_operation_is_advertised(monkeypatch):
    """回归：把 select 探针挪进状态块时漏了 patterns.Add('select')，
    列表项一度显示成「只能按坐标点」，等于不能选中。"""
    script = desktop.worker_script()
    start = script.index('$selection = Get-CachedPattern')
    # 取「探针 -> 处理」这一小段，确认同一个 if 里既读了状态也登记了操作。
    block = script[start:start + 400]
    assert "$patterns.Add('select')" in block


def test_state_text_never_shows_the_raw_control_type():
    """焦点控件没名字时要翻译，不能把 `ControlType.Menu` 打给用户。

    实测右键弹出菜单后焦点就是一个无名 Menu，`_state_text` 直接把程序化枚举
    拼进「焦点「...」」里了。
    """
    state = desktop._state(_state(focused_name='',
                                  focused_control_type='ControlType.Menu'))
    text = tool_desktop._state_text(state)
    assert 'ControlType' not in text
    assert '菜单' in text


def test_state_text_falls_back_to_a_readable_placeholder():
    state = desktop._state(_state(focused_name='', focused_control_type=''))
    text = tool_desktop._state_text(state)
    assert '未知控件' in text
    assert 'ControlType' not in text


def test_tree_walk_merges_duplicate_rows():
    """同一控件被走两遍时（ComboBox 展开就是），合并成一条并保留能力。

    实测：展开后「列表项 上海」出现两次，一次没有操作、一次可选中，
    坐标完全相同——模型大概率挑第一个（没操作的那份）。
    """
    script = desktop.worker_script()
    walk = script.split('function Get-UiTree', 1)[1].split('function Get-TreeBudget', 1)[0]
    assert '$seen' in walk and 'ContainsKey' in walk
    # 合并时元素也要换：否则列出的能力用不上
    assert '$elements[$slot] = $el' in walk


def test_state_only_change_skips_the_retry():
    """只改状态（勾选/写入）时不该再走「没有变化」的复查。

    指纹刻意不含 state，所以 toggle 一个复选框时指纹必然不变；如果只看指纹，
    每次勾选都要白付 140ms 加一次遍历。实测 toggle 约 520ms（含复查时约 660ms）。
    """
    branch = _ps_function(desktop.worker_script(), 'Invoke-ElementAction')
    assert '$targetChanged' in branch
    # 复查条件里必须排除 state 变化
    retry_cond = branch.split('-not $targetChanged')[1].split('{', 1)[0]
    assert 'fingerprint -eq' in retry_cond
    # 状态/值的比较要在复查之前
    assert branch.index('$targetChanged = $true') < branch.index('Start-Sleep -Milliseconds 140')


def test_no_change_answer_is_double_checked():
    """「没有变化」是最该复核的答案——模型读到它会以为动作没生效然后重试。

    实测右键菜单：点「复制」后弹出层关闭带延迟，40ms 时树还是旧的，回执说
    「界面没有变化」；其实已经关了。所以只在"确实看不出变化"时多等一次。
    """
    branch = _ps_function(desktop.worker_script(), 'Invoke-ElementAction')
    assert 'retry' in branch
    assert 'Start-Sleep -Milliseconds 140' in branch
    # 只在指纹相同时才复查，常见路径不多付代价
    assert 'fingerprint -eq [string]$tree.fingerprint' in branch


def test_one_container_cannot_eat_the_whole_budget():
    """单个父节点贡献的子项要设上限。

    实测文件对话框：文件夹列表每行拆成「名称/日期/类型」三个输入框，14 行就占了
    42 个名额；文件一多，120 的上限会先被表格吃满，而「文件名」和「打开」排在
    后面根本进不来——恰恰是要点的两个控件。
    """
    script = desktop.worker_script()
    assert 'TREE_CHILDREN_PER_PARENT' in script
    walk = script.split('function Get-UiTree', 1)[1].split('function Get-TreeBudget', 1)[0]
    assert 'TREE_CHILDREN_PER_PARENT' in walk
    # 超限要标 truncated，别让模型以为列表是完整的
    assert '$truncated = $true' in walk


def test_element_action_falls_back_to_a_click_when_the_control_has_no_pattern():
    """WPF 的 ComboBox 弹出项不支持 SelectionItem，用户就是点它一下。"""
    branch = _ps_function(desktop.worker_script(), 'Invoke-ElementAction')
    assert 'operation_unsupported' in branch
    assert 'Invoke-Step $clickStep' in branch
    assert '已改为点击它的中心' in branch


def test_rows_without_actions_or_bounds_are_dropped():
    """一行值得列出，是因为模型能对它做点什么：有元素级操作，或有坐标。

    实测出现过「#5 面板 · 只能按坐标点」——既没坐标也没操作，"按坐标点"是空话。
    """
    script = desktop.worker_script()
    node = _ps_function(script, 'Read-ElementNode')
    assert '$patterns.Count -eq 0 -and $bounds.Count -ne 4' in node


def test_no_coordinate_hint_without_coordinates(monkeypatch):
    _patch_tree(monkeypatch, _tree(desktop._element_info(
        _item(id=1, name='面板', operations=[], bounds=None), 1)))
    text = tool_desktop.inspect()
    assert '只能按坐标点' not in text


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


def test_ui_forbidden_check_covers_name_value_and_label():
    """第四档要同时看名字、值和类型标签。

    有些控件没有 Name 只有 Value（或反过来）。只匹配 name 的话，
    「无名字但值是『立即支付』」的控件就漏过去了——实测确认过。
    """
    script = desktop.worker_script()
    section = script.split('must_do_self', 1)[0]
    tail = section.rsplit('forbidden', 1)[-1]
    assert 'node.name' in tail and 'node.value' in tail and 'node.label' in tail


def test_tree_walk_rechecks_the_budget_after_findall():
    """FindAll 是一次跨进程调用，本身不受节点间 deadline 约束。

    实测：PDF 页面的子树 26154 个节点要 30 秒。回来之后必须立刻检查一次预算，
    否则「设了时间上限」只是摆设。
    """
    script = desktop.worker_script()
    assert 'FindAll 本身是' in script
    walk = script.split('function Get-UiTree', 1)[1].split('function Get-TreeBudget', 1)[0]
    # FindAll 之后必须紧跟一次超时检查
    after_find = walk.split('FindAll(', 1)[1]
    assert 'maxMs' in after_find.split('$stack.Push', 1)[0]


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


def test_every_desktop_tool_is_registered_everywhere():
    """六个桌面工具在八个登记点上必须齐全。

    这套代码里"加一个工具"要动 8 个地方（schema / handler / 策略 / 定时禁用 /
    子代理禁用 / 同类键 / 中文标签 / 审批文案）。漏一个不会报错，只会少一层
    保护或少一句人话——所以用矩阵守着，而不是靠记。
    """
    import feishu_audit_card  # noqa: F401
    import hooks
    import policy_bash
    import subagent
    import tool_schemas
    import tools
    from exec_policy import DESKTOP_TOOLS

    schemas = {item['function']['name'] for item in tool_schemas.TOOL_SCHEMAS}
    subagent_src = open(subagent.__file__, encoding='utf-8').read()
    import approval_view

    for tool in DESKTOP_TOOLS:
        assert tool in schemas, tool
        assert tool in tools.tool_registry, tool
        assert tool in hooks.SCHEDULED_FORBIDDEN_TOOLS, tool
        assert f"'{tool}'" in subagent_src, tool
        assert policy_bash.approval_class(tool, {}) == '', tool
        assert tool in approval_view._TOOL_LABELS, tool
        # 审批文案必须走专用分支（退化成参数对说明漏了登记）
        assert 'desktop input' not in approval_view.detail(tool, {}), tool


def test_every_desktop_audit_event_has_a_label_and_icon():
    import audit
    import feishu_audit_card

    events = [name for name in audit.AUDIT_EVENTS if name.startswith('desktop')]
    assert events
    for event in events:
        assert event in audit.EVENT_LABELS, event
        assert event in feishu_audit_card._ICONS, event


def test_inspect_is_forbidden_in_scheduled_turns():
    import hooks

    assert 'desktop_inspect' in hooks.SCHEDULED_FORBIDDEN_TOOLS


def test_inspect_is_forbidden_for_subagents():
    import subagent

    source = open(subagent.__file__, encoding='utf-8').read()
    assert "'desktop_inspect'" in source


# ---------- 动作回执：界面变没变 ----------

def _result(**over):
    base = {'action': 'element', 'state': desktop._state(_state()),
            'duration_ms': 20,
            'target': desktop._element_info(_item(id=3, name='高级选项'), 3)}
    base.update(over)
    return desktop.ActionResult(**base)


def _patch_element(monkeypatch, result):
    monkeypatch.setattr(desktop, 'element_action', lambda *a, **kw: result)


def test_element_result_reports_state_change(monkeypatch):
    _patch_element(monkeypatch, _result(
        target_after=desktop._element_info(
            _item(id=3, name='高级选项', state=['expanded']), 3),
        fingerprint_before=FP, fingerprint_after=FP))
    text = tool_desktop.element_action(
        operation='expand', element=3, tree=FP, expected_pid=123,
        expected_window_id='ABCD')
    assert '已完成展开「高级选项」' in text
    assert '现在是已展开' in text
    text.encode('gbk')


def test_element_result_lists_newly_appeared_controls(monkeypatch):
    """展开/点开之后最常问的下一句是「现在有什么」——直接给，省一次 inspect。"""
    _patch_element(monkeypatch, _result(
        items_added=(desktop._element_info(
            _item(id=11, label='文本', name='这里是展开后才有的内容'), 11),),
        fingerprint_before=FP, fingerprint_after='b' * 40))
    text = tool_desktop.element_action(
        operation='expand', element=3, tree=FP, expected_pid=123,
        expected_window_id='ABCD')
    assert '多出 1 个控件' in text
    assert '#11 文本 "这里是展开后才有的内容"' in text
    assert 'b' * 40 in text          # 新指纹可以直接拿去接着操作


def test_element_result_caps_the_new_control_list(monkeypatch):
    added = tuple(desktop._element_info(_item(id=i, name=f'新控件{i}'), i)
                  for i in range(1, 13))
    _patch_element(monkeypatch, _result(items_added=added,
                                        fingerprint_before=FP,
                                        fingerprint_after='b' * 40))
    text = tool_desktop.element_action(
        operation='expand', element=3, tree=FP, expected_pid=123,
        expected_window_id='ABCD')
    assert '多出 12 个控件' in text
    assert '还有 4 个新控件' in text


def test_operation_note_is_reset_per_request():
    """worker 是常驻进程：一次滚动留下的 note 不能挂到后面某次点击上。"""
    script = desktop.worker_script()
    entry = script.split('function Invoke-HarnessOp', 1)[1].split('$payload =', 1)[0]
    assert 'OperationNote' in entry


def test_state_change_does_not_contradict_itself(monkeypatch):
    """状态变了就别再说「界面没有变化」——两句话会互相打架。"""
    _patch_element(monkeypatch, _result(
        target_after=desktop._element_info(
            _item(id=3, name='记住我', state=['checked']), 3),
        fingerprint_before=FP, fingerprint_after=FP))
    text = tool_desktop.element_action(
        operation='toggle', element=3, tree=FP, expected_pid=123,
        expected_window_id='ABCD')
    assert '现在是已勾选' in text
    assert '界面没有变化' not in text


def test_element_result_admits_when_it_cannot_read_the_tree(monkeypatch):
    """读不到界面时不能编一句「界面没有变化」。"""
    _patch_element(monkeypatch, _result(fingerprint_before=FP,
                                        fingerprint_after=''))
    text = tool_desktop.element_action(
        operation='invoke', element=3, tree=FP, expected_pid=123,
        expected_window_id='ABCD')
    assert '界面没有变化' not in text
    assert '读不到界面状态' in text


def test_element_result_flags_truncated_after_tree(monkeypatch):
    _patch_element(monkeypatch, _result(
        items_added=(desktop._element_info(_item(id=11, name='新'), 11),),
        items_after_truncated=True,
        fingerprint_before=FP, fingerprint_after='b' * 40))
    text = tool_desktop.element_action(
        operation='expand', element=3, tree=FP, expected_pid=123,
        expected_window_id='ABCD')
    assert '被截断' in text


def test_after_action_text_has_no_blank_lines(monkeypatch):
    _patch_element(monkeypatch, _result(
        target_after=desktop._element_info(
            _item(id=3, name='记住我', state=['checked']), 3),
        fingerprint_before=FP, fingerprint_after=FP))
    text = tool_desktop.element_action(
        operation='toggle', element=3, tree=FP, expected_pid=123,
        expected_window_id='ABCD')
    assert chr(10) + chr(10) not in text


def test_element_result_says_when_nothing_changed(monkeypatch):
    """没变化也要说——不然模型会怀疑动作没生效然后重试。"""
    _patch_element(monkeypatch, _result(
        fingerprint_before=FP, fingerprint_after=FP))
    text = tool_desktop.element_action(
        operation='toggle', element=3, tree=FP, expected_pid=123,
        expected_window_id='ABCD')
    assert '界面没有变化' in text
    assert FP in text


def test_window_switch_is_announced_in_the_receipt(monkeypatch):
    """点按钮弹出对话框时，回执必须说前台换了窗口。

    实测 WPF MessageBox：动作前 180A2E、动作后 E1548，回执只说"多出 5 个控件"，
    还让模型"用上面的编号配这个指纹"——模型拿旧 pid 去操作新窗口必然失败。
    """
    _patch_element(monkeypatch, _result(
        before=desktop._state(_state(window_id='180A2E')),
        state=desktop._state(_state(title='确认', pid=999, window_id='E1548')),
        fingerprint_before=FP, fingerprint_after='b' * 40))
    text = tool_desktop.element_action(
        operation='invoke', element=3, tree=FP, expected_pid=123,
        expected_window_id='180A2E')
    assert '前台窗口换成了「确认」' in text
    assert 'pid 999' in text and 'E1548' in text
    assert '要用这个 pid 和窗口号' in text


def test_same_window_receipt_does_not_mention_a_switch(monkeypatch):
    _patch_element(monkeypatch, _result(
        before=desktop._state(_state()), state=desktop._state(_state()),
        fingerprint_before=FP, fingerprint_after=FP))
    text = tool_desktop.element_action(
        operation='invoke', element=3, tree=FP, expected_pid=123,
        expected_window_id='ABCD')
    assert '前台窗口换成了' not in text


def test_element_action_reads_state_before(monkeypatch):
    """state_before 必须被解析，否则「前台换窗口」根本说不了。"""
    seen = {}

    def fake(payload, **kwargs):
        seen['before'] = payload
        return {'ok': True, 'action': 'element', 'operation': 'invoke',
                'element': 1, 'target': _item(),
                'state_before': _state(window_id='180A2E'),
                'state_after': _state(window_id='E1548'),
                'fingerprint_before': FP, 'fingerprint_after': 'b' * 40}

    monkeypatch.setattr(desktop, '_run', fake)
    result = desktop.element_action('invoke', 1, FP, **IDENTITY)
    assert result.before is not None
    assert result.before.window_id == '180A2E'


def test_after_tree_is_walked_on_the_new_window_when_foreground_switched():
    """窗口换了就要按新窗口重新遍历，否则回执是自相矛盾的。"""
    branch = _ps_function(desktop.worker_script(), 'Invoke-ElementAction')
    assert 'walkTarget' in branch
    assert '$after = Get-WindowState' in branch
    # 先读状态、再决定走哪棵树
    assert branch.index('Get-WindowState') < branch.index('walkTarget')


def test_focus_reuses_the_tree_instead_of_walking_again():
    """focus 只改键盘焦点，复用刚走过的树即可——省掉一次 600ms 级遍历。"""
    branch = _ps_function(desktop.worker_script(), 'Invoke-ElementAction')
    assert '$isFocus' in branch
    assert '$afterTree = $tree' in branch


def test_invalid_operation_message_lists_every_operation():
    """新增操作后忘了更新提示，模型就只能看到「不支持」却不知道支持什么。"""
    script = desktop.worker_script()
    tail = script.rsplit('operation 只能是', 1)[1]
    for op in ('invoke', 'setvalue', 'toggle', 'select', 'expand', 'collapse',
               'scroll', 'focus'):
        assert op in tail, op


def test_unsupported_message_says_what_the_control_supports():
    """报「不支持」时要顺带说「它支持什么」，否则模型只能一个个试。"""
    script = desktop.worker_script()
    assert 'function Get-OperationHint' in script
    assert '它支持：' in script
    assert '没有可用的元素级操作' in script


def test_every_element_operation_has_a_chinese_label():
    """新增操作时三处文案都要覆盖，否则会静默退化成英文枚举。

    踩过两次：加 `scroll` 时漏了审批卡和活动行，界面上显示「操作」。
    表已经收敛成一张（approval_view 里），这条守着它别掉队。
    """
    import approval_view

    for operation in desktop.ELEMENT_OPERATIONS:
        assert operation in approval_view.ELEMENT_OPERATION_LABELS, operation
        assert operation in approval_view._ELEMENT_OPERATION_LABELS, operation
        assert re.search(r'[一-鿿]',
                         approval_view.ELEMENT_OPERATION_LABELS[operation]), operation


def test_scroll_has_a_chinese_verb_everywhere():
    """审批卡和活动行都要能说「滚动」，不能退化成「操作」。"""
    import approval_view
    import tool_activity

    args = {'action': 'element', 'element': 5, 'operation': 'scroll',
            'expect': '选项列表', 'amount': 3,
            'expected_pid': 1, 'expected_window_id': 'AB'}
    card = approval_view.detail('desktop_action', args)
    assert '滚动该控件' in card and '向下 3 格' in card
    line = tool_activity.format_line('desktop_action', args, 0.3)
    assert '滚动' in line and '操作' not in line.replace('操作键鼠', '')


def test_setvalue_reports_the_value_actually_read_back(monkeypatch):
    """写入之后读回实际值：界面可能格式化/截断/校验，这是唯一可靠的确认。"""
    _patch_element(monkeypatch, _result(
        operation='setvalue',
        target=desktop._element_info(_item(id=5, label='输入框', value='旧'), 5),
        target_after=desktop._element_info(
            _item(id=5, label='输入框', value='2026-10-28'), 5),
        fingerprint_before=FP, fingerprint_after=FP))
    text = tool_desktop.element_action(
        operation='setvalue', element=5, tree=FP, text='2026/10/28',
        expected_pid=123, expected_window_id='ABCD')
    assert '读回内容 "2026-10-28"' in text


def test_scroll_reports_when_it_is_already_at_the_edge():
    """到顶/到底要说出来，否则模型会一直滚（每次一轮往返 + 一次审批）。"""
    script = desktop.worker_script()
    branch = script.split("$operation -eq 'scroll'", 1)[1]
    assert '已经滚到底了' in branch
    assert '已经滚到顶了' in branch
    assert 'VerticalScrollPercent' in branch


def test_scroll_target_search_is_its_own_function():
    """滚动目标搜索（自己 → 祖先 → 后代）从操作分发里抽出来了。

    原来那 40 行内联在 Invoke-ElementOperation 里，加上到顶到底判断整段近 90 行。
    """
    script = desktop.worker_script()
    assert 'function Find-ScrollablePattern' in script
    branch = script.split("$operation -eq 'scroll'", 1)[1].split("$operation -eq 'focus'", 1)[0]
    assert 'Find-ScrollablePattern $el' in branch
    # 内联搜索不该再有
    assert 'GetParent' not in branch


def test_scroll_operation_needs_amount():
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_element_action('scroll', 3, FP, **IDENTITY)
    payload = desktop.validate_element_action('scroll', 3, FP, amount=-2, **IDENTITY)
    assert payload['amount'] == -2
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_element_action('scroll', 3, FP, amount=0, **IDENTITY)
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_element_action('invoke', 3, FP, amount=2, **IDENTITY)


# ---------- 跨模块一致性 ----------

def test_every_tool_has_a_recovery_classification():
    """恢复分类要覆盖所有工具，否则读操作会被当成「可能重复副作用」。

    踩过：加了 `desktop_inspect` / `desktop_wait` / `desktop_displays` 之后
    忘了登记，中断恢复时会告诉模型「先检查状态再重试，可能已部分生效」——
    对纯读工具是误导。`bash` 例外：按命令文本动态判定。
    """
    import harness  # noqa: F401  导入期把 task/checkpoint/compact 注册进表
    import tool_recovery
    import tools

    known = (tool_recovery.READ_ONLY_TOOLS | tool_recovery.IDEMPOTENT_TOOLS
             | tool_recovery.DESTRUCTIVE_TOOLS | {'bash'})
    missing = sorted(set(tools.tool_registry) - known)
    assert not missing, f'unclassified tools: {missing}'
    # 三张表不能有交集：一个工具只能有一个恢复语义。
    assert not tool_recovery.READ_ONLY_TOOLS & tool_recovery.IDEMPOTENT_TOOLS
    assert not tool_recovery.IDEMPOTENT_TOOLS & tool_recovery.DESTRUCTIVE_TOOLS
    assert not tool_recovery.READ_ONLY_TOOLS & tool_recovery.DESTRUCTIVE_TOOLS


def test_read_only_desktop_tools_are_classified_read_only():
    import tool_recovery

    for tool in ('desktop_inspect', 'desktop_displays', 'desktop_wait',
                 'desktop_snapshot', 'desktop_windows'):
        assert tool_recovery.classify(tool, {}) == tool_recovery.READ_ONLY, tool
    assert tool_recovery.classify('desktop_action', {}) == tool_recovery.DESTRUCTIVE


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
