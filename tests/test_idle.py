"""空闲/锁屏探测：JSON 解析、错误收敛与 2 秒缓存。"""
import base64
import subprocess
from types import SimpleNamespace

import pytest

import idle


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    idle.reset()
    monkeypatch.delenv('HARNESS_IDLE', raising=False)
    monkeypatch.setattr(idle, 'PLATFORM', 'win32')
    yield
    idle.reset()


class Recorder:
    def __init__(self, stdout='', *, returncode=0, stderr=''):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, command, timeout):
        self.calls.append((command, timeout))
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout,
                               stderr=self.stderr)


def _script(command):
    assert command[:4] == ['powershell.exe', '-NoProfile', '-NonInteractive',
                           '-EncodedCommand']
    return base64.b64decode(command[4]).decode('utf-16-le')


def test_parses_idle_and_lock_state():
    recorder = Recorder('{"idle_seconds":67.9,"locked":false}')
    data = idle.state(runner=recorder)
    assert data['available'] is True
    assert data['idle_seconds'] == 67
    assert data['locked'] is False
    assert data['checked_at'].endswith('Z')
    assert 'error' not in data
    command, timeout = recorder.calls[0]
    assert timeout == idle.TIMEOUT
    script = _script(command)
    assert 'GetLastInputInfo' in script
    assert 'OpenInputDesktop' in script


def test_locked_and_negative_idle_is_clamped():
    recorder = Recorder('{"idle_seconds":-3,"locked":true}')
    data = idle.state(runner=recorder)
    assert data == {**data, 'available': True, 'idle_seconds': 0, 'locked': True}


def test_result_is_cached_until_ttl_or_force():
    first = Recorder('{"idle_seconds":5,"locked":false}')
    data = idle.state(runner=first)
    cached = idle.state(runner=Recorder('{"idle_seconds":99,"locked":true}'))
    assert cached == data
    assert len(first.calls) == 1

    fresh = Recorder('{"idle_seconds":42,"locked":true}')
    forced = idle.state(runner=fresh, force=True)
    assert forced['idle_seconds'] == 42
    assert forced['locked'] is True
    assert len(fresh.calls) == 1

    idle.reset()
    after = Recorder('{"idle_seconds":7,"locked":false}')
    assert idle.state(runner=after)['idle_seconds'] == 7
    assert len(after.calls) == 1


def test_failures_are_cached_too():
    recorder = Recorder('not json')
    assert idle.state(runner=recorder)['error'] == 'invalid output'
    assert idle.state(runner=recorder)['error'] == 'invalid output'
    assert len(recorder.calls) == 1


def test_bom_and_whitespace_are_tolerated():
    recorder = Recorder('\ufeff {"idle_seconds": 3, "locked": false} \r\n')
    data = idle.state(runner=recorder)
    assert data['available'] is True
    assert data['idle_seconds'] == 3


def test_null_idle_seconds_is_partial_failure():
    recorder = Recorder('{"idle_seconds":null,"locked":true}')
    data = idle.state(runner=recorder)
    assert data['available'] is False
    assert data['idle_seconds'] is None
    assert data['locked'] is True
    assert data['error'] == 'idle_unavailable'


def test_invalid_locked_value_becomes_none():
    recorder = Recorder('{"idle_seconds":1,"locked":"yes"}')
    assert idle.state(runner=recorder)['locked'] is None


def test_nonzero_exit_reports_stderr():
    recorder = Recorder('', returncode=1, stderr='Add-Type failed\nline two')
    data = idle.state(runner=recorder)
    assert data['available'] is False
    assert data['error'] == 'Add-Type failed line two'


def test_timeout_is_reported():
    def slow(command, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    data = idle.state(runner=slow)
    assert data['available'] is False
    assert data['error'] == 'timeout'


def test_os_error_is_reported():
    def broken(command, timeout):
        raise OSError('powershell missing')

    data = idle.state(runner=broken)
    assert data['available'] is False
    assert 'powershell missing' in data['error']


def test_env_opt_out(monkeypatch):
    monkeypatch.setenv('HARNESS_IDLE', '0')
    recorder = Recorder('{"idle_seconds":1,"locked":false}')
    data = idle.state(runner=recorder)
    assert data['available'] is False
    assert data['error'] == 'disabled'
    assert data['locked'] is None
    assert recorder.calls == []


def test_non_windows_is_unsupported(monkeypatch):
    monkeypatch.setattr(idle, 'PLATFORM', 'darwin')
    recorder = Recorder('{"idle_seconds":1,"locked":false}')
    assert idle.state(runner=recorder)['error'] == 'unsupported'
    assert recorder.calls == []
