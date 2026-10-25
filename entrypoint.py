"""Command-line entrypoint for Harness Agent."""

import argparse
import contextlib
import os
import sys
import threading
from pathlib import Path

import console
import harness

console.harden_stdio()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Harness Agent')
    parser.add_argument('--doctor', action='store_true', help='check local configuration and exit')
    parser.add_argument(
        '--ui',
        choices=('ansi', 'textual', 'textual-next'),
        help='override HARNESS_UI for this run (default textual; ansi keeps native scrollback)',
    )
    parser.add_argument('--resume', metavar='SESSION_ID', help='resume a saved session')
    parser.add_argument('prompt', nargs='?', help='one-shot prompt (enables headless mode)')
    parser.add_argument(
        '-p',
        '--print',
        dest='print_mode',
        action='store_true',
        help='run one turn without the REPL and print the answer',
    )
    parser.add_argument('--json', action='store_true', help='emit a JSONL event stream on stdout (implies -p)')
    parser.add_argument(
        '--session', metavar='SESSION_ID', help='resume this session, or create it when missing (implies -p)'
    )
    parser.add_argument('--workspace', metavar='PATH', help='workspace for this headless session (implies -p)')
    parser.add_argument(
        '-i',
        '--image',
        action='append',
        default=[],
        metavar='PATH',
        help='attach an image to the prompt (repeatable; implies -p)',
    )
    return parser


def _warn_legacy_sessions() -> None:
    """旧行为把 .cache 放在启动目录；home 切换后给出一条可操作的提示。"""
    import harness

    legacy = Path.cwd() / '.cache' / 'sessions'
    try:
        if legacy.resolve() == harness.SESSIONS_DIR.resolve():
            return
        if not legacy.is_dir() or not any(legacy.glob('session_*.jsonl')):
            return
        if any(harness.SESSIONS_DIR.glob('session_*.jsonl')):
            return
    except OSError:
        return
    print(f'[home] 当前目录存在旧会话缓存 {legacy}，但 agent home 是 '
          f'{harness.HOME_DIR}；如需沿用请设置 HARNESS_HOME={Path.cwd()}',
          file=sys.stderr)


def _headless_requested(args) -> bool:
    return bool(
        args.print_mode
        or args.json
        or args.session is not None
        or args.workspace is not None
        or args.image
        or args.prompt is not None
    )


def _interactive_lock():
    """行式 REPL 全程持 agent_lock，阻止 cron 队列插话。

    Textual 模式下锁由 UI 的 agent worker 按「每个回合」持有（空闲时释放，
    让 cron 有机会执行）；entrypoint 再包一层会让 worker 永远拿不到锁——
    表现就是输入后一直停在「正在处理」。
    """
    if os.getenv('HARNESS_UI', 'textual') in ('textual', 'textual-next'):
        return contextlib.nullcontext()
    return harness.agent_lock


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.ui:  # 显式参数优先于 .env：TUI 卡住时可以直接 --ui ansi 救场
        os.environ['HARNESS_UI'] = args.ui

    if args.doctor:
        return harness.run_doctor()
    if _headless_requested(args):
        if args.resume:
            # --resume 只属于交互式会话；无头执行恢复会话用 --session。
            # 两者同时给时宁可直接报用法，也不要静默新建一个会话。
            print('[headless] --resume 仅用于交互模式；无头执行请改用 --session', file=sys.stderr)
            return 2
        from headless import run

        return run(args)
    try:
        harness.validate_runtime_config()
    except RuntimeError as exc:
        raise SystemExit(f'[config] {exc}') from exc

    harness.load_durable_jobs()
    # 默认心跳与 API 常驻模式共用同一张 cron 表；稳定 ID 保证重复启动不叠加。
    import heartbeat
    heartbeat.ensure_registered()
    _warn_legacy_sessions()
    harness.start_storage_maintenance()
    # 后台预热 LLM 客户端（openai SDK import ~1.5s）：用户敲第一条消息时已就绪
    threading.Thread(target=harness.client.warmup, daemon=True).start()
    threading.Thread(target=harness.cron_scheduler_loop, daemon=True).start()
    threading.Thread(
        target=harness.queue_processor_loop,
        args=(harness.run_agent_turn_locked,),
        daemon=True,
    ).start()

    harness.start_mcp_initialization_async()
    with _interactive_lock():
        try:
            if args.resume:
                store = harness.SessionEventStore.open(harness.resolve_session_path(args.resume))
                harness.run_repl(store.rebuild_openai_history(), session_store=store)
            else:
                harness.run_repl([])
        except (
            FileNotFoundError,
            ValueError,
            harness.SessionReplayError,
        ) as exc:
            print(f'[session] {exc}')
        except KeyboardInterrupt:
            print('\n[会话已中断]')

    try:
        harness.wait_for_cron_idle()
    except KeyboardInterrupt:
        print('\n[会话已中断]')
    finally:
        # 回合后的标题/记忆整理还在写会话日志：先有界等待，超时留下带会话
        # id 的告警，再关 MCP，避免后台写入与进程退出赛跑。
        harness.shutdown_housekeeping(timeout=10)
        harness.wait_for_mcp_tools(10)
        harness.shutdown_mcp()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
