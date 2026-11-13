"""新增能力的真实延迟 profile：走真实实现，不打桩。

用法::

    python scripts/profile_new_tools.py            # 全部
    python scripts/profile_new_tools.py --skip-desktop-input

不注入键鼠、不写剪贴板。会动磁盘的两处都已隔离：

- **策略判定**：要往规则文件与域名账本里写入测数据，全部只写临时副本（`isolated_ledgers`），
  跑完把模块常量还原——原来会覆盖用户的真实规则并塞一条 `*.example.com` allow。
- **截屏**：会往 `.cache/screenshots` 落一张图，用 `--skip-screen` 跳过；
  OCR 需要一张已有截图才能跑。

审计查询只读真实会话（读才是有意义的基准），不写任何东西。
"""
from __future__ import annotations

import argparse
import contextlib
import os
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))

import console  # noqa: E402

console.harden_stdio()


def timed(fn, repeat: int = 10):
    """跑 ``repeat`` 次，返回 (首次 ms, p50, p95, max)。"""
    first_started = time.perf_counter()
    fn()
    first = (time.perf_counter() - first_started) * 1000
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    p50 = statistics.median(samples)
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    return first, p50, p95, samples[-1]


def row(label: str, first: float, p50: float, p95: float, peak: float,
        note: str = '') -> None:
    print(f'  {label:34} first={first:8.1f}ms  p50={p50:7.2f}ms  '
          f'p95={p95:7.2f}ms  peak={peak:5.2f}MB  {note}')


def profile_desktop() -> None:
    print('\n[desktop] 前台窗口快照（只读，不注入键鼠）')
    import desktop_control_windows as desktop

    tracemalloc.start()
    first, p50, p95, _ = timed(desktop.snapshot, 10)
    peak = tracemalloc.get_traced_memory()[1] / 1048576
    tracemalloc.stop()
    row('desktop_snapshot', first, p50, p95, peak, '常驻 worker')

    os.environ['HARNESS_PS_WORKER'] = '0'
    try:
        first, p50, p95, _ = timed(desktop.snapshot, 3)
    finally:
        os.environ.pop('HARNESS_PS_WORKER', None)
    row('  （对照：独立进程）', first, p50, p95, 0.0, '每次起 PowerShell')


def profile_clipboard() -> None:
    print('\n[clipboard] 读取当前剪贴板（只读）')
    import clipboard_windows as clipboard

    tracemalloc.start()
    first, p50, p95, _ = timed(clipboard.read, 10)
    peak = tracemalloc.get_traced_memory()[1] / 1048576
    tracemalloc.stop()
    row('clipboard_read', first, p50, p95, peak, '常驻 worker')

    os.environ['HARNESS_PS_WORKER'] = '0'
    try:
        first, p50, p95, _ = timed(clipboard.read, 3)
    finally:
        os.environ.pop('HARNESS_PS_WORKER', None)
    row('  （对照：独立进程）', first, p50, p95, 0.0, '每次起 PowerShell')


def profile_screen() -> None:
    print('\n[screen] 截屏 + 本地 OCR（真实截图，写 .cache）')
    import harness
    import screen_capture

    if not screen_capture.stats(harness.SCREENSHOT_DIR)['files']:
        print('  （跳过：没有已有截图，OCR 需要一张）')
    started = time.perf_counter()
    shot = screen_capture.capture()
    capture_ms = (time.perf_counter() - started) * 1000
    print(f'  screen_capture                     {capture_ms:8.1f}ms   '
          f'{shot.width}x{shot.height}, {len(shot.png) // 1024}KB')

    import ocr_windows

    target = screen_capture.save_png(shot.png, harness.SCREENSHOT_DIR)
    started = time.perf_counter()
    ocr_windows.recognize(str(target))
    print(f'  screen_ocr                          '
          f'{(time.perf_counter() - started) * 1000:8.1f}ms')


@contextlib.contextmanager
def isolated_ledgers():
    """把会写盘的账本指到临时目录，结束后**原值恢复**。

    `rules.DURABLE_PATH` / `net_grants.DURABLE_PATH` 是模块级常量，在导入时就绑定了
    真实路径；`profile_policy()` 会写规则文件、往域名账本里加一条 allow——不改指就
    等于“跑一次基准，用户的自定义规则被换成脚本里那两条”。

    两者都按 (路径, mtime, 大小) 缓存，所以换路径后缓存自然失效，无需手动清。
    """
    import net_grants
    import rules

    saved = (rules.DURABLE_PATH, net_grants.DURABLE_PATH, net_grants.LOCK_PATH)
    with tempfile.TemporaryDirectory(prefix='profile-ledgers-') as tmp:
        base = Path(tmp)
        rules.DURABLE_PATH = base / 'rules.yaml'
        net_grants.DURABLE_PATH = base / 'net_grants.json'
        net_grants.LOCK_PATH = base / 'net_grants.lock'
        try:
            yield base
        finally:
            (rules.DURABLE_PATH, net_grants.DURABLE_PATH,
             net_grants.LOCK_PATH) = saved


def profile_policy() -> None:
    print('\n[policy] 每次工具调用的判定开销')
    import hooks
    import net_grants
    import rules

    # 规则与域名账本只写临时副本：这两行原来会改写真实授权（§9.3 R0-b1）。
    with isolated_ledgers():
        rules.write(rules.parse({'version': 1, 'rules': [
            {'decision': 'allow', 'prefixes': ['npm test']},
            {'decision': 'deny', 'prefixes': ['npm publish']},
        ]}))
        net_grants.add('*.example.com')

        # 计时与 tracer 分开跑：tracemalloc 会把每次分配的追踪开销算进墙钟。
        for label, tool, args in (
                ('evaluate_call(bash)', 'bash', {'command': 'ls -la'}),
                ('evaluate_call(web_fetch)', 'web_fetch',
                 {'url': 'https://docs.example.com/a'}),
                ('evaluate_call(desktop_action)', 'desktop_action',
                 {'action': 'click'}),
        ):
            first, p50, p95, _ = timed(
                lambda t=tool, a=args: hooks.evaluate_call(t, a), 20)
            row(label, first, p50, p95, 0.0)

        tracemalloc.start()
        hooks.evaluate_call('bash', {'command': 'ls -la'})
        peak = tracemalloc.get_traced_memory()[1] / 1048576
        tracemalloc.stop()
    print(f'  （单次判定峰值内存 {peak:.2f} MB；规则与域名账本只写临时副本）')


def profile_audit() -> None:
    print('\n[audit] 跨会话审计查询')
    import audit
    import harness

    tracemalloc.start()
    first, p50, p95, _ = timed(
        lambda: audit.collect(harness.SESSIONS_DIR, limit=100), 5)
    peak = tracemalloc.get_traced_memory()[1] / 1048576
    tracemalloc.stop()
    sessions = len(list(Path(harness.SESSIONS_DIR).glob('session_*.jsonl')))
    row(f'audit.collect({sessions} 会话)', first, p50, p95, peak)


def profile_worker_stats() -> None:
    import ps_worker

    stats = ps_worker.stats()
    if not stats:
        return
    print('\n[worker] 常驻进程汇总')
    for line in stats.values():
        print(f'  {line}')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='新增能力的真实延迟 profile')
    parser.add_argument('--skip-desktop-input', action='store_true',
                        help='跳过桌面快照（无图形会话时用）')
    parser.add_argument('--skip-screen', action='store_true',
                        help='跳过截屏（会写 .cache 并占用屏幕）')
    args = parser.parse_args(argv)

    print(f'python {sys.version.split()[0]}')
    if not args.skip_desktop_input:
        try:
            profile_desktop()
        except Exception as exc:
            print(f'  （跳过 desktop：{type(exc).__name__}: {exc}）')
    try:
        profile_clipboard()
    except Exception as exc:
        print(f'  （跳过 clipboard：{type(exc).__name__}: {exc}）')
    if not args.skip_screen:
        try:
            profile_screen()
        except Exception as exc:
            print(f'  （跳过 screen：{type(exc).__name__}: {exc}）')
    profile_policy()
    profile_audit()
    profile_worker_stats()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
