"""clipboard_read / clipboard_write 工具层：格式化与审计脱敏。"""
import clipboard_windows
import tool_clipboard


class _Store:
    def __init__(self):
        self.events = []

    def append(self, event, **payload):
        self.events.append((event, payload))


def read_result(text='hello', *, total=None, truncated=False, duration=7):
    total = len(text.encode()) if total is None else total
    return clipboard_windows.ClipboardRead(
        text=text, total_bytes=total, truncated=truncated, duration_ms=duration)


def test_read_formats_text_and_audits_lengths(monkeypatch):
    events = []

    def audit(event, **payload):
        events.append((event, payload))

    monkeypatch.setattr(clipboard_windows, 'read',
                        lambda **kwargs: read_result('hello'))
    output = tool_clipboard.read(audit=audit)

    assert output == 'Clipboard text: 5 bytes\n\nhello'
    assert events == [('clipboard_read', {
        'bytes': 5, 'chars': 5, 'truncated': False, 'duration_ms': 7})]
    assert 'hello' not in repr(events)


def test_read_empty_and_truncated_messages(monkeypatch):
    monkeypatch.setattr(clipboard_windows, 'read',
                        lambda **kwargs: read_result('', total=0))
    assert 'contains no text' in tool_clipboard.read()

    monkeypatch.setattr(
        clipboard_windows, 'read',
        lambda **kwargs: read_result('a' * 10, total=999_999, truncated=True))
    output = tool_clipboard.read()
    assert 'truncated; showing the first 200 KiB' in output
    assert output.endswith('a' * 10)


def test_read_error_is_returned_to_the_model(monkeypatch):
    def fail(**kwargs):
        raise clipboard_windows.ClipboardError('clipboard is locked')

    monkeypatch.setattr(clipboard_windows, 'read', fail)
    assert tool_clipboard.read() == 'Error: clipboard is locked'


def test_write_formats_and_audits(monkeypatch):
    events = []
    seen = {}

    def write(text, **kwargs):
        seen['text'] = text
        return clipboard_windows.ClipboardWrite(
            chars=len(text), bytes=len(text.encode()), duration_ms=4)

    monkeypatch.setattr(clipboard_windows, 'write', write)
    output = tool_clipboard.write(
        'secret text', audit=lambda event, **payload: events.append((event, payload)))

    assert seen['text'] == 'secret text'
    assert output == 'Clipboard updated: 11 chars (11 bytes).'
    assert events == [('clipboard_written', {
        'chars': 11, 'bytes': 11, 'duration_ms': 4})]
    assert 'secret text' not in repr(events)


def test_write_rejects_non_strings_without_calling_powershell(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('called')

    monkeypatch.setattr(clipboard_windows, 'write', unexpected)
    assert tool_clipboard.write(42) == 'Error: text must be a string'


def test_write_error_is_returned_to_the_model(monkeypatch):
    def fail(text, **kwargs):
        raise clipboard_windows.ClipboardError('clipboard is locked')

    monkeypatch.setattr(clipboard_windows, 'write', fail)
    assert tool_clipboard.write('x') == 'Error: clipboard is locked'


def test_audit_failures_do_not_break_the_tool(monkeypatch):
    monkeypatch.setattr(clipboard_windows, 'read',
                        lambda **kwargs: read_result('hello'))

    def broken(*args, **kwargs):
        raise RuntimeError('audit down')

    assert tool_clipboard.read(audit=broken).endswith('hello')
