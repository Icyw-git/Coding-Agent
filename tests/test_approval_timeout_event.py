"""审批超时：显式会话事件 + 可区分「无人应答」的拒绝语。

用户主动拒绝与审批通道超时都会导致工具被拒，但远程端需要能区分两者：一个是
明确决定，一个是网关失联/没人处理。这里钉住三条契约：

1. 超时观察者只收到一次终态为 timeout 的请求；
2. 观察者抛异常不改变拒绝结果（观察性记录不能反过来放行）；
3. enforce 在超时时返回带原因的固定前缀，且事件落进当前会话的事件流。
"""
import approval
import harness
import hooks
from session_store import SessionEventStore


def test_gate_timeout_calls_observer_once():
    seen = []
    gate = approval.PermissionGate(timeout=0.05, on_timeout=seen.append)
    gate.backend = 'remote'

    allowed, message = gate.decide_queued('bash', {'command': 'rm build'}, 'destructive')

    assert allowed is False
    assert message.startswith('timeout ')
    assert len(seen) == 1
    req = seen[0]
    assert req.status == 'timeout' and req.resolved_by == 'gate'
    assert req.tool == 'bash'


def test_observer_failure_does_not_change_denial(capsys):
    def boom(_req):
        raise RuntimeError('observer down')

    gate = approval.PermissionGate(timeout=0.05, on_timeout=boom)
    gate.backend = 'remote'

    allowed, message = gate.decide_queued('bash', {'command': 'rm build'}, 'destructive')

    assert allowed is False and message.startswith('timeout ')
    assert 'observer down' in capsys.readouterr().err


def test_enforce_timeout_returns_reason_and_writes_session_event(monkeypatch):
    session_id = 'timeout-session'
    store = SessionEventStore(harness.SESSIONS_DIR, session_id=session_id)
    store.append('session_created')

    monkeypatch.setattr(hooks.GATE, 'backend', 'remote')
    monkeypatch.setattr(hooks.GATE, 'timeout', 0.2)
    hooks.GATE.clear()

    with hooks.use_session(session_id):
        result = hooks.enforce_headless('bash', {'command': 'rm build'})

    # 拒绝语保留固定前缀（tool_activity 等消费者按前缀判失败），并说明原因
    assert result.startswith('Permission denied: approval timed out')
    events = [event for event in store.read_events()
              if event['type'] == 'approval_timeout']
    assert len(events) == 1
    event = events[0]
    assert event['tool'] == 'bash'
    assert event['session_id'] == session_id
    assert event['request_id'].startswith('perm_')
    assert event['timeout_s'] == 0.2
    assert event['source'] == ''   # 用例不在回合里，没有 turn token


def test_timeout_event_is_skipped_without_a_session_file(monkeypatch):
    """会话文件不存在（已归档/假 harness）时静默跳过，不新建孤儿会话。"""
    monkeypatch.setattr(hooks.GATE, 'backend', 'remote')
    monkeypatch.setattr(hooks.GATE, 'timeout', 0.05)
    hooks.GATE.clear()

    with hooks.use_session('missing-session'):
        result = hooks.enforce_headless('bash', {'command': 'rm build'})

    assert result.startswith('Permission denied: approval timed out')
    assert not (harness.SESSIONS_DIR / 'session_missing-session.jsonl').exists()
