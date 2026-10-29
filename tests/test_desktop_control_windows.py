"""Desktop input backend validation and PowerShell contract tests; never touch a real desktop."""
import base64
import json
import subprocess

import pytest

import desktop_control_windows as desktop


def _record(state=None, action='snapshot', steps=None):
    state = state or {
        'available': True, 'title': 'Notepad', 'process': 'notepad', 'pid': 123,
        'window_id': 'ABCD', 'class_name': 'Notepad', 'rect': [0, 0, 800, 600],
        'focused_name': 'Text area', 'focused_control_type': 'ControlType.Document',
        'focused_is_password': False, 'sensitive_target': False,
    }
    if action == 'snapshot':
        data = {'ok': True, 'state': state}
    elif action == 'sequence':
        data = {'ok': True, 'action': 'sequence',
                'steps': steps if steps is not None else [], 'state_after': state}
    else:
        data = {'ok': True, 'state_after': state, 'action': action}
    blob = base64.b64encode(json.dumps(data).encode('utf-8')).decode('ascii')
    return subprocess.CompletedProcess([], 0, blob, '')


def _runner(record=None, seen=None):
    record = record or _record()
    def run(script, env, timeout):
        if seen is not None:
            seen.update(script=script, env=env, timeout=timeout)
        return record
    return run


def test_snapshot_returns_foreground_window_state():
    state = desktop.snapshot(runner=_runner())
    assert state.title == 'Notepad'
    assert state.pid == 123
    assert state.window_id == 'ABCD'
    assert state.rect == (0, 0, 800, 600)


@pytest.mark.parametrize('args', [
    {'action': 'click', 'expected_pid': 123, 'expected_window_id': 'ABCD', 'x': 2, 'y': 3},
    {'action': 'move', 'expected_pid': 123, 'expected_window_id': 'ABCD', 'x': 2, 'y': 3},
    {'action': 'type', 'expected_pid': 123, 'expected_window_id': 'ABCD', 'text': '中文'},
    {'action': 'key', 'expected_pid': 123, 'expected_window_id': 'ABCD', 'key': 'enter'},
    {'action': 'scroll', 'expected_pid': 123, 'expected_window_id': 'ABCD', 'amount': -2},
])
def test_action_encodes_payload_and_returns_state(args):
    seen = {}
    result = desktop.action(**args, runner=_runner(
        record=_record(action=args['action']), seen=seen))
    payload = json.loads(__import__('base64').b64decode(
        seen['env']['HARNESS_DESKTOP_PAYLOAD']))
    assert payload['action'] == args['action']
    assert payload['expected_pid'] == 123
    assert payload['expected_window_id'] == 'ABCD'
    assert result.state.pid == 123


def test_action_requires_window_snapshot_binding():
    with pytest.raises(desktop.DesktopControlError, match='expected_pid'):
        desktop.validate_action('click', x=1, y=1)
    with pytest.raises(desktop.DesktopControlError, match='expected_window_id'):
        desktop.validate_action('click', expected_pid=1, x=1, y=1)


@pytest.mark.parametrize('kwargs', [
    {'action': 'click', 'expected_pid': 1, 'expected_window_id': 'A', 'x': 'x', 'y': 1},
    {'action': 'type', 'expected_pid': 1, 'expected_window_id': 'A', 'text': 'x' * 1001},
    {'action': 'key', 'expected_pid': 1, 'expected_window_id': 'A', 'key': 'not a key'},
    {'action': 'scroll', 'expected_pid': 1, 'expected_window_id': 'A', 'amount': 21},
    {'action': 'invoke', 'expected_pid': 1, 'expected_window_id': 'A'},
    {'action': 'move', 'expected_pid': 1, 'expected_window_id': 'ZZ', 'x': 0, 'y': 0},
])
def test_invalid_actions_rejected_before_runner(kwargs):
    called = []
    with pytest.raises(desktop.DesktopControlError):
        desktop.action(**kwargs, runner=lambda *args: called.append(args))
    assert called == []


SEQUENCE_STEPS = [
    {'action': 'click', 'x': 10, 'y': 20},
    {'action': 'type', 'text': '你好'},
    {'action': 'key', 'key': 'enter'},
]


def test_sequence_encodes_steps_and_reports_outcomes():
    seen = {}
    outcome = [{'action': 'click', 'ok': True},
               {'action': 'type', 'ok': True},
               {'action': 'key', 'ok': True}]
    result = desktop.action(
        'sequence', expected_pid=123, expected_window_id='abcd',
        steps=SEQUENCE_STEPS, runner=_runner(
            record=_record(action='sequence', steps=outcome), seen=seen))
    payload = json.loads(base64.b64decode(seen['env']['HARNESS_DESKTOP_PAYLOAD']))
    assert payload['action'] == 'sequence'
    assert payload['expected_window_id'] == 'ABCD'
    assert payload['steps'] == [
        {'action': 'click', 'x': 10, 'y': 20},
        {'action': 'type', 'text': '你好'},
        {'action': 'key', 'key': 'enter'},
    ]
    assert [step['action'] for step in result.steps] == ['click', 'type', 'key']
    assert result.state.pid == 123


def test_sequence_scales_timeout_with_step_count():
    seen = {}
    desktop.action(
        'sequence', expected_pid=1, expected_window_id='A',
        steps=[{'action': 'key', 'key': 'enter'}] * 12,
        runner=_runner(record=_record(action='sequence', steps=[]), seen=seen))
    assert seen['timeout'] > desktop.DEFAULT_TIMEOUT


@pytest.mark.parametrize('kwargs', [
    {'action': 'sequence', 'expected_pid': 1, 'expected_window_id': 'A',
     'steps': [], 'x': 1},
    {'action': 'sequence', 'expected_pid': 1, 'expected_window_id': 'A'},
    {'action': 'sequence', 'expected_pid': 1, 'expected_window_id': 'A',
     'steps': []},
    {'action': 'sequence', 'expected_pid': 1, 'expected_window_id': 'A',
     'steps': [{'action': 'click', 'x': 1, 'y': 2}] * 13},
    {'action': 'sequence', 'expected_pid': 1, 'expected_window_id': 'A',
     'steps': [{'action': 'click', 'x': 1, 'y': 2, 'text': 'no'}]},
    {'action': 'sequence', 'expected_pid': 1, 'expected_window_id': 'A',
     'steps': [{'action': 'click'}]},
    {'action': 'sequence', 'expected_pid': 1, 'expected_window_id': 'A',
     'steps': [{'action': 'invoke'}]},
    {'action': 'sequence', 'expected_pid': 1, 'expected_window_id': 'A',
     'steps': ['click']},
    {'action': 'click', 'expected_pid': 1, 'expected_window_id': 'A',
     'x': 1, 'y': 2, 'steps': [{'action': 'key', 'key': 'enter'}]},
])
def test_invalid_sequences_rejected_before_runner(kwargs):
    called = []
    with pytest.raises(desktop.DesktopControlError):
        desktop.action(**kwargs, runner=lambda *args: called.append(args))
    assert called == []


def test_power_shell_contract_checks_window_identity_and_sensitive_focus():
    assert 'GetForegroundWindow' in desktop.CONTROL_SCRIPT
    assert 'expected_window_id' in desktop.CONTROL_SCRIPT
    assert 'focused_is_password' in desktop.CONTROL_SCRIPT
    assert 'sensitive_target' in desktop.CONTROL_SCRIPT
    assert 'SendInput' in desktop.CONTROL_SCRIPT


def test_powershell_script_contract_covers_fixed_output_and_dpi():
    # $pid 撞只读自动变量曾让每次调用必失败；编码曾把 GBK 标题喂给 UTF-8 解码。
    assert '$pid =' not in desktop.CONTROL_SCRIPT
    assert '$procId' in desktop.CONTROL_SCRIPT
    assert 'ToBase64String' in desktop.CONTROL_SCRIPT
    assert 'SetProcessDPIAware' in desktop.CONTROL_SCRIPT
    assert 'Invoke-Step' in desktop.CONTROL_SCRIPT
    assert 'foreach ($step in $steps)' in desktop.CONTROL_SCRIPT
    assert 'steps_done' in desktop.CONTROL_SCRIPT


def test_powershell_timeout_is_reported():
    def timed_out(*args):
        raise subprocess.TimeoutExpired('powershell', 1)
    with pytest.raises(desktop.DesktopControlError, match='超时'):
        desktop.snapshot(runner=timed_out)


def test_key_combo_and_click_options_are_normalized():
    seen = {}
    desktop.action('sequence', expected_pid=1, expected_window_id='A',
                   steps=[{'action': 'key', 'key': ' Ctrl+Shift+S '},
                          {'action': 'click', 'x': 1, 'y': 2,
                           'button': 'RIGHT', 'count': 2}],
                   runner=_runner(record=_record(action='sequence'), seen=seen))
    payload = json.loads(base64.b64decode(
        seen['env']['HARNESS_DESKTOP_PAYLOAD']))
    assert payload['steps'] == [
        {'action': 'key', 'key': 'ctrl+shift+s'},
        {'action': 'click', 'x': 1, 'y': 2, 'button': 'right', 'count': 2},
    ]
    plain = desktop.validate_action(
        'click', expected_pid=1, expected_window_id='A', x=3, y=4)
    assert plain == {'action': 'click', 'expected_pid': 1,
                     'expected_window_id': 'A', 'x': 3, 'y': 4}


@pytest.mark.parametrize('kwargs', [
    {'action': 'key', 'expected_pid': 1, 'expected_window_id': 'A',
     'key': 'ctrl+ctrl+s'},
    {'action': 'key', 'expected_pid': 1, 'expected_window_id': 'A',
     'key': 'hyper+s'},
    {'action': 'key', 'expected_pid': 1, 'expected_window_id': 'A',
     'key': 'ctrl+'},
    {'action': 'key', 'expected_pid': 1, 'expected_window_id': 'A',
     'key': 'ctrl+alt+shift+win+ctrl+s'},
    {'action': 'key', 'expected_pid': 1, 'expected_window_id': 'A',
     'key': 'ctrl+unknown'},
    {'action': 'click', 'expected_pid': 1, 'expected_window_id': 'A',
     'x': 1, 'y': 2, 'button': 'back'},
    {'action': 'click', 'expected_pid': 1, 'expected_window_id': 'A',
     'x': 1, 'y': 2, 'count': 3},
    {'action': 'move', 'expected_pid': 1, 'expected_window_id': 'A',
     'x': 1, 'y': 2, 'button': 'left'},
    {'action': 'type', 'expected_pid': 1, 'expected_window_id': 'A',
     'text': 'x', 'count': 2},
    {'action': 'sequence', 'expected_pid': 1, 'expected_window_id': 'A',
     'steps': [{'action': 'wait'}]},
    {'action': 'sequence', 'expected_pid': 1, 'expected_window_id': 'A',
     'steps': [{'action': 'wait', 'ms': 49}]},
    {'action': 'sequence', 'expected_pid': 1, 'expected_window_id': 'A',
     'steps': [{'action': 'wait', 'ms': 3001}]},
    {'action': 'sequence', 'expected_pid': 1, 'expected_window_id': 'A',
     'steps': [{'action': 'wait', 'ms': 100, 'x': 1}]},
    {'action': 'sequence', 'expected_pid': 1, 'expected_window_id': 'A',
     'steps': [{'action': 'wait', 'ms': 100}], 'button': 'left'},
])
def test_invalid_combos_clicks_and_waits_rejected(kwargs):
    called = []
    with pytest.raises(desktop.DesktopControlError):
        desktop.action(**kwargs, runner=lambda *args: called.append(args))
    assert called == []


def test_wait_step_is_sequence_only_and_extends_timeout():
    with pytest.raises(desktop.DesktopControlError, match='action 只能是'):
        desktop.validate_action('wait', expected_pid=1, expected_window_id='A')
    seen = {}
    desktop.action('sequence', expected_pid=1, expected_window_id='A',
                   steps=[{'action': 'wait', 'ms': 3000}],
                   runner=_runner(record=_record(action='sequence'), seen=seen))
    assert seen['timeout'] >= desktop.DEFAULT_TIMEOUT + 3.0
    payload = json.loads(base64.b64decode(
        seen['env']['HARNESS_DESKTOP_PAYLOAD']))
    assert payload['steps'] == [{'action': 'wait', 'ms': 3000}]


def test_powershell_script_contract_covers_combos_buttons_and_wait():
    assert '$modMap' in desktop.CONTROL_SCRIPT
    assert 'invalid_button' in desktop.CONTROL_SCRIPT
    assert 'invalid_count' in desktop.CONTROL_SCRIPT
    assert 'invalid_wait' in desktop.CONTROL_SCRIPT
    assert '0x0008' in desktop.CONTROL_SCRIPT
    assert '0x0020' in desktop.CONTROL_SCRIPT
    assert 'Start-Sleep -Milliseconds $ms' in desktop.CONTROL_SCRIPT


@pytest.mark.slow   # real PowerShell round-trip
def test_sendinput_struct_is_64bit_sized():
    """INPUT 必须是 40 字节（type 4 + 对齐 4 + union 32）。

    少了 `Size = 32` 会得到 32 字节，SendInput 返回 0 且 GetLastError=87
    （参数错误）——所有真实键鼠注入都会失败，但**单元测试全绿**，因为测试都
    注入 fake runner。用真实 PowerShell 断言结构大小，把这个坑锁住。
    """
    probe = r'''
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public static class SizeProbe {
    [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public U u; }
    [StructLayout(LayoutKind.Explicit, Size = 32)] public struct U { [FieldOffset(0)] public K k; }
    [StructLayout(LayoutKind.Sequential)] public struct K {
        public ushort wVk; public ushort wScan; public uint dwFlags;
        public uint time; public UIntPtr extra;
    }
    public static int Size() { return Marshal.SizeOf(typeof(INPUT)); }
}
'@
[SizeProbe]::Size()
'''
    import shutil
    import subprocess

    exe = shutil.which('powershell') or shutil.which('pwsh')
    if exe is None:
        pytest.skip('PowerShell not available')
    result = subprocess.run([exe, '-NoProfile', '-NonInteractive',
                             '-Command', probe],
                            capture_output=True, text=True, timeout=120)
    assert result.stdout.strip() == '40', result.stderr[-300:]


def test_control_script_declares_explicit_union_size():
    """脚本层面也断言：漏了 Size 就等于漏了 8 字节。"""
    assert 'StructLayout(LayoutKind.Explicit, Size = 32)' in         desktop.CONTROL_SCRIPT
    assert 'public struct MOUSEINPUT' in desktop.CONTROL_SCRIPT


def test_terminal_input_is_blocked_by_default():
    """终端窗口默认拒绝键入：敲进去的任何字符都会被当命令执行。

    这一条来自一次真实事故——验证审计时把 type 打进了正在用的终端。
    终端标题通常不含「密码」之类敏感词，敏感词匹配抓不到它，必须单列。
    """
    script = desktop.CONTROL_SCRIPT
    assert 'terminal_input' in script
    assert 'allow_terminal' in script
    assert 'is_terminal' in script


def test_allow_terminal_is_part_of_the_payload():
    payload = desktop.validate_action('type', expected_pid=1,
                                      expected_window_id='AB',
                                      text='echo hi')
    assert payload['allow_terminal'] is False
    payload = desktop.validate_action('type', expected_pid=1,
                                      expected_window_id='AB',
                                      text='echo hi', allow_terminal=True)
    assert payload['allow_terminal'] is True


def test_terminal_flag_does_not_affect_move():
    """点一下终端选中文字是正常需求：只有 type/key 受终端保护影响。"""
    script = desktop.CONTROL_SCRIPT
    assert "@('type', 'key')" in script
    payload = desktop.validate_action('move', expected_pid=1,
                                      expected_window_id='AB', x=1, y=2)
    assert payload['allow_terminal'] is False


def test_window_state_reports_terminal_flag():
    state = desktop._state({'available': True, 'process': 'WindowsTerminal',
                            'is_terminal': True})
    assert state.is_terminal is True
    assert desktop.state_dict(state)['is_terminal'] is True
