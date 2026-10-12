import entrypoint


def test_build_parser_supports_doctor_and_resume():
    args = entrypoint.build_parser().parse_args(['--doctor'])
    assert args.doctor is True
    assert args.resume is None

    args = entrypoint.build_parser().parse_args(['--resume', 'session-1'])
    assert args.doctor is False
    assert args.resume == 'session-1'


def test_main_doctor_returns_without_starting_services(monkeypatch):
    called = []
    monkeypatch.setattr(entrypoint.harness, 'run_doctor', lambda: called.append('doctor') or 3)

    assert entrypoint.main(['--doctor']) == 3
    assert called == ['doctor']
