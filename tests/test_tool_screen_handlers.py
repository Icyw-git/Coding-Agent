"""tools 层的屏幕 handler：缓存目录、默认语言与审计事件接线。"""
import tools


class _Store:
    def __init__(self):
        self.events = []

    def append(self, event, **payload):
        self.events.append((event, payload))
        return payload


class _Manager:
    def __init__(self, store):
        self.session_store = store


def test_capture_handler_uses_harness_dir_and_audits(tmp_path, monkeypatch):
    store = _Store()
    seen = {}

    def fake_capture(screen, region, delay_ms, *, directory, runner=None, audit=None):
        seen.update(screen=screen, region=region, delay_ms=delay_ms,
                    directory=directory)
        audit('screen_captured', path='x.png', sha1='abc')
        return 'captured'

    monkeypatch.setattr(tools, '_screen_capture_tool', fake_capture)
    monkeypatch.setattr(tools, '_h', lambda: type('H', (), {
        'SCREENSHOT_DIR': tmp_path,
        'active_checkpoint_manager': staticmethod(lambda: _Manager(store)),
    }))

    assert tools.run_screen_capture('all', '1,2,3,4', 250) == 'captured'
    assert seen == {'screen': 'all', 'region': '1,2,3,4', 'delay_ms': 250,
                    'directory': tmp_path}
    assert store.events == [('screen_captured', {'path': 'x.png', 'sha1': 'abc'})]


def test_ocr_handler_passes_default_language_and_audits(tmp_path, monkeypatch):
    store = _Store()
    seen = {}

    def fake_ocr(path, lang=None, region=None, *, directory, runner=None, audit=None):
        seen.update(path=path, lang=lang, region=region, directory=directory)
        audit('screen_ocr', path='x.png', chars=2)
        return 'text'

    monkeypatch.setattr(tools, '_screen_ocr_tool', fake_ocr)
    monkeypatch.setattr(tools, '_h', lambda: type('H', (), {
        'SCREENSHOT_DIR': tmp_path,
        'active_checkpoint_manager': staticmethod(lambda: _Manager(store)),
    }))

    assert tools.run_screen_ocr() == 'text'
    assert seen['lang'] is None
    assert seen['directory'] == tmp_path
    assert store.events == [('screen_ocr', {'path': 'x.png', 'chars': 2})]


def test_audit_without_session_store_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, '_h', lambda: type('H', (), {
        'SCREENSHOT_DIR': tmp_path,
        'active_checkpoint_manager': staticmethod(lambda: None),
    }))

    tools._screen_audit('screen_captured', path='x.png')
