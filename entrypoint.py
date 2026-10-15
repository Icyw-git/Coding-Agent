"""Command-line entrypoint for Harness Agent."""

import argparse
import threading

import harness


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Harness Agent')
    parser.add_argument('--doctor', action='store_true', help='check local configuration and exit')
    parser.add_argument('--resume', metavar='SESSION_ID', help='resume a saved session')
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.doctor:
        return harness.run_doctor()
    try:
        harness.validate_runtime_config()
    except RuntimeError as exc:
        raise SystemExit(f'[config] {exc}') from exc

    harness.load_durable_jobs()
    threading.Thread(target=harness.cron_scheduler_loop, daemon=True).start()
    threading.Thread(
        target=harness.queue_processor_loop,
        args=(harness.run_agent_turn_locked,),
        daemon=True,
    ).start()

    harness.initialize_mcp_tools()
    with harness.agent_lock:
        try:
            if args.resume:
                store = harness.SessionEventStore.open(
                    harness.resolve_session_path(args.resume)
                )
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
        harness.shutdown_mcp()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
