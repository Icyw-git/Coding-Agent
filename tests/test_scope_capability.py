"""审批作用域 N7：批准一次不等于永久放开。

之前白名单是一个 set：批准一次 ``rm``，这个会话里所有同类 ``rm`` 永久免审。
Muse 的审批是**能力（capability）而不是建议**，绑定到具体目标与用例，且支持
one-time / session / task / time-bounded / perpetual。

四个作用域：once（默认）/ times / until / session。
"""
from __future__ import annotations

import time

import approval_scope

ONCE = approval_scope.ONCE
TIMES = approval_scope.TIMES
UNTIL = approval_scope.UNTIL
SESSION = approval_scope.SESSION


# ---------------------------------------------------------------- 默认值

def test_default_scope_is_once(monkeypatch):
    """最重要的一个改动：默认从 session 变 once。

    用户点"同意"时心里想的是**眼前这一条**，不是"以后同类都免了"。
    """
    monkeypatch.delenv(approval_scope.ENV_SCOPE, raising=False)
    assert approval_scope.default_scope() == ONCE


def test_default_scope_can_be_restored(monkeypatch):
    monkeypatch.setenv(approval_scope.ENV_SCOPE, 'session')
    assert approval_scope.default_scope() == SESSION


def test_bad_default_scope_falls_back(monkeypatch):
    monkeypatch.setenv(approval_scope.ENV_SCOPE, 'whatever')
    assert approval_scope.default_scope() == ONCE


# ---------------------------------------------------------------- 构造

def test_make_once():
    record = approval_scope.make(ONCE)
    assert record['scope'] == ONCE
    assert record['remaining'] == 0


def test_make_times_clamps_to_at_least_one():
    assert approval_scope.make(TIMES, times=0)['remaining'] == 1
    assert approval_scope.make(TIMES, times=-5)['remaining'] == 1
    assert approval_scope.make(TIMES, times=4)['remaining'] == 4


def test_make_until_caps_at_max():
    """会话档与到期档都有硬上限：不让"本会话"变成无限期。"""
    now = 1000.0
    record = approval_scope.make(UNTIL, until=now + 999 * 3600, now=now)
    assert record['expires_at'] <= now + approval_scope.MAX_SESSION_S


def test_make_unknown_scope_falls_back():
    assert approval_scope.make('nonsense')['scope'] == ONCE


# ---------------------------------------------------------------- 活性判定

def test_once_is_active_until_consumed():
    record = approval_scope.make(ONCE)
    assert approval_scope.is_active(record)
    assert approval_scope.consume(record) is None   # 用一次就没了


def test_times_counts_down():
    record = approval_scope.make(TIMES, times=2)
    assert approval_scope.is_active(record)
    record = approval_scope.consume(record)
    assert record['remaining'] == 1
    record = approval_scope.consume(record)
    assert record is None


def test_until_expires():
    now = 1000.0
    record = approval_scope.make(UNTIL, until=now + 60, now=now)
    assert approval_scope.is_active(record, now=now + 30)
    assert not approval_scope.is_active(record, now=now + 61)


def test_session_has_a_hard_cap():
    """「本会话」也不该无限期：12 小时之后要重新确认。"""
    now = 1000.0
    record = approval_scope.make(SESSION, now=now)
    assert approval_scope.is_active(record, now=now + 3600)
    assert not approval_scope.is_active(
        record, now=now + approval_scope.MAX_SESSION_S + 1)


def test_session_does_not_consume():
    record = approval_scope.make(SESSION)
    assert approval_scope.consume(record) is record


def test_is_active_rejects_garbage():
    assert not approval_scope.is_active(None)
    assert not approval_scope.is_active({})
    assert not approval_scope.is_active({'scope': 'bogus'})


# ---------------------------------------------------------------- 展示

def test_describe_each_scope():
    assert approval_scope.describe(ONCE) == '仅这一次'
    assert '2 次' in approval_scope.describe(TIMES, times=2)
    assert '本会话' in approval_scope.describe(SESSION)


def test_describe_until_reports_minutes():
    now = time.time()
    text = approval_scope.describe(UNTIL, until=now + 300)
    assert '分钟' in text


# ---------------------------------------------------------------- 接入 PermissionGate

def _fresh_gate():
    import approval
    gate = approval.PermissionGate()
    gate.clear()
    return gate


def _approve(gate, command, **kwargs):
    req = gate.request('bash', {'command': command}, 'test')
    gate.resolve(req.req_id, True, by='test', **kwargs)
    return req


def test_once_leaves_no_allowlist():
    """默认作用域批准后，同类操作仍要再问一次。"""
    gate = _fresh_gate()
    _approve(gate, 'rm x')
    assert not gate.session_allows('bash', {'command': 'rm y'})


def test_times_allowlist_counts_down():
    gate = _fresh_gate()
    _approve(gate, 'rm x', scope=TIMES, times=2)
    assert gate.session_allows('bash', {'command': 'rm y'})
    assert gate.session_allows('bash', {'command': 'rm z'})
    assert not gate.session_allows('bash', {'command': 'rm w'})


def test_until_allowlist_expires():
    gate = _fresh_gate()
    _approve(gate, 'rm x', scope=UNTIL, until=time.time() + 0.2)
    assert gate.session_allows('bash', {'command': 'rm y'})
    time.sleep(0.25)
    assert not gate.session_allows('bash', {'command': 'rm y'})


def test_session_allowlist_keeps_old_behaviour():
    gate = _fresh_gate()
    _approve(gate, 'rm x', scope=SESSION)
    for _ in range(5):
        assert gate.session_allows('bash', {'command': 'rm y'})


def test_revoke_clears_scoped_grants():
    """撤销必须能收回所有作用域的授权，不只是 session 档。"""
    gate = _fresh_gate()
    for command, kwargs in (('rm a', {'scope': TIMES, 'times': 3}),
                            ('rm b', {'scope': SESSION}),
                            ('rm c', {'scope': UNTIL,
                                      'until': time.time() + 600})):
        _approve(gate, command, **kwargs)
    gate.revoke_session()
    assert not gate.session_allows('bash', {'command': 'rm x'})


def test_denied_request_leaves_no_allowlist():
    gate = _fresh_gate()
    req = gate.request('bash', {'command': 'rm x'}, 'test')
    gate.resolve(req.req_id, False, by='test', scope=SESSION)
    assert not gate.session_allows('bash', {'command': 'rm y'})


def test_different_class_still_requires_approval():
    """作用域只放宽"同类"，不会放宽到别的命令。"""
    gate = _fresh_gate()
    _approve(gate, 'rm x', scope=SESSION)
    assert gate.session_allows('bash', {'command': 'rm y'})
    assert not gate.session_allows('bash', {'command': 'sudo rm y'})
