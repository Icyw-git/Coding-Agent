"""自动放行的审计：审查官与规则两条路径都要留痕。"""
import harness
import hooks
import rules


def _store(tmp_path, session_id='auto-audit'):
    path = harness.SESSIONS_DIR / f'session_{session_id}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    store = harness.SessionEventStore.open(path)
    store.append_message({'role': 'user', 'content': 'hi'})
    return path


def _events(path, kind='approval_auto'):
    return [event for event in harness.SessionEventStore.open(path).read_events()
            if event.get('type') == kind]


def test_rule_approval_writes_audit_event(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'auto-audit')
    path = _store(tmp_path)
    rules.write(rules.parse({'version': 1, 'rules': [
        {'decision': 'allow', 'prefixes': ['npm test']},
    ]}))

    assert hooks.enforce('bash', {'command': 'npm test build'},
                         interactive=True) is None

    events = _events(path)
    assert len(events) == 1
    event = events[0]
    assert event['by'] == 'rule'
    assert event['code'] == 'rule_allowed'
    assert event['tool'] == 'bash'
    assert event['command'] == 'npm test build'


def test_rule_audit_records_no_secret_payload(tmp_path, monkeypatch):
    """审计只记命令摘要与参数键名，不复制整份参数。"""
    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'auto-audit')
    path = _store(tmp_path)
    rules.write(rules.parse({'version': 1, 'rules': [
        {'decision': 'allow', 'prefixes': ['npm test']},
    ]}))

    hooks.enforce('bash', {'command': 'npm test build', 'cwd': 'D:/tmp'},
                  interactive=True)

    event = _events(path)[0]
    assert event['args_keys'] == ['command', 'cwd']
    assert 'cwd' not in event


def test_audit_failure_does_not_break_the_tool(tmp_path, monkeypatch):
    """审计写入抛错不能让一次已经放行的调用失败。"""
    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'auto-audit')

    class BrokenStore:
        @staticmethod
        def open(path):
            raise OSError('disk full')

    class FakeHarness:
        DENY_LIST = []
        DESTRUCTIVE = []
        SESSIONS_DIR = tmp_path / 'nowhere'
        SessionEventStore = BrokenStore

        @staticmethod
        def get_workdir():
            return tmp_path

        @staticmethod
        def active_checkpoint_manager():
            return None

    monkeypatch.setattr(hooks, '_h', lambda: FakeHarness)
    (tmp_path / 'nowhere').mkdir()
    (tmp_path / 'nowhere' / 'session_auto-audit.jsonl').write_text('',
                                                                   encoding='utf-8')
    rules.write(rules.parse({'version': 1, 'rules': [
        {'decision': 'allow', 'prefixes': ['npm test']},
    ]}))
    assert hooks.enforce('bash', {'command': 'npm test'},
                         interactive=True) is None


def test_no_audit_without_active_session(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, 'current_session_id', lambda: '')
    path = _store(tmp_path)
    rules.write(rules.parse({'version': 1, 'rules': [
        {'decision': 'allow', 'prefixes': ['npm test']},
    ]}))
    hooks.enforce('bash', {'command': 'npm test'}, interactive=True)
    assert _events(path) == []


def test_normal_allow_does_not_write_audit(tmp_path, monkeypatch):
    """只有「自动放行」留痕：普通放行的命令不写 approval_auto。"""
    monkeypatch.setattr(hooks, 'current_session_id', lambda: 'auto-audit')
    path = _store(tmp_path)
    hooks.enforce('read', {'path': 'notes.md'}, interactive=True)
    assert _events(path) == []


def test_approval_auto_is_queryable(tmp_path):
    import audit

    _store(tmp_path)
    harness.SessionEventStore.open(
        harness.SESSIONS_DIR / 'session_auto-audit.jsonl').append(
            'approval_auto', tool='bash', by='rule', code='rule_allowed',
            reason='allowed by rule', command='npm test', session_id='auto-audit')

    events = audit.collect(harness.SESSIONS_DIR, types=['approval_auto'])
    assert events and events[0]['by'] == 'rule'
    assert events[0]['tool'] == 'bash'
