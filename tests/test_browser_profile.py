"""浏览器 profile：独立目录、权限收紧、日常 profile 检测。"""
import os
from pathlib import Path

import pytest

import browser_profile


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    target = tmp_path / 'browser-profile'
    monkeypatch.delenv(browser_profile.ENV, raising=False)
    monkeypatch.setattr(browser_profile, 'profile_dir', lambda: target)
    return target


def test_default_path_lives_under_cache(monkeypatch):
    monkeypatch.delenv(browser_profile.ENV, raising=False)
    import paths

    assert browser_profile.profile_dir() == paths.cache_path(
        browser_profile.DIRNAME)


def test_env_override_wins(monkeypatch, tmp_path):
    custom = tmp_path / 'custom-profile'
    monkeypatch.setenv(browser_profile.ENV, str(custom))
    assert browser_profile.profile_dir() == custom.resolve()


def test_ensure_creates_directory(profile):
    assert not profile.exists()
    created = browser_profile.ensure()
    assert created == profile and profile.is_dir()


def test_ensure_is_idempotent(profile):
    browser_profile.ensure()
    assert browser_profile.ensure() == profile


def test_ensure_reports_file_at_path(profile):
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text('not a dir', encoding='utf-8')
    with pytest.raises(browser_profile.ProfileError, match='not a directory'):
        browser_profile.ensure()


def test_ensure_without_create_does_not_mkdir(profile):
    assert browser_profile.ensure(create=False) == profile
    assert not profile.exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX 权限位在 Windows 上不适用')
def test_posix_permissions_are_tightened(profile):
    browser_profile.ensure()
    mode = profile.stat().st_mode & 0o777
    assert mode == 0o700


def test_detects_daily_chrome_profile(profile):
    chrome = Path.home() / 'AppData' / 'Local' / 'Google' / 'Chrome' / 'User Data'
    assert browser_profile.is_inside_daily_profile(chrome)
    assert browser_profile.is_inside_daily_profile(
        '/home/me/.mozilla/firefox/abc.default')
    assert not browser_profile.is_inside_daily_profile(profile)


def test_describe_warns_when_pointing_at_daily_profile(monkeypatch):
    monkeypatch.setenv(browser_profile.ENV,
                       str(Path('/x/Google/Chrome/User Data')))
    text = browser_profile.describe()
    assert '⚠' in text and '日常浏览器' in text


def test_describe_reports_missing_then_existing(profile):
    assert '未创建' in browser_profile.describe()
    browser_profile.ensure()
    assert '未创建' not in browser_profile.describe()


def test_status_reports_override(monkeypatch, tmp_path):
    monkeypatch.setenv(browser_profile.ENV, str(tmp_path / 'p'))
    state = browser_profile.status()
    assert state['overridden'] is True
    assert state['exists'] is False


def test_suggest_args_points_at_profile(profile):
    assert browser_profile.suggest_args() == [f'--user-data-dir={profile}']
