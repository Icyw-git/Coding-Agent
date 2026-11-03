"""桌面工具失败时必须能被"失败判定"认出来。

`tool_activity.is_failure` 的约定是「错误以 `Error` 开头」，终端活动行据此打
`×`/`√`（`tool_activity.py:104`），`/usage` 的「失败 N」也据此计数。

但 `tool_desktop` 的失败文案是纯中文（`读不到前台窗口：…`、`等待失败：…`、
`操作没做成：…`），于是**失败的动作在活动行上打的是 `√`**，`/usage` 记成完成。
审计台账里有 5 条 `desktop_snapshot_failed`，而 `/usage` 的 desktop_snapshot
失败数是 0——两本账对不上。
"""


import desktop_control_windows as desktop
import tool_desktop
from tool_activity import is_failure


def _boom(*args, **kwargs):
    raise desktop.DesktopControlError('uia_timeout: no answer in 400ms')


def test_snapshot_failure_is_marked_failed(monkeypatch):
    monkeypatch.setattr(desktop, 'snapshot', _boom)
    text = tool_desktop.snapshot()
    assert is_failure(text), f'活动行会打成 √：{text}'


def test_wait_failure_is_marked_failed(monkeypatch):
    monkeypatch.setattr(desktop, 'wait', _boom)
    text = tool_desktop.wait(text='保存', state='absent')
    assert is_failure(text), f'活动行会打成 √：{text}'


def test_element_action_failure_is_marked_failed(monkeypatch):
    monkeypatch.setattr(desktop, 'element_action', _boom)
    text = tool_desktop.element_action(
        operation='invoke', element=1, tree=None,
        expected_pid=None, expected_window_id=None)
    assert is_failure(text), f'活动行会打成 √：{text}'


def test_plain_action_failure_is_marked_failed(monkeypatch):
    monkeypatch.setattr(desktop, 'action', _boom)
    text = tool_desktop.action('click', x=10, y=10)
    assert is_failure(text), f'活动行会打成 √：{text}'


def test_refusals_stay_recognizable_as_refusals(monkeypatch):
    """拒绝码不能被 `Error:` 前缀吃掉——别处靠码头判「这是拒绝不是失败」。"""
    def refuse(*args, **kwargs):
        raise desktop.DesktopControlError('sensitive_target: 这是密码窗口')

    monkeypatch.setattr(desktop, 'wait', refuse)
    text = tool_desktop.wait(text='保存', state='absent')
    assert text.startswith('sensitive_target:'), text
