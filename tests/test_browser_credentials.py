"""浏览器凭据保护：cookies / login DB 不得被文件工具读走。"""
import pytest

import checkpoints
import hooks
from exec_policy import PATH_SECRET, classify_path, is_secret_path


@pytest.mark.parametrize('name', [
    'Cookies', 'Cookies-journal', 'Login Data', 'Login Data-journal',
    'Web Data', 'Local State', 'cookies.sqlite', 'key4.db', 'logins.json',
])
def test_browser_credential_files_are_secrets(name):
    assert is_secret_path(name, checkpoints.DEFAULT_SECRETS)


@pytest.mark.parametrize('name', [
    'History', 'Bookmarks', 'Preferences', 'Network Persistent State',
    'Current Session', 'manifest.json',
])
def test_ordinary_browser_files_are_not_secrets(name):
    """只挡凭据库：把整个 profile 都当成秘密会让正常排障无从下手。"""
    assert not is_secret_path(name, checkpoints.DEFAULT_SECRETS)


def test_case_insensitive_like_windows(tmp_path):
    path = tmp_path / 'cookies'
    path.write_text('x', encoding='utf-8')
    assert is_secret_path(str(path), checkpoints.DEFAULT_SECRETS)
    path2 = tmp_path / 'LOGIN DATA'
    path2.write_text('x', encoding='utf-8')
    assert is_secret_path(str(path2), checkpoints.DEFAULT_SECRETS)


def test_classify_path_marks_browser_db_as_secret(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    chrome = tmp_path / 'Chrome' / 'User Data' / 'Default'
    chrome.mkdir(parents=True)
    cookie = chrome / 'Cookies'
    cookie.write_text('sqlite', encoding='utf-8')

    zone = classify_path(str(cookie), workspace=workspace,
                         secrets=checkpoints.DEFAULT_SECRETS)
    assert zone == PATH_SECRET


def test_read_tool_denies_browser_cookies(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    cookie = tmp_path / 'Chrome' / 'Cookies'
    cookie.parent.mkdir(parents=True)
    cookie.write_text('sqlite', encoding='utf-8')

    class FakeHarness:
        DENY_LIST = []
        DESTRUCTIVE = []
        SESSIONS_DIR = tmp_path / 'sessions'
        SessionEventStore = None

        @staticmethod
        def get_workdir():
            return workspace

        @staticmethod
        def active_checkpoint_manager():
            return None

    monkeypatch.setattr(hooks, '_h', lambda: FakeHarness)
    message = hooks.enforce('read', {'path': str(cookie)}, interactive=True)
    assert message and 'protected path' in message


def test_grep_denies_browser_cookies(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    cookie = tmp_path / 'Edge' / 'Cookies'
    cookie.parent.mkdir(parents=True)
    cookie.write_text('sqlite', encoding='utf-8')

    class FakeHarness:
        DENY_LIST = []
        DESTRUCTIVE = []
        SESSIONS_DIR = tmp_path / 'sessions'
        SessionEventStore = None

        @staticmethod
        def get_workdir():
            return workspace

        @staticmethod
        def active_checkpoint_manager():
            return None

    monkeypatch.setattr(hooks, '_h', lambda: FakeHarness)
    message = hooks.enforce('grep', {'pattern': 'session', 'path': str(cookie)},
                            interactive=True)
    assert message and 'protected path' in message
