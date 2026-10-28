"""桌面感知路径的 profile：时间、内存、payload 与 token 成本。

用法::

    python scripts/profile_desktop.py               # 全部
    python scripts/profile_desktop.py walk          # 逐窗口遍历耗时（逐个进程，不需要抢前台）
    python scripts/profile_desktop.py api           # 走真实 worker 的端到端延迟（需要前台窗口）
    python scripts/profile_desktop.py memory        # Python 侧解析/渲染的内存与泄漏
    python scripts/profile_desktop.py tokens        # 回包大小与 token 成本
    python scripts/profile_desktop.py --json        # 机器可读

回答四个问题
------------
1. **遍历多快**：一次 `desktop_inspect` 在不同应用上各要多久（中位数 / 最坏）。
2. **钱花在哪**：UIA 枚举 vs 属性读取 vs JSON/Base64 回包，各占多少。
3. **内存**：Python 侧一次 `inspect` 的峰值分配、常驻增长（300 次循环），
   以及每控件的常驻成本。
4. **token**：渲染给模型的文本有多少 token，相对「全量 JSON」和「截图」省多少。

为什么单独一个脚本而不是塞进 pytest
------------------------------------
pytest 跑的是"行为对不对"，不是"快不快、占多少"。把性能断言写进单测会让测试在
慢机器上误报，而完全不做又会让回归悄悄发生。脚本给数字，人看趋势。

两类测量的区别
--------------
``walk`` 用的是脚本自带的探测片段（直接对指定窗口跑 ``Get-UiTree``），
好处是**不需要抢前台**、可以逐个进程对比；``api`` 走真实链路
（``worker`` + ``desktop_control_windows.inspect()``），代价是只测得到前台窗口。
两者数字不该互相替代。
"""
from __future__ import annotations

import base64
import gc
import json
import os
import statistics
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import desktop_control_windows as desktop  # noqa: I001,E402
import harness  # noqa: F401,E402  (导入期 load_dotenv + 配置)
import tool_desktop  # noqa: E402

#: 遍历片段：与被测实现共用 ``_CONTROL_SETUP``（同一套裁剪/预算/过滤逻辑），
#: 只是把「前台窗口」换成「指定 hwnd」，好让脚本逐个进程量。
WALK_PROBE = r'''
$wins = @(Get-VisibleWindows)
foreach ($pick in $wins) {
    if ($pick.minimized) { continue }
    $hwnd = [IntPtr][Convert]::ToInt64([string]$pick.window_id, 16)
    $samples = @()
    $items = 0
    $jsonKb = 0
    for ($i = 0; $i -lt $env:PROFILE_REPEAT; $i++) {
        $t = Get-UiTree $hwnd 120 12 2500
        $samples += $t.duration_ms
        $items = $t.items.Count
        if ($i -eq 0) {
            $payload = [ordered]@{ ok = $true; items = @($t.items);
                                   fingerprint = $t.fingerprint }
            $json = $payload | ConvertTo-Json -Depth 12 -Compress
            $jsonKb = [int]([Text.Encoding]::UTF8.GetByteCount($json) / 1024)
        }
    }
    [ordered]@{
        process = $pick.process
        title = $pick.title
        items = $items
        samples = $samples
        json_kb = $jsonKb
    } | ConvertTo-Json -Compress -Depth 5
}
'''


def _run_powershell(script: str, env: dict | None = None,
                    timeout: float = 300.0) -> subprocess.CompletedProcess:
    import ps_worker

    payload = base64.b64encode(script.encode('utf-8')).decode('ascii') + chr(10)
    exe = desktop._powershell_exe() or 'powershell'
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    return subprocess.run(
        [exe, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-WindowStyle', 'Hidden', '-Command', ps_worker.BOOTSTRAP],
        input=payload, capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=timeout,
        env={**os.environ, **(env or {})}, creationflags=flags)


# ---------- 1. 逐窗口遍历 ----------

def profile_walk(repeat: int = 3) -> list[dict]:
    import ps_worker

    script = (ps_worker.PRELUDE + desktop._CONTROL_SETUP + chr(10) + WALK_PROBE)
    result = _run_powershell(script, {'PROFILE_REPEAT': str(repeat)})
    rows = []
    for line in (result.stdout or '').splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        samples = [int(item) for item in raw.get('samples') or []]
        if not samples:
            continue
        rows.append({
            'process': raw.get('process') or '?',
            'title': (raw.get('title') or '')[:24],
            'items': int(raw.get('items') or 0),
            'median_ms': round(statistics.median(samples), 1),
            'max_ms': max(samples),
            'json_kb': int(raw.get('json_kb') or 0),
        })
    rows.sort(key=lambda row: row['median_ms'], reverse=True)
    return rows


# ---------- 2. 真实链路 ----------

def profile_api(repeat: int = 5) -> dict:
    out: dict = {}
    # 显示器信息不依赖前台窗口，先量它：这样没有前台时这条也能报出来。
    display_samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        desktop.displays()
        display_samples.append((time.perf_counter() - started) * 1000)
    out['displays_ms'] = round(statistics.median(display_samples), 1)

    try:
        tree = _profile_inspect(out, repeat)
    except desktop.DesktopControlError as exc:
        out['error'] = str(exc)[:120]
        out['hint'] = '需要前台窗口；先点一下目标窗口再跑'
        return out
    if tree.fingerprint:
        started = time.perf_counter()
        try:
            desktop.wait('control', '绝不可能出现的控件名', 'present', 500)
        except desktop.DesktopControlError as exc:
            out['wait_error'] = str(exc)[:80]
        out['wait_timeout_500ms_took_ms'] = round((time.perf_counter() - started) * 1000, 1)
    return out


def _profile_inspect(out: dict, repeat: int):
    started = time.perf_counter()
    tree = desktop.inspect()
    out['inspect_cold_ms'] = round((time.perf_counter() - started) * 1000, 1)
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        tree = desktop.inspect()
        samples.append((time.perf_counter() - started) * 1000)
    out['inspect_warm_ms'] = round(statistics.median(samples), 1)
    out['inspect_worst_ms'] = round(max(samples), 1)
    out['items'] = len(tree.items)

    format_samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        text = tool_desktop.inspect()
        format_samples.append((time.perf_counter() - started) * 1000)
    out['inspect_and_format_ms'] = round(statistics.median(format_samples), 1)
    out['render_chars'] = len(text)
    return tree


# ---------- 3. Python 侧内存 ----------

def _synthetic_items(count: int = 120) -> list[dict]:
    items = []
    for index in range(1, count + 1):
        items.append({
            'id': index,
            'label': '按钮' if index % 3 else '输入框',
            'name': f'控件名称 {index} 占位文本用于模拟真实长度',
            'value': 'wifi' if index % 3 == 0 else '',
            'password': False, 'enabled': True, 'focusable': True,
            'focused': index == 1, 'bounds': [10 + index, 20, 88, 30],
            'operations': ['invoke'] if index % 3 else ['setvalue'],
        })
    return items


def profile_memory(cycles: int = 300) -> dict:
    items = _synthetic_items()
    payload = {'ok': True, 'state': {'available': True, 'title': '内存测试',
                                     'process': 'synthetic', 'pid': 1,
                                     'window_id': 'ABCD'}, 'items': items,
               'total': len(items), 'truncated': False,
               'fingerprint': 'a' * 40, 'duration_ms': 100}
    blob = json.dumps(payload)

    gc.collect()
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    parsed = [desktop._element_info(item, index)
              for index, item in enumerate(json.loads(blob)['items'], 1)]
    peak = tracemalloc.get_traced_memory()[1]
    rendered = tool_desktop._element_line(parsed[0])
    del parsed, rendered
    gc.collect()
    after = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()

    # 常驻增长：反复走「解析 → 渲染」，看有没有东西被留住
    gc.collect()
    tracemalloc.start()
    start = tracemalloc.get_traced_memory()[0]
    for _ in range(cycles):
        rows = [desktop._element_info(item, index)
                for index, item in enumerate(json.loads(blob)['items'], 1)]
        text = chr(10).join(tool_desktop._element_line(row) for row in rows)
        del rows, text
    gc.collect()
    grown = tracemalloc.get_traced_memory()[0] - start
    current, loop_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        'items': len(items),
        'payload_kb': round(len(blob.encode('utf-8')) / 1024, 1),
        'peak_kb': round((peak - base) / 1024, 1),
        'retained_kb': round((after - base) / 1024, 1),
        'per_item_bytes': int((peak - base) / max(1, len(items))),
        'loop_peak_kb': round(loop_peak / 1024, 1),
        'growth_after_cycles_kb': round(grown / 1024, 2),
        'cycles': cycles,
    }


# ---------- 4. token 成本 ----------

def profile_tokens() -> dict:
    items = _synthetic_items()
    tree = desktop.UiTree(
        items=tuple(desktop._element_info(item, index)
                    for index, item in enumerate(items, 1)),
        truncated=False, fingerprint='a' * 40,
        state=desktop._state({'available': True, 'title': '记事本',
                              'process': 'notepad', 'pid': 1, 'window_id': 'ABCD'}),
        duration_ms=120)
    text = _render(tree)
    raw = json.dumps({'items': items}, ensure_ascii=False)
    line_chars = [len(tool_desktop._element_line(item)) for item in tree.items]
    return {
        'items': len(tree.items),
        'rendered_chars': len(text),
        'rendered_tokens_est': int(len(text) / 2.0),
        'per_item_chars': int(statistics.mean(line_chars)) if line_chars else 0,
        'raw_json_kb': round(len(raw.encode('utf-8')) / 1024, 1),
        'raw_json_tokens_est': int(len(raw) / 3.0),
        'screenshot_tokens_est': '1000-1800（社区实测）',
    }


def _render(tree) -> str:
    import desktop_control_windows as module

    original = module.inspect
    module.inspect = lambda **kwargs: tree
    try:
        return tool_desktop.inspect()
    finally:
        module.inspect = original


def main(argv: list[str]) -> int:
    as_json = '--json' in argv
    positional = [arg for arg in argv[1:] if not arg.startswith('-')]
    modes = {'all', 'walk', 'api', 'memory', 'tokens'}
    want = next((arg for arg in positional if arg in modes), 'all')
    repeat = 5
    for arg in positional:
        if arg.isdigit():
            repeat = max(1, int(arg))

    if not desktop._powershell_exe():
        print('这里没有 PowerShell，桌面相关测量跳过。')
        return 1

    report: dict = {}
    if want in ('all', 'walk'):
        report['walk'] = profile_walk(repeat=3)
    if want in ('all', 'api'):
        try:
            report['api'] = profile_api(repeat=repeat)
        except desktop.DesktopControlError as exc:
            report['api'] = {'error': str(exc)[:120],
                             'hint': '需要前台窗口；先点一下目标窗口再跑'}
    if want in ('all', 'memory'):
        report['memory'] = profile_memory()
    if want in ('all', 'tokens'):
        report['tokens'] = profile_tokens()

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if 'walk' in report:
        print('逐窗口遍历（中位数，越低越好）')
        print('  注：Chromium 类窗口第一次被无障碍接口访问时要自己建树，'
              '冷起的数字会明显偏大；同一进程内重复跑才是稳态。')
        print(f"  {'进程 / 标题':<36} {'控件':>4} {'中位':>7} {'最坏':>7} {'回包':>6}")
        for row in report['walk']:
            label = f"{row['process']} / {row['title']}"[:36]
            print(f"  {label:<36} {row['items']:>4} {row['median_ms']:>6.1f}ms "
                  f"{row['max_ms']:>6}ms {row['json_kb']:>5}KB")
    if 'api' in report:
        api = report['api']
        print('\n真实链路（worker + inspect）')
        if 'displays_ms' in api:
            print(f"  显示器信息 {api['displays_ms']}ms")
        if 'error' in api:
            print(f"  前台相关跳过：{api['error']}（{api['hint']}）")
        else:
            print(f"  冷启动 {api['inspect_cold_ms']}ms；热 {api['inspect_warm_ms']}ms"
                  f"（最坏 {api['inspect_worst_ms']}ms），{api['items']} 个控件")
            print(f"  含渲染 {api['inspect_and_format_ms']}ms，文本 {api['render_chars']} 字符")
            if 'displays_ms' in api:
                print(f"  显示器信息 {api['displays_ms']}ms")
            if 'wait_timeout_500ms_took_ms' in api:
                print(f"  wait 超时 500ms 实际 {api['wait_timeout_500ms_took_ms']}ms")
    if 'memory' in report:
        mem = report['memory']
        print('\nPython 侧内存')
        print(f"  {mem['items']} 个控件 · payload {mem['payload_kb']}KB")
        print(f"  单次解析峰值 {mem['peak_kb']}KB（每控件 {mem['per_item_bytes']} 字节），"
              f"释放后常驻 {mem['retained_kb']}KB")
        print(f"  {mem['cycles']} 次循环后增长 {mem['growth_after_cycles_kb']}KB"
              f"（峰值 {mem['loop_peak_kb']}KB）")
    if 'tokens' in report:
        tok = report['tokens']
        print('\ntoken 成本')
        print(f"  渲染后 {tok['rendered_chars']} 字符 ≈ {tok['rendered_tokens_est']} tokens"
              f"（每控件 {tok['per_item_chars']} 字符）")
        print(f"  全量 JSON {tok['raw_json_kb']}KB ≈ {tok['raw_json_tokens_est']} tokens；"
              f"截图 {tok['screenshot_tokens_est']} tokens")
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
