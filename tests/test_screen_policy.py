"""屏幕工具的审批边界：后台拒绝、每次审批、禁用硬拒。"""
import pytest

import hooks


@pytest.fixture()
def config(tmp_path, monkeypatch):
    class Config:
        DENY_LIST = []
        DESTRUCTIVE = []

        @staticmethod
        def get_workdir():
            return tmp_path

        @staticmethod
        def active_checkpoint_manager():
            return None

    monkeypatch.setattr(hooks, '_h', lambda: Config)
    monkeypatch.delenv('HARNESS_SCREEN_TOOLS', raising=False)
    hooks.GATE.clear()
    yield
    hooks.GATE.clear()


def test_background_turn_denies_screen_tools(config):
    capture = hooks.enforce('screen_capture', {'screen': 'primary'},
                            interactive=False)
    ocr = hooks.enforce('screen_ocr', {}, interactive=False)

    assert 'requires an interactive turn' in capture
    assert 'screen_ocr' in ocr


def test_screen_tools_ignore_the_session_whitelist(config, monkeypatch):
    monkeypatch.setattr(hooks.GATE, 'session_allows', lambda *a, **k: True)
    asked = []

    def decide(tool, args, reason):
        asked.append(tool)
        return True, 'ok'

    monkeypatch.setattr(hooks.GATE, 'decide', decide)

    assert hooks.enforce('screen_capture', {'screen': 'primary'},
                         interactive=True) is None
    assert asked == ['screen_capture']


def test_other_approvals_still_honour_the_whitelist(config, monkeypatch):
    monkeypatch.setattr(hooks.GATE, 'session_allows', lambda *a, **k: True)
    asked = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda tool, args, reason: asked.append(tool) or (True, 'ok'))

    assert hooks.enforce('web_fetch', {'url': 'https://example.com'},
                         interactive=True) is None
    assert asked == []


def test_disabled_screen_tools_are_denied_before_asking(config, monkeypatch):
    monkeypatch.setenv('HARNESS_SCREEN_TOOLS', '0')
    asked = []
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda tool, args, reason: asked.append(tool) or (True, 'ok'))

    message = hooks.enforce('screen_capture', {'screen': 'primary'},
                            interactive=True)

    assert 'HARNESS_SCREEN_TOOLS=0' in message
    assert asked == []
