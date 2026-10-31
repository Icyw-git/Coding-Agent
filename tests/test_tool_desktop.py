"""Desktop tool formatting and audit-redaction tests."""
from types import SimpleNamespace

import desktop_control_windows
import tool_desktop


def test_snapshot_audit_does_not_store_window_title(monkeypatch):
    state = desktop_control_windows.WindowState(
        available=True, title='Private document title', process='notepad',
        pid=42, window_id='ABCD', rect=(0, 0, 640, 480),
        focused_name='Document',
    )
    monkeypatch.setattr(desktop_control_windows, 'snapshot', lambda **kwargs: state)
    events = []
    output = tool_desktop.snapshot(
        audit=lambda event, **payload: events.append((event, payload)))
    assert 'Private document title' not in str(events)
    assert 'Private document title' in output
    assert events[0][1]['title_sha1']


def test_sequence_output_names_every_step_kind(monkeypatch):
    """每个步骤都要说清自己在干什么。

    `_ACTION_VERBS` 漏了 `wait`，于是「3. 操作 完成」——等于什么都没说，
    而 wait 恰恰是模型最容易怀疑"到底等了没有"的那一步。
    """
    steps = ({'action': 'move', 'ok': True}, {'action': 'wait', 'ok': True},
             {'action': 'click', 'ok': True}, {'action': 'type', 'ok': True})
    monkeypatch.setattr(desktop_control_windows, 'action', lambda *a, **kw:
                        SimpleNamespace(
                            action='sequence', duration_ms=9, steps=steps,
                            before=desktop_control_windows.WindowState(
                                available=True, title='Notepad', process='notepad',
                                pid=42, window_id='ABCD'),
                            state=desktop_control_windows.WindowState(
                                available=True, title='Notepad', process='notepad',
                                pid=42, window_id='ABCD')))
    text = tool_desktop.action('sequence', expected_pid=42,
                               expected_window_id='ABCD')
    for verb in ('移动鼠标', '等待', '点击', '输入文字'):
        assert verb in text, verb
    assert '操作 完成' not in text
    text.encode('gbk')


def test_window_list_says_when_it_is_truncated(monkeypatch):
    """limit 截断要说清楚。

    「可见窗口 1 个（共 18 个）」会被读成"只看到 1 个窗口、另 17 个是别的窗口"，
    实际是 limit 截断——模型据此会以为桌面很空。
    """
    windows = [{'pid': 1, 'window_id': 'A1', 'title': '记事本',
                'process': 'notepad', 'foreground': True, 'minimized': False}]
    monkeypatch.setattr(desktop_control_windows, 'list_windows',
                        lambda **kw: {'total': 18, 'windows': windows})
    text = tool_desktop.list_windows(limit=1)
    assert '可见窗口 18 个' in text
    assert '前 1 个' in text
    text.encode('gbk')

    monkeypatch.setattr(desktop_control_windows, 'list_windows',
                        lambda **kw: {'total': 1, 'windows': windows})
    text = tool_desktop.list_windows(limit=20)
    assert '可见窗口 1 个：' in text
    assert '前 1 个' not in text


def test_snapshot_rect_is_grouped_not_a_bare_tuple(monkeypatch):
    """窗口矩形也用「左,上 宽x高」。

    四个裸数字（`90,90,690,630`）看不出哪两个是宽高；inspect 的控件坐标改成
    分组写法时漏了这一处，同一次对话里出现两种格式只会让模型更迷惑。
    """
    monkeypatch.setattr(desktop_control_windows, 'snapshot', lambda **kw:
                        desktop_control_windows.WindowState(
                            available=True, title='Notepad', process='notepad',
                            pid=42, window_id='ABCD', rect=(90, 90, 690, 630)))
    text = tool_desktop.snapshot()
    assert '位置 90,90 690x630' in text
    assert '位置 90,90,690,630' not in text
    text.encode('gbk')


def test_action_audit_omits_typed_text(monkeypatch):
    monkeypatch.setattr(desktop_control_windows, 'action', lambda *args, **kwargs:
                       SimpleNamespace(
                           action='type', duration_ms=5,
                           state=desktop_control_windows.WindowState(
                               available=True, title='Notepad', process='notepad',
                               pid=42, window_id='ABCD')))
    events = []
    tool_desktop.action(
        'type', text='private text', expected_pid=42,
        expected_window_id='ABCD',
        audit=lambda event, **payload: events.append((event, payload)))
    assert 'private text' not in repr(events)
    assert events[0][0] == 'desktop_action'
    assert events[0][1]['action'] == 'type'


def test_action_audit_omits_typed_characters(monkeypatch):
    """单字符键等于键入内容，和 type 的文本一样不能进台账。

    `key` 允许 `a` / `1`。逐字敲一条密码，台账里就等于存了密码；命名键
    （enter / tab / ctrl+v）不涉及内容，可以记。
    """
    monkeypatch.setattr(desktop_control_windows, 'action', lambda *args, **kwargs:
                       SimpleNamespace(
                           action='key', duration_ms=5,
                           state=desktop_control_windows.WindowState(
                               available=True, title='Notepad', process='notepad',
                               pid=42, window_id='ABCD')))
    for key, recorded in (('a', False), ('1', False), ('enter', True),
                          ('ctrl+v', True)):
        events = []

        def collect(event, _events=events, **payload):
            _events.append(payload)

        tool_desktop.action('key', key=key, expected_pid=42,
                            expected_window_id='ABCD', audit=collect)
        assert ('key' in events[0]) is recorded, key
    # 判据只有一处：校验放行的单字符和审计跳过的单字符必须是同一批
    for single in ('a', '1', 'z'):
        assert desktop_control_windows.is_single_character(single)
    for named in ('enter', 'tab', 'ctrl+v', 'F5', 'space'):
        assert not desktop_control_windows.is_single_character(named)


def test_sequence_output_lists_steps_and_audit_counts_them(monkeypatch):
    monkeypatch.setattr(desktop_control_windows, 'action', lambda *args, **kwargs:
                       SimpleNamespace(
                           action='sequence', duration_ms=9,
                           steps=({'action': 'click', 'ok': True},
                                  {'action': 'type', 'ok': True}),
                           state=desktop_control_windows.WindowState(
                               available=True, title='Notepad', process='notepad',
                               pid=42, window_id='ABCD')))
    events = []
    output = tool_desktop.action(
        'sequence', steps=[{'action': 'click', 'x': 1, 'y': 2},
                           {'action': 'type', 'text': 'private text'}],
        expected_pid=42, expected_window_id='ABCD',
        audit=lambda event, **payload: events.append((event, payload)))
    assert '1. 点击 完成' in output
    assert '2. 输入文字 完成' in output
    assert 'private text' not in repr(events)
    assert events[0][0] == 'desktop_action'
    assert events[0][1]['steps'] == 2
    assert events[0][1]['action'] == 'sequence'


def test_snapshot_unavailable_reports_error(monkeypatch):
    monkeypatch.setattr(desktop_control_windows, 'snapshot', lambda **kwargs:
                       desktop_control_windows.WindowState(
                           available=False, error='no_foreground_window'))
    events = []
    output = tool_desktop.snapshot(
        audit=lambda event, **payload: events.append((event, payload)))
    assert '当前没有可用的前台窗口（no_foreground_window）' in output
    assert events[0][0] == 'desktop_snapshot'


def test_click_options_reach_the_capability_layer(monkeypatch):
    seen = {}

    def fake(action, **kwargs):
        seen.update(action=action, **kwargs)
        return SimpleNamespace(
            action=action, duration_ms=3,
            state=desktop_control_windows.WindowState(
                available=True, title='Notepad', process='notepad',
                pid=42, window_id='ABCD'))

    monkeypatch.setattr(desktop_control_windows, 'action', fake)
    tool_desktop.action('click', x=5, y=6, button='right', count=2,
                        expected_pid=42, expected_window_id='ABCD')
    assert seen['button'] == 'right'
    assert seen['count'] == 2
