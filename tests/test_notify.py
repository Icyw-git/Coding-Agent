"""本地 Toast：命令编码、XML 转义、限频与失败隔离。"""
import base64
import re
import subprocess
from types import SimpleNamespace

import pytest

import notify


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    notify.reset()
    monkeypatch.delenv('HARNESS_NOTIFY', raising=False)
    monkeypatch.setattr(notify, 'PLATFORM', 'win32')
    yield
    notify.reset()


class Recorder:
    def __init__(self, returncode=0, stdout='ok', stderr=''):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, command, timeout):
        self.calls.append((command, timeout))
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout,
                               stderr=self.stderr)


def _script(command):
    assert command[:4] == ['powershell.exe', '-NoProfile', '-NonInteractive',
                           '-EncodedCommand']
    return base64.b64decode(command[4]).decode('utf-16-le')


def _xml(script):
    match = re.search(r"FromBase64String\('([^']+)'\)", script)
    assert match is not None
    return base64.b64decode(match.group(1)).decode('utf-8')


def test_sends_escaped_toast_through_powershell():
    recorder = Recorder()
    result = notify.notify('构建 <失败> & "x"', '日志：a < b', level='error',
                           runner=recorder)
    assert result == {'sent': True, 'level': 'error'}
    assert len(recorder.calls) == 1
    command, timeout = recorder.calls[0]
    assert timeout == notify.TIMEOUT
    script = _script(command)
    assert notify.AUMID in script
    xml = _xml(script)
    assert '&lt;失败&gt; &amp; "x"' in xml
    assert '日志：a &lt; b' in xml
    assert '<audio silent="true"/>' not in xml


def test_info_level_is_silent_and_bad_level_falls_back():
    recorder = Recorder()
    notify.notify('提醒', '喝水', runner=recorder)
    assert '<audio silent="true"/>' in _xml(_script(recorder.calls[0][0]))

    notify.reset()
    notify.notify('提醒', '喝水', level='nonsense', runner=recorder)
    assert '<audio silent="true"/>' in _xml(_script(recorder.calls[1][0]))


def test_empty_title_gets_default_and_long_body_is_capped():
    recorder = Recorder()
    notify.notify(None, 'x' * 1000, runner=recorder)
    xml = _xml(_script(recorder.calls[0][0]))
    assert '<text>Harness Agent</text>' in xml
    assert 'x' * (notify.MAX_BODY - 1) + '…' in xml


def test_control_characters_are_stripped():
    recorder = Recorder()
    notify.notify('a\x00b', 'c\x07d', runner=recorder)
    xml = _xml(_script(recorder.calls[0][0]))
    assert '<text>ab</text>' in xml
    assert '<text>cd</text>' in xml


def test_rate_limit_is_global_and_resettable():
    recorder = Recorder()
    assert notify.notify('first', runner=recorder)['sent'] is True
    limited = notify.notify('second', runner=recorder)
    assert limited['sent'] is False
    assert limited['reason'] == 'rate_limited'
    assert 0 < limited['retry_in'] <= notify.MIN_INTERVAL
    assert len(recorder.calls) == 1

    notify.reset()
    assert notify.notify('third', runner=recorder)['sent'] is True
    assert len(recorder.calls) == 2


def test_env_opt_out_never_spawns_powershell(monkeypatch):
    monkeypatch.setenv('HARNESS_NOTIFY', 'off')
    recorder = Recorder()
    result = notify.notify('title', runner=recorder)
    assert result == {'sent': False, 'reason': 'disabled', 'level': 'info'}
    assert recorder.calls == []


def test_non_windows_is_unsupported(monkeypatch):
    monkeypatch.setattr(notify, 'PLATFORM', 'linux')
    recorder = Recorder()
    assert notify.notify('title', runner=recorder)['reason'] == 'unsupported'
    assert recorder.calls == []


def test_nonzero_exit_returns_failure():
    recorder = Recorder(returncode=1, stderr='access denied')
    result = notify.notify('title', runner=recorder)
    assert result['sent'] is False
    assert result['reason'] == 'failed'
    assert result['exit_code'] == 1
    assert result['error'] == 'access denied'


def test_runner_exceptions_are_returned_not_raised():
    def broken(command, timeout):
        raise OSError('powershell missing')

    result = notify.notify('title', runner=broken)
    assert result['sent'] is False
    assert result['reason'] == 'failed'
    assert 'powershell missing' in result['error']


def test_timeout_returns_failure():
    def slow(command, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    result = notify.notify('title', runner=slow)
    assert result == {'sent': False, 'reason': 'timeout', 'level': 'info'}


def test_failed_attempt_also_counts_toward_rate_limit():
    recorder = Recorder(returncode=1, stderr='boom')
    assert notify.notify('a', runner=recorder)['reason'] == 'failed'
    assert notify.notify('b', runner=recorder)['reason'] == 'rate_limited'
    assert len(recorder.calls) == 1
