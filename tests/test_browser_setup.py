"""浏览器 MCP 配置助手：片段生成与日常 profile 检查。"""
import json
import sys
from pathlib import Path

import pytest

import browser_profile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import browser_setup  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    profile = tmp_path / 'browser-profile'
    servers = tmp_path / 'mcp_servers.json'
    monkeypatch.setattr(browser_profile, 'profile_dir', lambda: profile)
    monkeypatch.setenv('MCP_SERVERS_FILE', str(servers))
    return profile, servers


def test_isolated_snippet_by_default(env):
    cfg = browser_setup.snippet(keep_login=False)
    assert cfg['name'] == 'playwright'
    assert '--isolated' in cfg['args']
    assert not any('user-data-dir' in arg for arg in cfg['args'])


def test_keep_login_uses_dedicated_profile(env):
    profile, _ = env
    cfg = browser_setup.snippet(keep_login=True)
    assert '--isolated' not in cfg['args']
    assert f'--user-data-dir={profile}' in cfg['args']


def test_snippet_cli_creates_profile_when_keeping_login(env, capsys):
    profile, _ = env
    assert browser_setup.main(['snippet', '--keep-login']) == 0
    payload = json.loads(capsys.readouterr().out.split('\n\n')[0])
    assert payload[0]['name'] == 'playwright'
    assert profile.is_dir()


def test_check_reports_missing_config(env, capsys):
    _, servers = env
    assert browser_setup.main(['check']) == 0
    assert '配置文件不存在' in capsys.readouterr().out
    assert not servers.exists()


def test_check_flags_missing_profile_flags(env, capsys):
    _, servers = env
    servers.write_text(json.dumps([
        {'name': 'playwright', 'command': 'npx', 'args': ['-y', '@playwright/mcp']}
    ]), encoding='utf-8')

    assert browser_setup.main(['check']) == 0
    output = capsys.readouterr().out
    assert 'README' not in output
    assert '既没有 --isolated 也没有 --user-data-dir' in output


def test_check_reports_isolated_mode(env, capsys):
    _, servers = env
    servers.write_text(json.dumps([
        {'name': 'playwright', 'command': 'npx', 'args': ['--isolated']}
    ]), encoding='utf-8')

    browser_setup.main(['check'])
    assert '不保留登录态' in capsys.readouterr().out


def test_check_reports_login_profile(env, capsys):
    profile, servers = env
    servers.write_text(json.dumps([
        {'name': 'playwright', 'command': 'npx',
         'args': [f'--user-data-dir={profile}']}
    ]), encoding='utf-8')

    browser_setup.main(['check'])
    assert '登录态会被复用' in capsys.readouterr().out


def test_check_rejects_broken_config(env, capsys):
    _, servers = env
    servers.write_text('{not json', encoding='utf-8')
    assert browser_setup.main(['check']) == 2
    assert '配置读不了' in capsys.readouterr().err


def test_check_rejects_non_list(env, capsys):
    _, servers = env
    servers.write_text('{"name": "x"}', encoding='utf-8')
    assert browser_setup.main(['check']) == 2
    assert '必须是 JSON 数组' in capsys.readouterr().err


def test_check_warns_on_daily_profile(env, capsys, tmp_path, monkeypatch):
    daily = tmp_path / 'Google' / 'Chrome' / 'User Data'
    monkeypatch.setattr(browser_profile, 'profile_dir', lambda: daily)
    _, servers = env
    servers.write_text(json.dumps([
        {'name': 'playwright', 'command': 'npx',
         'args': [f'--user-data-dir={daily}']}
    ]), encoding='utf-8')

    browser_setup.main(['check'])
    assert '⚠' in capsys.readouterr().out


def test_marker_detection_covers_command_and_args():
    """server 叫 `pw` 也要认识：真实配置里名字常是缩写。"""
    assert browser_setup._looks_like_browser(
        {'name': 'pw', 'command': 'npx', 'args': ['@playwright/mcp']})
    assert browser_setup._looks_like_browser(
        {'name': 'web', 'command': 'puppeteer-mcp', 'args': []})
    assert not browser_setup._looks_like_browser(
        {'name': 'fs', 'command': 'npx', 'args': ['mcp-fs']})
