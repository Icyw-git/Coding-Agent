"""跨渠道会话接续（M5）。

之前：在 TUI 里聊到一半，手机上发消息会新建会话，上下文断掉。
现在：只在「本来就要新建」时接续最近活跃会话，且**必须明说接的是哪个会话**
——静默接续比不接续更糟，用户会以为自己发错了地方。
"""
from __future__ import annotations

import json
import time

import pytest

import session_continue


def _make_session(directory, session_id, *, updated_ago=0, title='话题'):
    """造一个最小可解析的会话文件，并让它的最后活动时间落在指定时刻。"""
    path = directory / f'session_{session_id}.jsonl'
    ts = time.time() - updated_ago
    events = [
        {'seq': 1, 'ts': ts, 'type': 'message',
         'message': {'role': 'user', 'content': '你好'}},
        {'seq': 2, 'ts': ts, 'type': 'message',
         'message': {'role': 'assistant', 'content': '在'}},
        {'seq': 3, 'ts': ts, 'type': 'session_title', 'title': title},
    ]
    path.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events) + '\n',
                    encoding='utf-8')
    return path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv('HARNESS_CONTINUE_SESSION', raising=False)
    monkeypatch.delenv('HARNESS_CONTINUE_WINDOW_S', raising=False)


# ---------------------------------------------------------------- 挑选

def test_picks_most_recent(tmp_path):
    _make_session(tmp_path, 'old', updated_ago=3600, title='旧')
    _make_session(tmp_path, 'new', updated_ago=60, title='新')
    picked = session_continue.recent_session(tmp_path)
    assert picked is not None
    assert picked['id'] == 'new'


def test_ignores_sessions_outside_window(tmp_path):
    _make_session(tmp_path, 'stale', updated_ago=20 * 3600)
    assert session_continue.recent_session(tmp_path) is None


def test_window_is_configurable(tmp_path, monkeypatch):
    _make_session(tmp_path, 's', updated_ago=20 * 3600)
    monkeypatch.setenv(session_continue.ENV_WINDOW, str(30 * 3600))
    assert session_continue.recent_session(tmp_path) is not None


def test_bad_window_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv(session_continue.ENV_WINDOW, 'oops')
    assert session_continue.window_seconds() == session_continue.DEFAULT_WINDOW_S


def test_excludes_current_session(tmp_path):
    """自己接自己没意义，还会把「新会话」重定向回旧会话。"""
    _make_session(tmp_path, 'only', updated_ago=60)
    assert session_continue.recent_session(tmp_path, exclude_id='only') is None


def test_disabled_switch(tmp_path, monkeypatch):
    _make_session(tmp_path, 's', updated_ago=60)
    monkeypatch.setenv('HARNESS_CONTINUE_SESSION', '0')
    assert session_continue.recent_session(tmp_path) is None


def test_missing_directory_is_safe(tmp_path):
    assert session_continue.recent_session(tmp_path / 'nope') is None


def test_broken_file_is_skipped(tmp_path):
    """坏会话文件不能把整个接续功能带崩。"""
    (tmp_path / 'session_broken.jsonl').write_text('{not json\n', encoding='utf-8')
    _make_session(tmp_path, 'good', updated_ago=60)
    picked = session_continue.recent_session(tmp_path)
    assert picked is not None and picked['id'] == 'good'


def test_empty_directory(tmp_path):
    assert session_continue.recent_session(tmp_path) is None


# ---------------------------------------------------------------- 说明文案

def test_describe_mentions_title_and_time(tmp_path):
    _make_session(tmp_path, 's', updated_ago=3 * 3600, title='重构网关')
    picked = session_continue.recent_session(tmp_path)
    text = session_continue.describe(picked)
    assert '重构网关' in text
    assert '3 小时前' in text


@pytest.mark.parametrize('ago,expect', [
    (30, '刚刚'),
    (600, '10 分钟前'),
    (7200, '2 小时前'),
    (2 * 86400, '2 天前'),
])
def test_relative_time(ago, expect):
    now = time.time()
    assert session_continue._relative(now, now - ago) == expect


def test_relative_time_handles_zero():
    assert session_continue._relative(time.time(), 0) == '稍前'


def test_describe_without_title_falls_back():
    text = session_continue.describe({'updated': time.time()})
    assert '新会话' in text


# ---------------------------------------------------------------- 网关接入

class _FakeApi:
    def __init__(self):
        self.created = 0

    def create_session(self):
        self.created += 1
        return 'fresh'


class _FakeGateway:
    """只保留 _continue_or_create 依赖的最小接口。"""

    _continue_or_create = None

    def __init__(self):
        import threading
        self.api = _FakeApi()
        self.bindings = {}
        self.replies = []
        self._bindings_lock = threading.Lock()

    def _save_bindings(self):
        pass

    def _reply(self, chat_id, text):
        self.replies.append(text)


def _gateway_with(tmp_path, monkeypatch):
    """把 _continue_or_create 绑到假网关上，并指向临时会话目录。"""
    import llm_route
    from feishu_gateway import FeishuGateway

    gateway = _FakeGateway()
    gateway._continue_or_create = FeishuGateway._continue_or_create.__get__(gateway)
    home = llm_route.harness()
    monkeypatch.setattr(home, 'SESSIONS_DIR', tmp_path, raising=False)
    return gateway


def test_gateway_continues_recent_session(tmp_path, monkeypatch):
    _make_session(tmp_path, 'recent', updated_ago=120, title='昨天的活')
    gateway = _gateway_with(tmp_path, monkeypatch)

    session_id = gateway._continue_or_create('chat1')

    assert session_id == 'recent'
    assert gateway.api.created == 0          # 没有新建
    assert gateway.bindings['chat1'] == 'recent'
    # 必须明说接的是哪个会话
    assert any('昨天的活' in text for text in gateway.replies)


def test_gateway_creates_when_nothing_recent(tmp_path, monkeypatch):
    gateway = _gateway_with(tmp_path, monkeypatch)
    session_id = gateway._continue_or_create('chat1')
    assert session_id == 'fresh'
    assert gateway.api.created == 1


def test_gateway_creates_when_disabled(tmp_path, monkeypatch):
    _make_session(tmp_path, 'recent', updated_ago=60)
    monkeypatch.setenv('HARNESS_CONTINUE_SESSION', '0')
    gateway = _gateway_with(tmp_path, monkeypatch)
    assert gateway._continue_or_create('chat1') == 'fresh'


def test_gateway_mentions_missing_session(tmp_path, monkeypatch):
    """绑定丢失时也要说清是「原会话没了」，不能只报接续。"""
    _make_session(tmp_path, 'recent', updated_ago=60, title='T')
    gateway = _gateway_with(tmp_path, monkeypatch)
    gateway._continue_or_create('chat1', missing=True)
    assert any('原会话已不存在' in text for text in gateway.replies)
