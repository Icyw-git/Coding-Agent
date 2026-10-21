"""tool_exec：超时校验、审批门与联网抓取边界。"""
import tool_exec
import web_access


def _gate(denied=None, log=None):
    log = [] if log is None else log

    def gate(name, args):
        log.append((name, args))
        return denied

    return gate, log


def _bash_kwargs(tmp_path, gate):
    return {'workdir': tmp_path, 'gate': gate,
            'default_timeout': 1, 'background_timeout': 2, 'max_timeout': 3}


def test_bash_timeout_validation():
    defaults = {'default': 120, 'background': 3600, 'maximum': 3600}
    assert tool_exec.bash_timeout(None, **defaults) == 120
    assert tool_exec.bash_timeout(5, **defaults) == 5

    for bad in (0, -1, 3601, 'x'):
        try:
            tool_exec.bash_timeout(bad, **defaults)
        except ValueError as exc:
            assert 'timeout' in str(exc)
        else:
            raise AssertionError(f'{bad!r} should be rejected')


def test_bash_denial_skips_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_exec.sandbox, 'spawn_shell',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('must not spawn')))
    gate, log = _gate(denied='Error: denied')

    result = tool_exec.bash('echo hi', cwd=str(tmp_path),
                            **_bash_kwargs(tmp_path, gate))

    assert result == 'Error: denied'
    assert [entry[0] for entry in log] == ['bash']


def test_bash_rejects_bad_timeout_before_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_exec.sandbox, 'spawn_shell',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('must not spawn')))
    gate, _ = _gate()

    result = tool_exec.bash('echo hi', cwd=str(tmp_path), timeout=0,
                            **_bash_kwargs(tmp_path, gate))

    assert 'timeout' in result


def test_fetch_url_denial_skips_network(tmp_path, monkeypatch):
    monkeypatch.setattr(web_access, 'check_url', lambda url: None)
    monkeypatch.setattr(web_access, 'fetch',
                        lambda url: (_ for _ in ()).throw(
                            AssertionError('must not fetch')))
    gate, _ = _gate(denied='Error: denied')

    assert tool_exec.fetch_url('https://example.com', gate=gate) == 'Error: denied'


def test_fetch_url_truncates_and_reports_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(web_access, 'check_url', lambda url: None)
    monkeypatch.setattr(web_access, 'fetch', lambda url: {
        'text': 'x' * 50, 'final_url': 'https://example.com/',
        'content_type': 'text/plain', 'truncated': True})
    gate, log = _gate()

    output = tool_exec.fetch_url('https://example.com', max_chars=10, gate=gate)

    assert 'Content-Type: text/plain' in output
    assert 'truncated; 40 chars omitted' in output
    assert 'exceeded 2 MiB' in output
    assert log == [('web_fetch', {'url': 'https://example.com'})]


def test_background_output_validates_id():
    assert tool_exec.run_background_output('') == 'Error: bg_id must be a non-empty string'
    assert tool_exec.run_kill_background('  ') == 'Error: bg_id must be a non-empty string'
