"""policy_denied 审计：拒绝路径必须留痕，且字段可读。

`policy_denied` 在审计白名单里登记了很久却一直没有写入方——`/audit` 能回答
「放行了什么」，却答不了「什么被挡住了」。而后者才是安全审计的主要用途。
"""
import harness
import hooks


def _session(tmp_path, session_id='denied'):
    path = harness.SESSIONS_DIR / f'session_{session_id}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    store = harness.SessionEventStore.open(path)
    store.append_message({'role': 'user', 'content': 'x'})
    return path


def _denied_events(path):
    return [event for event in harness.SessionEventStore.open(path).read_events()
            if event.get('type') == 'policy_denied']


def test_blacklisted_command_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'denied')
    path = _session(tmp_path)

    message = hooks.enforce('bash', {'command': 'shutdown /s'}, interactive=True)

    assert message and 'Dangerous command' in message
    events = _denied_events(path)
    assert len(events) == 1
    assert events[0]['tool'] == 'bash'
    assert events[0]['code'] == 'deny_pattern'
    assert events[0]['blocked_by'] == 'policy'
    assert 'shutdown' in events[0]['command']


def test_secret_path_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'denied')
    path = _session(tmp_path)

    hooks.enforce('read', {'path': '.env'}, interactive=True)

    events = _denied_events(path)
    assert events[0]['code'] == 'secret_path'
    assert events[0]['blocked_by'] == 'policy'


def test_domain_deny_is_recorded(tmp_path, monkeypatch):
    import net_grants

    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'denied')
    path = _session(tmp_path)
    net_grants.add('blocked.example.com', scope='deny')

    hooks.enforce('web_fetch', {'url': 'https://blocked.example.com/x'},
                  interactive=True)

    events = _denied_events(path)
    assert events[0]['code'] == 'domain_denied'


def test_background_screen_denial_is_marked_background(tmp_path, monkeypatch):
    """后台拒绝不是策略拒绝，而是「这条路径没法问人」——必须区分。"""
    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'denied')
    path = _session(tmp_path)

    message = hooks.enforce('screen_capture', {'screen': 'primary'},
                            interactive=False)

    assert 'requires an interactive turn' in message
    events = _denied_events(path)
    assert events[0]['blocked_by'] == 'background'
    assert events[0]['tool'] == 'screen_capture'


def test_scheduled_delete_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'denied')
    path = _session(tmp_path)

    with hooks.use_scheduled_turn():
        message = hooks.enforce('delete_paths', {'paths': ['a.txt']},
                                interactive=True)

    assert 'scheduled task is read-only' in message
    events = _denied_events(path)
    assert events[0]['blocked_by'] == 'background'
    assert 'delete_paths' in events[0]['reason']


def test_allowed_call_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'denied')
    path = _session(tmp_path)

    assert hooks.enforce('read', {'path': 'notes.md'}, interactive=True) is None
    assert _denied_events(path) == []


def test_no_session_is_silent(tmp_path, monkeypatch):
    """没有活动会话时只跳过，不能因为审计写不进去就让调用失败。"""
    monkeypatch.setattr(hooks, 'current_session_id', lambda: '')
    path = _session(tmp_path)

    assert hooks.enforce('bash', {'command': 'shutdown /s'},
                         interactive=True) is not None
    assert _denied_events(path) == []


def test_audit_surfaces_blocked_by_and_reason(tmp_path, monkeypatch):
    """审计投影要带出 blocked_by 与 reason，否则卡片只能说「有东西被拒了」。"""
    import audit

    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'denied')
    _session(tmp_path)
    hooks.enforce('screen_capture', {'screen': 'primary'}, interactive=False)

    events = audit.collect(harness.SESSIONS_DIR, types=['policy_denied'])
    assert events
    assert events[0]['blocked_by'] == 'background'
    assert 'interactive turn' in events[0]['reason']


def test_card_line_distinguishes_background_from_policy():
    import feishu_audit_card as card

    policy_line = card.event_line({
        'type': 'policy_denied', 'ts': 0, 'tool': 'bash',
        'code': 'deny_pattern', 'blocked_by': 'policy',
        'reason': 'Error: denied command shutdown'})
    background_line = card.event_line({
        'type': 'policy_denied', 'ts': 0, 'tool': 'screen_capture',
        'code': 'screen_access', 'blocked_by': 'background',
        'reason': 'Error: requires an interactive turn'})

    assert 'bash' in policy_line and 'deny_pattern' in policy_line
    assert '后台' in background_line
    assert '后台' not in policy_line
