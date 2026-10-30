"""desktop_displays：显示器矩形 / DPI 的解析、文案与策略边界。

真机读数会随缩放和屏幕数量变，这里注入能力层，锁住「解析正确 + 话说清楚」。
"""

import desktop_control_windows as desktop
import tool_desktop
from exec_policy import ASK, DENY, Policy


def _state(**over):
    base = {'available': True, 'title': '记事本', 'process': 'notepad', 'pid': 1,
            'window_id': 'ABCD', 'rect': [2200, 100, 800, 600], 'dpi': 144}
    base.update(over)
    return base


def _reply(displays=None, virtual=(0, 0, 3840, 1440), index=2, dpi=144):
    if displays is None:
        displays = [
            {'name': '\\\\.\\DISPLAY1', 'primary': True,
             'bounds': [0, 0, 1920, 1080], 'work': [0, 0, 1920, 1040],
             'dpi_x': 96, 'dpi_y': 96, 'scale': 1.0},
            {'name': '\\\\.\\DISPLAY2', 'primary': False,
             'bounds': [1920, 0, 1920, 1080], 'work': [1920, 0, 1920, 1040],
             'dpi_x': 144, 'dpi_y': 144, 'scale': 1.5},
        ]
    return {'ok': True, 'action': 'displays', 'displays': displays,
            'virtual_screen': list(virtual), 'foreground_index': index,
            'foreground_dpi': dpi, 'foreground': _state()}


def _patch(monkeypatch, reply):
    monkeypatch.setattr(desktop, '_run', lambda payload, **kw: reply)


# ---------- 解析 ----------

def test_displays_parses_two_monitors(monkeypatch):
    _patch(monkeypatch, _reply())
    result = desktop.displays()
    assert len(result.displays) == 2
    first, second = result.displays
    assert first.name == '\\\\.\\DISPLAY1' and first.primary
    assert first.bounds == (0, 0, 1920, 1080)
    assert first.scale == 1.0
    assert second.scale == 1.5
    assert second.work == (1920, 0, 1920, 1040)
    assert result.virtual_screen == (0, 0, 3840, 1440)
    assert result.foreground_index == 2
    assert result.foreground_dpi == 144


def test_displays_survives_garbage_entries(monkeypatch):
    _patch(monkeypatch, _reply(displays=[
        {'name': 'X', 'bounds': 'nope'},
        {'bounds': [0, 0, 100, 100]},
        'nonsense',
    ]))
    result = desktop.displays()
    assert len(result.displays) == 1
    item = result.displays[0]
    assert item.name == 'DISPLAY1'          # 缺名字时按序号兜底
    assert item.work == (0, 0, 100, 100)    # 缺工作区时回落整块屏
    assert item.dpi_x == 96                 # 缺 DPI 时按 100% 算


def test_displays_sends_displays_action(monkeypatch):
    seen = {}

    def fake(payload, **kwargs):
        seen.update(payload)
        return _reply()

    monkeypatch.setattr(desktop, '_run', fake)
    desktop.displays()
    assert seen['action'] == 'displays'


def test_state_carries_dpi(monkeypatch):
    """DPI 跟着状态走：inspect 用它提示缩放，别的地方也能用。"""
    _patch(monkeypatch, _reply())
    assert desktop.displays().state.dpi == 144
    assert desktop.state_dict(desktop._state(_state()))['dpi'] == 144


# ---------- 文案 ----------

def test_coordinate_tuples_are_grouped_the_same_way(monkeypatch):
    """四个裸数字没人看得出顺序：屏幕、工作区、窗口矩形都用「左,上 宽x高」。

    同一行里两种写法（`0,0 1920x1080` 与 `0,0,1920,1040`）尤其糟——读者会以为
    它们含义不同。窗口矩形那个坑在 inspect 上踩过一次（按 x1,y1,x2,y2 读，
    中心算到标题栏上去了）。
    """
    _patch(monkeypatch, _reply())
    text = tool_desktop.displays()
    assert '工作区 0,0 1920x1040' in text
    assert '工作区 0,0,1920,1040' not in text
    text.encode('gbk')


def test_displays_text_lists_each_monitor(monkeypatch):
    _patch(monkeypatch, _reply())
    text = tool_desktop.displays()
    assert '显示器 2 块' in text
    assert '虚拟桌面 0,0 3840x1440' in text
    assert '#1 \\\\.\\DISPLAY1（主屏）' in text
    assert '缩放 100%（96 dpi）' in text
    assert '#2 \\\\.\\DISPLAY2' in text
    assert '缩放 150%（144 dpi）' in text
    text.encode('gbk')


def test_displays_text_explains_scaled_coordinates(monkeypatch):
    """150% 缩放下必须说清「逻辑坐标要乘 1.5」——点偏位置最常见就是这个。"""
    _patch(monkeypatch, _reply())
    text = tool_desktop.displays()
    assert '物理像素' in text
    assert '乘 1.5' in text
    text.encode('gbk')


def test_displays_text_skips_conversion_hint_at_100_percent(monkeypatch):
    _patch(monkeypatch, _reply(index=1, dpi=96))
    text = tool_desktop.displays()
    assert '乘 1.5' not in text
    assert '在 #1 上' in text


def test_displays_text_handles_unknown_foreground(monkeypatch):
    _patch(monkeypatch, _reply(index=0, dpi=0))
    text = tool_desktop.displays()
    assert '不在这些显示器上' in text


def test_displays_text_handles_empty_list(monkeypatch):
    _patch(monkeypatch, _reply(displays=[]))
    assert '没有读到任何显示器' in tool_desktop.displays()


def test_displays_failure_is_actionable(monkeypatch):
    def boom(**kwargs):
        raise desktop.DesktopControlError('desktop control unavailable')

    monkeypatch.setattr(desktop, 'displays', boom)
    text = tool_desktop.displays()
    assert '读不到显示器信息' in text
    assert 'unavailable' in text


def test_inspect_text_mentions_scaling_when_not_100_percent(monkeypatch):
    tree = desktop.UiTree(
        items=(desktop._element_info({'id': 1, 'label': '按钮', 'name': '确定',
                                      'enabled': True, 'bounds': [1, 2, 3, 4]}, 1),),
        truncated=False, fingerprint='a1b2c3d4e5f60718',
        state=desktop._state(_state(title='设置页')), duration_ms=10)
    monkeypatch.setattr(desktop, 'inspect', lambda **kw: tree)
    text = tool_desktop.inspect()
    assert '150% 缩放' in text
    assert '物理像素' in text


def test_inspect_text_stays_quiet_at_100_percent(monkeypatch):
    tree = desktop.UiTree(
        items=(desktop._element_info({'id': 1, 'label': '按钮', 'name': '确定',
                                      'enabled': True}, 1),),
        truncated=False, fingerprint='a1b2c3d4e5f60718',
        state=desktop._state(_state(dpi=96)), duration_ms=10)
    monkeypatch.setattr(desktop, 'inspect', lambda **kw: tree)
    assert '缩放' not in tool_desktop.inspect()


# ---------- 策略 ----------

def test_displays_is_off_by_default(monkeypatch):
    monkeypatch.delenv('HARNESS_DESKTOP_CONTROL', raising=False)
    assert Policy().evaluate('desktop_displays', {}).action == DENY


def test_displays_requires_approval_when_enabled(monkeypatch):
    monkeypatch.setenv('HARNESS_DESKTOP_CONTROL', '1')
    decision = Policy().evaluate('desktop_displays', {})
    assert decision.action == ASK
    assert decision.code == 'desktop_control'


def test_displays_never_auto_allowed_by_tier(monkeypatch):
    monkeypatch.setenv('HARNESS_DESKTOP_CONTROL', '1')
    decision = Policy(sandbox_mode='danger-full-access',
                      approval_policy='never').evaluate('desktop_displays', {})
    assert decision.action == DENY


def test_displays_is_forbidden_in_scheduled_turns():
    import hooks

    assert 'desktop_displays' in hooks.SCHEDULED_FORBIDDEN_TOOLS


def test_displays_audit_and_labels():
    import approval_view
    import audit

    assert 'desktop_displays' in audit.AUDIT_EVENTS
    assert audit.EVENT_LABELS['desktop_displays'] == '看显示器'
    assert approval_view._TOOL_LABELS['desktop_displays'] == '看显示器'
