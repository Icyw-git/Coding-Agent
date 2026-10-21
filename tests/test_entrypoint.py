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
