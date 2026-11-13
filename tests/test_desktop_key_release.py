"""键序列：先校验、再按下；任何异常都要逆序释放已按下的键（R0-a 第③项）。

原实现有两处会让修饰键粘在按下状态：

```powershell
foreach ($mod in $mods) {
    if (-not $modMap.ContainsKey($mod)) { return ... }   # ← 校验发生在按下之后
    [HarnessDesktopNative]::VirtualKey($mod, $false)     # ← 已按下前一个修饰键，直接返回
}
[HarnessDesktopNative]::VirtualKey($vk,$false)           # ← 按下主键
[HarnessDesktopNative]::VirtualKey($vk,$true)            # ← 中间任何异常，没有 finally
```

`key=ctrl+bogus` 会把 Ctrl 留在按下状态，之后所有输入都变形。

**这条测试不碰真实键盘**：`Invoke-KeyStep` 把发送动作做成 `$Send` scriptblock 参数，
测试传一个只记账的假 sender（还会按指令在第 N 次调用时抛异常）。
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

POWERSHELL = shutil.which('powershell') or shutil.which('pwsh')
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason='需要 PowerShell')

#: 假的 sender：记下每次 (vk, down)，可以安排在第 N 次调用时抛异常。
HARNESS = r'''
function Invoke-Probe($KeyText, $FailAt) {
    $script:events = New-Object System.Collections.ArrayList
    $script:calls = 0
    $script:Send = {
        param($vk, $down)
        $script:calls++
        [void]$script:events.Add([ordered]@{ vk = [int]$vk; down = [bool]$down })
        if ($FailAt -gt 0 -and $script:calls -eq $FailAt) {
            throw 'injected: key event failed'
        }
    }
    $result = Invoke-KeyStep $KeyText $script:Send
    $payload = [ordered]@{
        result = $(if ($null -eq $result) { $null } else { $result })
        events = @($script:events)
    }
    $payload | ConvertTo-Json -Depth 6 -Compress |
        Out-File -FilePath $env:PROBE_OUT -Encoding utf8
}
'''


def _run(key_text: str, fail_at: int = 0) -> dict:
    """在临时目录里跑一次键序列，返回 {'result': …, 'events': [...]}。

    用 `-EncodedCommand`（base64 UTF-16LE）而不是 `-File`：PowerShell 5 读无 BOM 的
    文件按 ANSI 解释，脚本里的中文字面量会被拆成乱码而且引号配对会坏；写 BOM 或换
    编码又得区分 PS5/PS7。`-EncodedCommand` 是唯一一个两种版本上行为一致的入口。
    """
    import base64

    from desktop_scripts import KEY_STEP_SCRIPT

    body = (KEY_STEP_SCRIPT + '\n'
            + f'Invoke-Probe -KeyText {json.dumps(key_text)} -FailAt {int(fail_at)}\n')
    tmp = ROOT / '.test-tmp' / 'keyprobe'
    tmp.mkdir(parents=True, exist_ok=True)
    out_path = tmp / 'probe.json'
    out_path.unlink(missing_ok=True)
    encoded = base64.b64encode((HARNESS + '\n' + body).encode('utf-16-le')).decode('ascii')

    completed = subprocess.run(
        [POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-EncodedCommand', encoded],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        env={**os.environ, 'PROBE_OUT': str(out_path)}, timeout=120)
    assert completed.returncode == 0, completed.stderr[-1500:]
    assert out_path.is_file(), completed.stderr[-1500:]
    data = json.loads(out_path.read_text(encoding='utf-8-sig'))
    return {'result': data.get('result'), 'events': data.get('events') or []}


def _downs(events):
    return [e['vk'] for e in events if e['down']]


def _ups(events):
    return [e['vk'] for e in events if not e['down']]


def test_normal_key_is_pressed_and_released():
    out = _run('ctrl+v')
    assert out['result'] is None, out['result']
    assert _downs(out['events']) == [17, 86]      # ctrl 按下 → v 按下
    assert _ups(out['events']) == [86, 17]        # v 放开 → ctrl 放开


def test_unknown_modifier_presses_nothing():
    """`ctrl+bogus`：在校验阶段就被拒，一个键都不该按（原来会先把 Ctrl 按下去）。"""
    out = _run('ctrl+bogus')
    assert out['result']['code'] == 'invalid_key', out['result']
    assert out['events'] == [], f'已经开始按键了：{out["events"]}'


def test_unknown_base_key_presses_nothing():
    out = _run('shift+nope')
    assert out['result']['code'] == 'invalid_key', out['result']
    assert out['events'] == [], out['events']


@pytest.mark.parametrize('fail_at', [1, 2, 3])
def test_interrupted_sequence_releases_every_pressed_key(fail_at):
    """在按下第 1/2/3 个键之后注入异常：所有已按下的键都必须被放开。"""
    out = _run('ctrl+shift+s', fail_at=fail_at)
    assert out['result']['code'] == 'key_interrupted', out['result']

    downs = _downs(out['events'])
    ups = _ups(out['events'])
    # 失败前按下的每一个键，都要在释放阶段出现
    for vk in set(downs):
        assert vk in ups, f'键 {vk} 按下后没有释放（按下 {downs}，释放 {ups}）'
    assert out['result']['release_failed'] == '', out['result']


def test_double_click_style_repeat_is_not_used_for_keys():
    """简单的 `enter` 也能正常走完（防止重构把无修饰键的路径写坏）。"""
    out = _run('enter')
    assert out['result'] is None
    assert _downs(out['events']) == [13] and _ups(out['events']) == [13]
