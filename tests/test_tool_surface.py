"""工具面按能力开关裁剪：关掉的能力不占 token。

为什么要有这条
--------------
`desktop_*` 6 个工具的 schema 合计 1,631 tokens，而策略层在
`HARNESS_DESKTOP_CONTROL` 未设时对它们一律 **DENY**——模型拿到这些 schema 也永远
用不了。每次请求都发 1,631 tokens 的"必然被拒的说明"，是纯浪费。

判据只有一条：**这个工具在当前进程里有没有可能被放行？** 没有就不发。
（开关只认进程启动环境，所以按启动时的值裁剪与会话行为一致。）

刻意不做的事：不按"用户偏好"或"场景猜测"裁剪——那会让同一句话在不同会话里
得到不同的工具，模型无从学起。只按**能力开关**，它有确定语义。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROBE = '''
import json, sys
sys.path.insert(0, {src!r})
sys.path.insert(0, {root!r})
import harness
names = [t["function"]["name"] for t in harness.TOOLS]
chars = len(json.dumps([t["function"] for t in harness.TOOLS], ensure_ascii=False))
print(json.dumps({{"names": names, "chars": chars}}))
'''


def _surface_with(env_overrides: dict) -> dict:
    """在一个干净的子进程里按给定环境变量取工具面（开关只认启动环境）。"""
    import os
    env = {key: value for key, value in os.environ.items()
           if key not in ('HARNESS_DESKTOP_CONTROL', 'HARNESS_SCREEN_TOOLS',
                          'HARNESS_CLIPBOARD_TOOLS')}
    env.update(env_overrides)
    code = PROBE.format(src=str(ROOT / 'src'), root=str(ROOT))
    result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                            text=True, timeout=180, env=env)
    assert result.returncode == 0, result.stderr[-800:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_disabled_capabilities_do_not_ship_their_schemas():
    """桌面控制默认关：6 个 desktop 工具的 schema 不该出现在请求里。"""
    surface = _surface_with({'HARNESS_SCREEN_TOOLS': '1',
                             'HARNESS_CLIPBOARD_TOOLS': '1'})
    assert not [n for n in surface['names'] if n.startswith('desktop_')]
    # 其余工具照旧（别误伤）
    for name in ('read', 'bash', 'write', 'grep', 'remember', 'schedule_cron'):
        assert name in surface['names'], name


def test_enabled_capabilities_ship_their_schemas():
    """开了就必须发：模型看不到就等于这个功能不存在。"""
    surface = _surface_with({'HARNESS_DESKTOP_CONTROL': '1'})
    desktop = sorted(n for n in surface['names'] if n.startswith('desktop_'))
    assert desktop == ['desktop_action', 'desktop_displays', 'desktop_inspect',
                       'desktop_snapshot', 'desktop_wait', 'desktop_windows']


def test_screen_and_clipboard_follow_their_own_switches():
    off = _surface_with({'HARNESS_SCREEN_TOOLS': '0',
                         'HARNESS_CLIPBOARD_TOOLS': '0'})
    assert not [n for n in off['names'] if n.startswith(('screen_', 'clipboard_'))]

    on = _surface_with({'HARNESS_SCREEN_TOOLS': '1',
                        'HARNESS_CLIPBOARD_TOOLS': '1'})
    assert {'screen_capture', 'screen_ocr'} <= set(on['names'])
    assert {'clipboard_read', 'clipboard_write'} <= set(on['names'])


def test_trimming_actually_saves_tokens():
    """关掉桌面控制至少省 1,500 字符（≈375 tokens）——这是这条优化的全部意义。"""
    without = _surface_with({})
    with_desktop = _surface_with({'HARNESS_DESKTOP_CONTROL': '1'})
    saved = with_desktop['chars'] - without['chars']
    assert saved >= 6_000, f'只省了 {saved} 字符，裁剪没生效'
