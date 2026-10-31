"""desktop_wait：条件校验、结果解析、文案，以及动作后的变化摘要。

真实轮询在 PowerShell 侧，这里注入 ``_run`` / monkeypatch 能力层，不碰桌面。
"""
import pytest

import desktop_control_windows as desktop
import tool_desktop
from exec_policy import ASK, DENY, Policy

IDENTITY = {'expected_pid': 123, 'expected_window_id': 'ABCD'}


def _state(**over):
    base = {
        'available': True, 'title': '记事本', 'process': 'notepad', 'pid': 123,
        'window_id': 'ABCD', 'rect': [0, 0, 800, 600], 'focused_name': '',
        'focused_control_type': '', 'focused_is_password': False,
        'sensitive_target': False,
    }
    base.update(over)
    return base


def _element(**over):
    base = {'id': 3, 'label': '按钮', 'name': '已保存', 'value': '',
            'password': False, 'enabled': True, 'focusable': True,
            'focused': False, 'bounds': [10, 20, 30, 40],
            'operations': ['invoke']}
    base.update(over)
    return base


def _result(**over):
    base = {'matched': True, 'target': 'control', 'elapsed_ms': 1200, 'polls': 6,
            'state': _state(), 'element': _element(), 'fingerprint': 'a1b2c3d4e5f60718',
            'elements': 5, 'tree_truncated': False}
    base.update(over)
    return base


# ---------- 校验 ----------

def test_wait_requires_text():
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_wait('control', '   ', 'present', 1000)


def test_wait_rejects_unknown_target_and_state():
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_wait('desktop', '保存', 'present', 1000)
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_wait('control', '保存', 'visible', 1000)


@pytest.mark.parametrize('timeout', [0, 100, 30001, 'soon'])
def test_wait_clamps_timeout_range(timeout):
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_wait('control', '保存', 'present', timeout)


def test_wait_defaults_timeout_and_normalizes_text():
    payload = desktop.validate_wait('control', '  已   保存 ', None, None)
    assert payload['text'] == '已 保存'
    assert payload['timeout_ms'] == desktop.TOOL_WAIT_DEFAULT_MS
    assert payload['state'] == 'present'


def test_wait_requires_identity_pair():
    with pytest.raises(desktop.DesktopControlError):
        desktop.validate_wait('control', '保存', 'present', 1000,
                              expected_pid=123)


def test_wait_passes_identity_when_given():
    payload = desktop.validate_wait('control', '保存', 'present', 1000, **IDENTITY)
    assert payload['expected_pid'] == 123
    assert payload['expected_window_id'] == 'ABCD'


def test_wait_only_sends_exclusions_for_control(monkeypatch):
    monkeypatch.setenv('HARNESS_DESKTOP_EXCLUDE_PROCESSES', 'Code.exe')
    assert desktop.validate_wait('control', 'x', 'present', 1000)['exclude'] == ['Code.exe']
    assert 'exclude' not in desktop.validate_wait('window', 'x', 'present', 1000)


# ---------- 解析 ----------

def test_wait_parses_result(monkeypatch):
    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return {'ok': True, 'result': _result()}

    monkeypatch.setattr(desktop, '_run', fake)
    result = desktop.wait('control', '已保存', 'present', 3000)
    assert seen['action'] == 'wait'
    assert seen['timeout_ms'] == 3000
    assert result.matched is True
    assert result.elapsed_ms == 1200
    assert result.polls == 6
    assert result.element is not None and result.element.name == '已保存'
    assert result.fingerprint == 'a1b2c3d4e5f60718'


def test_wait_parses_timeout_result(monkeypatch):
    monkeypatch.setattr(desktop, '_run', lambda payload, **kw: {
        'ok': True, 'result': _result(matched=False, element=None, polls=9,
                                      elapsed_ms=5000, elements=12)})
    result = desktop.wait('control', '已保存', 'present', 5000)
    assert result.matched is False
    assert result.element is None
    assert result.elements == 12


def test_wait_parses_window_result(monkeypatch):
    monkeypatch.setattr(desktop, '_run', lambda payload, **kw: {
        'ok': True, 'result': {'matched': True, 'target': 'window',
                               'elapsed_ms': 400, 'polls': 4,
                               'state': _state(),
                               'window': {'title': '记事本', 'process': 'notepad',
                                          'pid': 123, 'window_id': 'ABCD'},
                               'windows': 7}})
    result = desktop.wait('window', '记事本', 'present', 3000)
    assert result.target == 'window'
    assert result.window['title'] == '记事本'
    assert result.windows == 7


# ---------- 文案 ----------

def _patch_wait(monkeypatch, result):
    monkeypatch.setattr(desktop, 'wait', lambda *a, **kw: result)


def test_wait_text_reports_control_found(monkeypatch):
    _patch_wait(monkeypatch, desktop.WaitResult(
        matched=True, target='control', desired='present', elapsed_ms=1200,
        polls=6, state=desktop._state(_state()),
        element=desktop._element_info(_element(), 3),
        fingerprint='a1b2c3d4e5f60718', elements=5))
    text = tool_desktop.wait(text='已保存')
    assert '等到了' in text and '已保存' in text
    assert '1.2 秒' in text
    assert '#3 按钮' in text
    assert 'a1b2c3d4e5f60718' in text
    text.encode('gbk')


def test_window_wait_timeout_reports_both_counts(monkeypatch):
    """超时提示里的"可见窗口 N 个"不能是被过滤后的数字。

    target=foreground 时列表先被筛成 1 个，只说这个数会让模型以为桌面只有一个窗口。
    """
    _patch_wait(monkeypatch, desktop.WaitResult(
        matched=False, target='foreground', desired='present', elapsed_ms=600,
        polls=3, state=desktop._state(_state()), windows=1, windows_total=14))
    text = tool_desktop.wait(text='保存', target='foreground', timeout_ms=600)
    assert '可见窗口 14 个' in text
    assert '符合条件' in text and '1 个' in text
    text.encode('gbk')

    # 没有过滤时只说一个数字，不要多出半句话
    _patch_wait(monkeypatch, desktop.WaitResult(
        matched=False, target='window', desired='present', elapsed_ms=600,
        polls=3, state=desktop._state(_state()), windows=9, windows_total=9))
    text = tool_desktop.wait(text='保存', target='window', timeout_ms=600)
    assert '可见窗口 9 个' in text
    assert '符合条件' not in text


def test_wait_text_reports_disappeared(monkeypatch):
    _patch_wait(monkeypatch, desktop.WaitResult(
        matched=True, target='control', desired='absent', elapsed_ms=800,
        polls=4, state=desktop._state(_state())))
    text = tool_desktop.wait(text='正在加载', state='absent')
    assert '已消失' in text
    assert '0.8 秒' in text


def test_absent_wait_is_hedged_when_the_tree_was_incomplete(monkeypatch):
    """「没找到」只有在遍历完整时等于「不在了」。

    轮询用缩小预算（80 个控件），大窗口会被截断；读不到树时更只能说明"看不见"。
    这时说一句「已消失」，模型就会当成对话框没了，接着去点被它挡住的东西。
    """
    _patch_wait(monkeypatch, desktop.WaitResult(
        matched=True, target='control', desired='absent', elapsed_ms=800,
        polls=4, state=desktop._state(_state()), elements=80, truncated=True,
        authoritative=False))
    text = tool_desktop.wait(text='正在加载', state='absent')
    assert '已消失' not in text
    assert '没再出现' in text
    assert '不等于它不在了' in text
    text.encode('gbk')

    # 读不到界面（遍历失败 / 窗口刚关）时的说法
    _patch_wait(monkeypatch, desktop.WaitResult(
        matched=True, target='control', desired='absent', elapsed_ms=300,
        polls=2, state=desktop._state(_state()), elements=0,
        authoritative=False))
    text = tool_desktop.wait(text='正在加载', state='absent')
    assert '没读到界面结构' in text
    assert '已消失' not in text


def test_wait_text_reports_timeout_with_current_state(monkeypatch):
    _patch_wait(monkeypatch, desktop.WaitResult(
        matched=False, target='control', desired='present', elapsed_ms=5000,
        polls=9, state=desktop._state(_state(title='其它窗口')), elements=12))
    text = tool_desktop.wait(text='已保存')
    assert '没等到' in text
    assert '其它窗口' in text
    assert '可读控件 12 个' in text
    assert 'timeout_ms' in text
    text.encode('gbk')


def test_wait_timeout_says_so_when_the_walk_was_truncated(monkeypatch):
    """遍历被截断时，「没看到」不等于「不存在」，必须说清楚。"""
    _patch_wait(monkeypatch, desktop.WaitResult(
        matched=False, target='control', desired='present', elapsed_ms=5000,
        polls=9, state=desktop._state(_state()), elements=80, truncated=True))
    text = tool_desktop.wait(text='保存')
    assert '被截断' in text
    assert 'timeout_ms 调大' not in text
    text.encode('gbk')


def test_wait_timeout_without_truncation_suggests_next_steps(monkeypatch):
    _patch_wait(monkeypatch, desktop.WaitResult(
        matched=False, target='control', desired='present', elapsed_ms=5000,
        polls=9, state=desktop._state(_state()), elements=5, truncated=False))
    text = tool_desktop.wait(text='保存')
    assert 'timeout_ms' in text
    assert '被截断' not in text


def test_wait_text_reports_window(monkeypatch):
    _patch_wait(monkeypatch, desktop.WaitResult(
        matched=True, target='window', desired='present', elapsed_ms=400,
        polls=4, state=desktop._state(_state()),
        window={'title': '记事本 - 新文件', 'process': 'notepad', 'pid': 42,
                'window_id': 'BEEF'}, windows=7))
    text = tool_desktop.wait(text='记事本', target='window')
    assert '记事本 - 新文件' in text
    assert 'pid 42' in text and 'BEEF' in text


def test_wait_text_reports_window_timeout(monkeypatch):
    _patch_wait(monkeypatch, desktop.WaitResult(
        matched=False, target='window', desired='present', elapsed_ms=3000,
        polls=30, state=desktop._state(_state()), windows=5))
    text = tool_desktop.wait(text='安装', target='window')
    assert '没等到' in text
    assert '可见窗口 5 个' in text


def test_refusals_are_reported_as_refusals(monkeypatch):
    """拒绝 ≠ 失败。

    「这个窗口是密码页面，不提供控件信息」被套成「等待失败：…」再附一句
    「换个办法试试」，等于教模型重试同一件事——它该做的是请用户自己操作。
    """
    def boom(*_a, **_kw):
        raise desktop.DesktopControlError(
            'sensitive_target: 这个窗口看起来是密码页面，不提供控件信息')
    monkeypatch.setattr(desktop, "wait", boom)
    text = tool_desktop.wait(text='输入框', timeout_ms=200)
    assert text.startswith('sensitive_target:')
    assert '等待失败' not in text


def test_wait_matching_ignores_the_control_type_label():
    """等待按「名字 + 值」匹配，和 expect 核对同一套判据。

    以前还搜类型标签，于是 `wait(text='按钮')` 会命中任意一个按钮——而 schema 里
    写的是「控件名或值」。两边口径不一致时，模型只能靠猜。
    """
    script = desktop.worker_script()
    body = script.split('function Find-TreeMatch', 1)[1].split(chr(10) + 'function ', 1)[0]
    assert 'item.name' in body and 'item.value' in body
    assert 'item.label' not in body


def test_wait_text_is_cleaned_before_printing(monkeypatch):
    """等待的目标文本也可能带零宽字符（控件名里实测有），统一清洗一次。"""
    _patch_wait(monkeypatch, desktop.WaitResult(
        matched=False, target='control', desired='present', elapsed_ms=300,
        polls=2, state=desktop._state(_state()), elements=3))
    text = tool_desktop.wait(text='保存​按钮')
    text.encode('gbk')
    assert '​' not in text
    assert '保存按钮' in text


def test_wait_failure_is_actionable(monkeypatch):
    def boom(*args, **kwargs):
        raise desktop.DesktopControlError('no_foreground_window: no foreground window')

    monkeypatch.setattr(desktop, 'wait', boom)
    text = tool_desktop.wait(text='保存')
    assert '等待失败' in text
    assert 'no foreground window' in text


# ---------- 动作后的变化摘要 ----------

def _patch_action(monkeypatch, before, after):
    def fake(*args, **kwargs):
        return desktop.ActionResult(
            action='click', state=desktop._state(after), duration_ms=12,
            before=desktop._state(before))

    monkeypatch.setattr(desktop, 'action', fake)


def test_action_reports_focus_change(monkeypatch):
    _patch_action(monkeypatch, _state(focused_name='搜索'),
                  _state(focused_name='保存'))
    text = tool_desktop.action('click', x=1, y=2, **IDENTITY)
    assert '已完成点击' in text
    assert '焦点从「搜索」变成「保存」' in text
    text.encode('gbk')


def test_action_reports_foreground_switch(monkeypatch):
    _patch_action(monkeypatch, _state(), _state(window_id='FFFF', title='另一个窗口'))
    text = tool_desktop.action('click', x=1, y=2, **IDENTITY)
    assert '前台窗口换成了「另一个窗口」' in text


def test_action_says_what_it_cannot_know(monkeypatch):
    """看不到变化不等于没生效：措辞必须留余地，否则模型会误判失败。"""
    _patch_action(monkeypatch, _state(), _state())
    text = tool_desktop.action('click', x=1, y=2, **IDENTITY)
    assert '都没变' in text
    assert '不代表' in text


def test_action_without_before_still_works(monkeypatch):
    monkeypatch.setattr(desktop, 'action', lambda *a, **kw: desktop.ActionResult(
        action='key', state=desktop._state(_state()), duration_ms=5))
    text = tool_desktop.action('key', key='enter', **IDENTITY)
    assert '已完成按键' in text
    assert '都没变' not in text


# ---------- 策略 ----------

def test_wait_is_off_by_default(monkeypatch):
    monkeypatch.delenv('HARNESS_DESKTOP_CONTROL', raising=False)
    assert Policy().evaluate('desktop_wait', {'text': '保存'}).action == DENY


def test_wait_requires_approval_when_enabled(monkeypatch):
    monkeypatch.setenv('HARNESS_DESKTOP_CONTROL', '1')
    decision = Policy().evaluate('desktop_wait', {'text': '保存'})
    assert decision.action == ASK
    assert decision.code == 'desktop_control'


def test_wait_never_auto_allowed_by_tier(monkeypatch):
    monkeypatch.setenv('HARNESS_DESKTOP_CONTROL', '1')
    decision = Policy(sandbox_mode='danger-full-access',
                      approval_policy='never').evaluate('desktop_wait', {'text': 'x'})
    assert decision.action == DENY


def test_no_desktop_tool_gets_a_reusable_session_key():
    """桌面工具一律不给「同会话同类免审」的键。

    踩过：新增 `desktop_displays` / `desktop_wait` 时都要手改这份清单，
    而 `desktop_windows` 一直漏在里面。漏了不会立刻出问题（没有消费方），
    但它把「每次都问」的约定变成了靠人记——用 DESKTOP_TOOLS 收口。
    """
    import exec_policy
    import policy_bash

    for tool in exec_policy.DESKTOP_TOOLS:
        assert policy_bash.approval_class(tool, {}) == '', tool


def test_wait_is_forbidden_in_scheduled_turns():
    import hooks

    assert 'desktop_wait' in hooks.SCHEDULED_FORBIDDEN_TOOLS


def test_wait_refuses_sensitive_windows_like_inspect():
    """敏感窗口上不能按控件等待——否则等于绕过 inspect 的拒绝。

    实测过：这里漏掉判断，就能用「猜名字 + 等它出现」把密码窗口里的
    输入框内容原样读出来（value 一起返回），而 inspect 对同一窗口是拒绝的。
    """
    script = desktop.worker_script()
    # 目标为 control 的分支里必须同时有 sensitive 判断和 exclude 判断。
    control_branch = script.split("$payload.action -eq 'wait'", 1)[1]
    control_branch = control_branch.split("$payload.action -eq 'inspect'", 1)[0]
    assert 'sensitive_target' in control_branch
    assert 'process_excluded' in control_branch


def test_wait_honours_the_timeout_budget():
    """每次遍历的预算不能超过剩余时间，否则「等 600ms」会跑成 4.4 秒。

    实测过：deadline 只在两轮之间检查，而单次遍历能烧 900ms，
    于是短预算被无限放大。两处都要收紧：poll 的遍历预算、窗口等待的 sleep。
    """
    script = desktop.worker_script()
    assert '$remaining' in script and 'pollMs' in script
    window_wait = script.split('function Wait-ForWindow', 1)[1].split('function ', 1)[0]
    assert '$left' in window_wait


def test_wait_text_tells_the_model_how_to_act(monkeypatch):
    """命中后的文案要带 pid / 窗口号——element 模式的必填参数就是它们。"""
    _patch_wait(monkeypatch, desktop.WaitResult(
        matched=True, target='control', desired='present', elapsed_ms=300,
        polls=1, state=desktop._state(_state(pid=23844, window_id='60D5E')),
        element=desktop._element_info(_element(), 3),
        fingerprint='a1b2c3d4e5f60718', elements=5))
    text = tool_desktop.wait(text='已保存')
    assert 'pid 23844' in text
    assert '60D5E' in text
    assert 'expected_pid 填 23844' in text
    text.encode('gbk')


def test_inspect_text_carries_the_identity_for_element_actions(monkeypatch):
    """inspect 的指引必须可执行：pid 和窗口号要在输出里。"""
    tree = desktop.UiTree(
        items=(desktop._element_info(_element(id=3, name='提交'), 3),),
        truncated=False, fingerprint='a1b2c3d4e5f60718',
        state=desktop._state(_state(pid=23844, window_id='60D5E')), duration_ms=9)
    monkeypatch.setattr(desktop, 'inspect', lambda **kw: tree)
    text = tool_desktop.inspect()
    assert 'pid 23844' in text and '60D5E' in text
    assert 'expected_pid 填 23844' in text
    assert 'expected_window_id 填 60D5E' in text


def test_wait_reports_authoritative_walk():
    """命中后要用「和 inspect 相同」的预算再走一遍，指纹才有同样的含义。

    实测过：wait 用 80 节点/900ms 轮询、inspect 用 120/2500，同一窗口
    （83 个控件）指纹分别是 7e9c15ec… 和 4b16b723…，模型照 wait 的指纹操作
    必然撞 tree_changed，然后陷入 wait→act→tree_changed 的死循环。
    """
    script = desktop.worker_script()
    assert 'fullBudget' in script
    assert 'authoritative' in script


def test_wait_parses_authoritative_flag(monkeypatch):
    monkeypatch.setattr(desktop, '_run', lambda payload, **kw: {
        'ok': True, 'result': _result(matched=True, element=None,
                                      fingerprint='', authoritative=False)})
    result = desktop.wait('control', '已保存', 'present', 3000)
    assert result.authoritative is False
    text = tool_desktop.wait(text='已保存')
    assert '编号不可用' in text


def test_powershell_never_casts_a_bool_to_string_in_a_condition():
    """`if ([string]$x.Contains(...))` 永远为真。

    实测踩过：`[string]$false` 是 'False'，而非空字符串在 PowerShell 里是真值，
    于是「等一个不存在的窗口标题」会立刻返回 matched=true。powershell 脚本没法
    被 pytest 直接单测，所以用这条守卫挡住同一类写法。
    """
    script = desktop.worker_script()
    offenders = [line.strip() for line in script.splitlines()
                 if '[string]' in line and '.Contains(' in line
                 and not line.lstrip().startswith('#')]
    assert not offenders, offenders


def test_wait_audit_and_labels():
    import audit

    assert 'desktop_wait' in audit.AUDIT_EVENTS
    assert audit.EVENT_LABELS['desktop_wait'] == '等界面变化'
