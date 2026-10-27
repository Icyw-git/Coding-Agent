"""用户规则的展示入口：/status 行、/rules 输出与真实策略联动。"""
import harness
import rules


def _store(tmp_path):
    return harness.SessionEventStore(tmp_path, session_id='rules-status')


def test_status_reports_missing_rules_file(tmp_path, monkeypatch):
    store = _store(tmp_path)
    output = harness.format_status(store)
    # 没有规则时归到「未启用」那一行，不再单独占一行。
    assert '用户规则' in output.split('未启用：', 1)[1]


def test_status_reports_loaded_rule_counts(tmp_path, monkeypatch):
    rules.write(rules.parse({'version': 1, 'rules': [
        {'decision': 'allow', 'prefixes': ['npm test']},
        {'decision': 'deny', 'prefixes': ['npm publish']},
    ]}))
    output = harness.format_status(_store(tmp_path))
    # 有规则时在「已启用」里显示条数（详情见 /rules）。
    assert '用户规则：2 条' in output


def test_status_reports_broken_rules_file(tmp_path, tmp_path_factory):
    rules.DURABLE_PATH.write_text('rules: [', encoding='utf-8')
    output = harness.format_status(_store(tmp_path))
    assert '加载失败' in output


def test_kill_switch_style_rule_relaxes_destructive_ask(monkeypatch, tmp_path):
    """真实链路：destructive 会 ASK，allow 规则把它改成放行。"""
    import hooks

    rules.write(rules.parse({'version': 1, 'rules': [
        {'decision': 'allow', 'prefixes': ['npm test']},
    ]}))
    decision = hooks.evaluate_call('bash', {'command': 'npm test'})
    assert decision.action == 'allow' and decision.code == 'rule_allowed'


def test_rule_cannot_bypass_read_only_tier(monkeypatch, tmp_path):
    import hooks

    rules.write(rules.parse({'version': 1, 'rules': [
        {'decision': 'allow', 'prefixes': ['rm -rf']},
    ]}))
    monkeypatch.setenv('HARNESS_SANDBOX_MODE', 'read-only')
    decision = hooks.evaluate_call('bash', {'command': 'rm -rf build'})
    assert decision.action == 'ask'
    assert decision.code != 'rule_allowed'


def test_rule_cannot_bypass_domain_deny(monkeypatch, tmp_path):
    import hooks
    import net_grants

    net_grants.add('blocked.example.com', scope='deny')
    rules.write(rules.parse({'version': 1, 'rules': [
        {'decision': 'allow', 'prefixes': ['x'], 'tools': ['web_fetch']},
    ]}))
    decision = hooks.evaluate_call(
        'web_fetch', {'url': 'https://blocked.example.com/page'})
    assert decision.action == 'deny' and decision.code == 'domain_denied'
