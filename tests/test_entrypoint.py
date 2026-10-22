import entrypoint
import headless


def test_build_parser_supports_doctor_and_resume():
    args = entrypoint.build_parser().parse_args(['--doctor'])
    assert args.doctor is True
    assert args.resume is None

    args = entrypoint.build_parser().parse_args(['--resume', 'session-1'])
    assert args.doctor is False
    assert args.resume == 'session-1'


def test_build_parser_supports_headless_flags():
    args = entrypoint.build_parser().parse_args([
        '-p', 'inspect', '--json', '--session', 's-1',
        '--workspace', 'ws', '--image', 'a.png', '--image', 'b.png',
    ])
    assert args.print_mode is True
    assert args.json is True
    assert args.session == 's-1'
    assert args.workspace == 'ws'
    assert args.image == ['a.png', 'b.png']
    assert args.prompt == 'inspect'
    assert entrypoint._headless_requested(args) is True


def test_positional_prompt_enables_headless_mode():
    args = entrypoint.build_parser().parse_args(['summarize the repo'])
    assert args.prompt == 'summarize the repo'
    assert entrypoint._headless_requested(args) is True

    interactive = entrypoint.build_parser().parse_args([])
    assert entrypoint._headless_requested(interactive) is False


def test_main_dispatches_headless_without_starting_services(monkeypatch):
    seen = []

    def fake_run(args):
        seen.append((args.prompt, args.json))
        return 7

    monkeypatch.setattr(headless, 'run', fake_run)

    assert entrypoint.main(['-p', 'hello', '--json']) == 7
    assert seen == [('hello', True)]


def test_main_rejects_resume_in_headless_mode(monkeypatch, capsys):
    def unexpected(_args):
        raise AssertionError('headless must not start on --resume')

    monkeypatch.setattr(headless, 'run', unexpected)

    assert entrypoint.main(['-p', 'hello', '--resume', 's-1']) == 2
    assert '--session' in capsys.readouterr().err


def test_main_doctor_returns_without_starting_services(monkeypatch):
    called = []
    monkeypatch.setattr(entrypoint.harness, 'run_doctor', lambda: called.append('doctor') or 3)

    assert entrypoint.main(['--doctor']) == 3
    assert called == ['doctor']


def test_doctor_reports_optional_document_extras(monkeypatch):
    monkeypatch.setattr('importlib.util.find_spec', lambda name: None)
    missing = entrypoint.harness._document_extras_status()
    assert 'pypdf' in missing and 'pypdfium2' in missing
    assert 'requirements-docs.txt' in missing

    monkeypatch.setattr('importlib.util.find_spec', lambda name: object())
    assert entrypoint.harness._document_extras_status() == 'installed'


def test_interactive_lock_defaults_to_textual_and_respects_ansi(monkeypatch):
    import contextlib

    monkeypatch.delenv('HARNESS_UI', raising=False)
    assert isinstance(entrypoint._interactive_lock(), contextlib.nullcontext)

    monkeypatch.setenv('HARNESS_UI', 'textual')
    ctx = entrypoint._interactive_lock()
    assert isinstance(ctx, contextlib.nullcontext)

    monkeypatch.setenv('HARNESS_UI', 'textual-next')
    ctx = entrypoint._interactive_lock()
    assert isinstance(ctx, contextlib.nullcontext)

    monkeypatch.setenv('HARNESS_UI', 'ansi')
    assert entrypoint._interactive_lock() is entrypoint.harness.agent_lock


def test_ui_flag_overrides_env(monkeypatch):
    monkeypatch.setenv('HARNESS_UI', 'textual')
    called = []
    monkeypatch.setattr(entrypoint.harness, 'run_doctor',
                        lambda: called.append(True) or 0)

    assert entrypoint.main(['--ui', 'ansi', '--doctor']) == 0
    assert entrypoint.os.environ['HARNESS_UI'] == 'ansi'
    assert called


def test_textual_failure_falls_back_to_line_repl(tmp_path, monkeypatch, capsys):
    import sys

    import cli_input
    import harness
    from ui import harness_app

    monkeypatch.setenv('HARNESS_UI', 'textual')
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(harness_app, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError('no driver')))
    monkeypatch.setattr(cli_input, 'create_reader', lambda *args: lambda _: '/quit')
    store = harness.SessionEventStore(tmp_path, session_id='ui-fallback')

    harness.run_repl([], store)

    output = capsys.readouterr().out
    assert '回退行式 REPL' in output and 'no driver' in output


def test_interactive_starts_before_mcp_finishes(monkeypatch):
    import threading
    import time

    import harness

    started = threading.Event()
    release = threading.Event()

    def slow_init():
        started.set()
        release.wait(5)

    monkeypatch.setattr(harness, 'initialize_mcp_tools', slow_init)
    monkeypatch.setattr(harness, '_mcp_async_started', threading.Event())
    monkeypatch.setattr(harness, '_mcp_ready', threading.Event())
    monkeypatch.setattr(harness, 'validate_runtime_config', lambda: None)
    monkeypatch.setattr(harness, 'load_durable_jobs', lambda: None)
    monkeypatch.setattr(harness, 'cron_scheduler_loop', lambda: None)
    monkeypatch.setattr(harness, 'queue_processor_loop', lambda *a, **k: None)
    monkeypatch.setattr(harness, 'wait_for_cron_idle', lambda: None)
    monkeypatch.setattr(harness, 'wait_for_mcp_tools', lambda timeout=10: True)
    monkeypatch.setattr(harness, 'shutdown_mcp', lambda: None)
    monkeypatch.setattr(harness.client, 'warmup', lambda: None)

    seen = []
    start = time.monotonic()

    def fake_repl(*args, **kwargs):
        seen.append(time.monotonic() - start)

    monkeypatch.setattr(harness, 'run_repl', fake_repl)

    entrypoint.main([])

    assert started.is_set()          # MCP 初始化已经开始
    assert seen and seen[0] < 0.5    # 但 REPL 没有等它（后台线程）
    release.set()
