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


# ---------- 输出截断 ----------

def test_truncate_output_keeps_the_marker():
    """截断必须说出来。

    同步 bash 原来静默截到 5 万字，同一文件的 background_output 却会标记；
    模型拿到 5 万字当成完整输出，末尾那行报错就看不见了。
    """
    text = 'x' * (tool_exec.MAX_OUTPUT_CHARS + 120)
    out = tool_exec._truncate_output(text)
    assert len(out) > tool_exec.MAX_OUTPUT_CHARS
    assert '已截断' in out and str(len(text)) in out
    out.encode('gbk')


def test_truncate_output_leaves_short_text_alone():
    assert tool_exec._truncate_output('abc') == 'abc'
    edge = 'y' * tool_exec.MAX_OUTPUT_CHARS
    assert tool_exec._truncate_output(edge) == edge


def test_bash_marks_truncated_output(tmp_path, monkeypatch):
    """走真实的 bash 路径确认标记会出现（不是只有辅助函数会）。"""
    class FakeProc:
        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_spawn(command, *, cwd, stdout, workspace):
        stdout.write(b'z' * (tool_exec.MAX_OUTPUT_CHARS + 50))
        stdout.flush()
        return FakeProc()

    monkeypatch.setattr(tool_exec.sandbox, 'spawn_shell', fake_spawn)
    monkeypatch.setattr(tool_exec, 'close_process', lambda proc: None)
    gate, _ = _gate()

    result = tool_exec.bash('echo hi', cwd=str(tmp_path),
                            **_bash_kwargs(tmp_path, gate))

    assert '已截断' in result
    result.encode('gbk')
