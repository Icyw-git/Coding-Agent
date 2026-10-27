"""OS 沙箱开销测量：决定是否把 HARNESS_OS_SANDBOX 默认打开。

回答三个问题：
1. 启用沙箱后 bash 命令慢多少（首次 + 稳态）？
2. 一次性副作用是什么（工作区打标、icacls 调用）？
3. Job Object 内存限额能不能真加上去？

用法::

    python scripts/bench_sandbox.py            # 全部
    python scripts/bench_sandbox.py --skip-write   # 不测写入行为
"""
from __future__ import annotations

import argparse
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))

import console  # noqa: E402

console.harden_stdio()

import sandbox  # noqa: E402


def _run(command: str, workspace: Path, timeout: float = 30) -> tuple[int, float, str]:
    """跑一条命令，返回 (退出码, 毫秒, 输出)。

    ``spawn_shell`` 的 stdout 参数要的是**真实文件对象**（沙箱路径会取
    ``fileno()`` 交给 CreateProcess），传 ``subprocess.PIPE`` 会在沙箱路径上
    直接 AttributeError——这是测量脚本自己的坑，不是产品 bug。
    """
    started = time.perf_counter()
    handle = tempfile.TemporaryFile()
    try:
        proc = sandbox.spawn_shell(command, cwd=str(workspace),
                                  stdout=handle, workspace=workspace)
        try:
            code = proc.wait(timeout=timeout)
        except Exception:
            code = -1
            if hasattr(proc, 'kill_tree'):
                try:
                    proc.kill_tree()
                except Exception:
                    pass
        handle.seek(0)
        text = handle.read().decode('utf-8', 'replace')
    finally:
        try:
            handle.close()
        except OSError:
            pass
    elapsed = (time.perf_counter() - started) * 1000
    return code, elapsed, text


def _sample(command: str, workspace: Path, mode: str, repeat: int = 5):
    os.environ[sandbox.ENV] = mode
    sandbox.reset_cache()
    timings = []
    first = None
    for index in range(repeat):
        code, ms, _ = _run(command, workspace)
        if index == 0:
            first = ms
        else:
            timings.append(ms)
        if code != 0:
            return {'error': f'exit {code}', 'first': first}
    return {'first': first, 'p50': statistics.median(timings),
            'min': min(timings), 'max': max(timings)}


def bench_overhead(workspace: Path) -> None:
    print('\n[1] 命令执行开销')
    cases = [
        ('空命令 (echo)', 'echo hi'),
        ('python -c 启动', 'python -c "print(1)"'),
        ('目录列举', 'dir /b'),
    ]
    for label, command in cases:
        off = _sample(command, workspace, 'off')
        if 'error' in off:
            print(f'  {label:18} off 失败: {off["error"]}')
            continue
        auto = _sample(command, workspace, 'auto')
        if 'error' in auto:
            print(f'  {label:18} auto 失败: {auto["error"]}')
            continue
        delta = auto['p50'] - off['p50']
        pct = (delta / off['p50'] * 100) if off['p50'] else 0
        print(f'  {label:18} off p50={off["p50"]:7.1f}ms   '
              f'auto p50={auto["p50"]:7.1f}ms   '
              f'差值 {delta:+7.1f}ms ({pct:+.0f}%)   首次 {auto["first"]:7.1f}ms')


def bench_workspace_label(workspace: Path) -> None:
    print('\n[2] 工作区打标（一次性）')
    fresh = Path(tempfile.mkdtemp()) / 'ws'
    fresh.mkdir(parents=True)
    os.environ[sandbox.ENV] = 'auto'
    sandbox.reset_cache()
    started = time.perf_counter()
    ok, reason = sandbox._ensure_workspace(fresh)
    ms = (time.perf_counter() - started) * 1000
    print(f'  首次启用: {ms:.0f} ms  ok={ok} reason={reason or "-"}')
    started = time.perf_counter()
    sandbox._ensure_workspace(fresh)
    print(f'  再次调用（已缓存）: {(time.perf_counter() - started) * 1000:.1f} ms')
    # 打标后工作区是否仍可正常写入
    probe = fresh / 'probe.txt'
    try:
        probe.write_text('x', encoding='utf-8')
        print('  打标后工作区写入: 正常')
    except OSError as exc:
        print(f'  打标后工作区写入: 失败 {exc}')


def bench_isolation(workspace: Path) -> None:
    print(chr(10) + "[3] 隔离效果：工作区外写入")
    print("  （每档用完全独立的工作区与目标目录：打标是持久的）")
    for mode in ("off", "auto"):
        os.environ[sandbox.ENV] = mode
        sandbox.reset_cache()      # 不重置会沿用上一档的缓存判定
        base = Path(tempfile.mkdtemp())
        ws = base / "ws"
        ws.mkdir()
        target_dir = Path(tempfile.mkdtemp())      # 与工作区无亲缘关系
        outside = target_dir / "outside.txt"
        code, _, _ = _run(f'echo x > "{outside}"', ws, timeout=20)
        exists = outside.exists()
        verdict = "写入成功（无隔离）" if exists else "被内核拒绝（隔离生效）"
        print(f"  {mode:6} exit={code:<3} {verdict}")
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(target_dir, ignore_errors=True)

    os.environ[sandbox.ENV] = "auto"
    sandbox.reset_cache()
    base = Path(tempfile.mkdtemp())
    ws = base / "ws"
    ws.mkdir()
    inside = ws / "inside.txt"
    _run(f'echo x > "{inside}"', ws, timeout=20)
    print("  工作区内写入：" + ("正常" if inside.exists() else "被拒（异常）"))
    shutil.rmtree(base, ignore_errors=True)


def bench_job_memory(workspace: Path) -> None:
    print(chr(10) + "[4] Job Object 内存限额现状")
    print("  _make_job() 只设了 KILL_ON_JOB_CLOSE，未设任何内存限制。")
    print("  JOBOBJECT_EXTENDED_LIMIT_INFORMATION 虽有 ProcessMemoryLimit /")
    print("  JobMemoryLimit 字段，但没有对应 LimitFlags 位，等于从未生效。")
    print("  → 要生效需 JOB_OBJECT_LIMIT_PROCESS_MEMORY(0x100) 或")
    print("    JOB_OBJECT_LIMIT_JOB_MEMORY(0x200) + 对应字段值。")
    print("  → 但 Job Object 只在沙箱启用时创建；off 时根本不参与。")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='OS 沙箱开销测量')
    parser.add_argument('--skip-write', action='store_true',
                        help='跳过写入隔离测试')
    args = parser.parse_args(argv)

    if os.name != 'nt':
        print('本脚本只测 Windows Low IL 后端')
        return 0

    workspace = Path(tempfile.mkdtemp()) / 'bench-ws'
    workspace.mkdir(parents=True)
    print(f'python {sys.version.split()[0]}')
    print(f'workspace {workspace}')
    print(f'当前模式 {sandbox.configured_mode()}')

    bench_overhead(workspace)
    bench_workspace_label(workspace)
    if not args.skip_write:
        bench_isolation(workspace)
    bench_job_memory(workspace)

    os.environ[sandbox.ENV] = 'off'
    sandbox.reset_cache()
    print(f'\n已恢复模式 {sandbox.configured_mode()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
