"""剪贴板能力层：base64 传输、截断、错误收敛（不碰真实剪贴板）。"""
import base64
import subprocess

import pytest

import clipboard_windows


class Result:
    def __init__(self, stdout=b'', stderr=b'', returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def fake_output(text):
    data = text.encode('utf-8')
    return f'{len(data)}\n{base64.b64encode(data).decode("ascii")}'.encode('ascii')


def test_read_decodes_utf8_and_reports_size():
    seen = {}

    def runner(script, payload, *, timeout, env):
        seen.update(script=script, payload=payload, timeout=timeout, env=env)
        return Result(stdout=fake_output('复制 📋 done'))

    result = clipboard_windows.read(runner=runner, timeout=3)

    assert result.text == '复制 📋 done'
    assert result.total_bytes == len('复制 📋 done'.encode())
    assert result.truncated is False
    assert result.duration_ms >= 0
    assert seen['payload'] == b''
    assert seen['timeout'] == 3
    assert int(seen['env']['HARNESS_CLIPBOARD_MAX_BYTES']) == clipboard_windows.MAX_READ_BYTES


def test_read_truncates_at_byte_cap():
    raw = b'a' * (clipboard_windows.MAX_READ_BYTES + 10)

    def runner(script, payload, *, timeout, env):
        return Result(stdout=(f'{len(raw)}\n'
                              f'{base64.b64encode(raw[:clipboard_windows.MAX_READ_BYTES]).decode()}'
                              ).encode())

    result = clipboard_windows.read(runner=runner)

    assert result.truncated is True
    assert result.total_bytes == len(raw)
    assert len(result.text.encode()) == clipboard_windows.MAX_READ_BYTES


def test_read_empty_clipboard():
    def runner(script, payload, *, timeout, env):
        return Result(stdout=b'0\n')

    result = clipboard_windows.read(runner=runner)
    assert result.text == ''
    assert result.total_bytes == 0
    assert result.truncated is False


def test_read_rejects_malformed_output():
    with pytest.raises(clipboard_windows.ClipboardError, match='unexpected'):
        clipboard_windows.read(runner=lambda *a, **k: Result(stdout=b'not-a-size\n'))

    with pytest.raises(clipboard_windows.ClipboardError, match='invalid text'):
        clipboard_windows.read(runner=lambda *a, **k: Result(stdout=b'4\n!!!!'))


def test_read_surfaces_powershell_error():
    def runner(*args, **kwargs):
        return Result(stderr=b'clipboard is locked', returncode=2)

    with pytest.raises(clipboard_windows.ClipboardError, match='clipboard is locked'):
        clipboard_windows.read(runner=runner)


def test_read_timeout_is_a_clipboard_error():
    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd='powershell', timeout=1)

    with pytest.raises(clipboard_windows.ClipboardError, match='timed out'):
        clipboard_windows.read(runner=runner)


def test_missing_powershell_is_reported(monkeypatch):
    monkeypatch.setattr(clipboard_windows, '_powershell_exe', lambda: None)
    with pytest.raises(clipboard_windows.ClipboardError, match='Windows PowerShell'):
        clipboard_windows.read()


def test_write_sends_utf8_base64_payload():
    seen = {}

    def runner(script, payload, *, timeout, env):
        seen['script'] = script
        seen['payload'] = payload
        seen['env'] = env
        return Result()

    result = clipboard_windows.write('你好\nworld', runner=runner, timeout=2)

    assert base64.b64decode(seen['payload']).decode() == '你好\nworld'
    assert seen['env'] == {}
    assert result.chars == len('你好\nworld')
    assert result.bytes == len('你好\nworld'.encode())
    assert result.duration_ms >= 0


def test_write_allows_clearing_and_rejects_non_strings():
    seen = {}

    def runner(script, payload, *, timeout, env):
        seen['payload'] = payload
        return Result()

    result = clipboard_windows.write('', runner=runner)
    assert result.chars == 0 and result.bytes == 0
    assert base64.b64decode(seen['payload']) == b''

    with pytest.raises(clipboard_windows.ClipboardError, match='must be a string'):
        clipboard_windows.write(None, runner=runner)
