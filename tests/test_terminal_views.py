"""终端侧共享视图 + 规则增删：CLI 与 TUI 不能各写一份。"""
import pytest

import rules
import terminal_views

# ---------- /domains ----------

def test_domains_empty_state_explains_default():
    text = terminal_views.domains('')
    assert '没有域名规则' in text
    assert '逐个审批' in text


def test_domains_add_list_remove():
    added = terminal_views.domains('allow *.example.com note docs')
    assert '已添加' in added and '*.example.com' in added

    listed = terminal_views.domains('')
    assert '允许 *.example.com' in listed

    removed = terminal_views.domains('remove *.example.com')
    assert '已删除' in removed
    assert '没有域名规则' in terminal_views.domains('')


def test_domains_deny_says_it_is_hard_blocked():
    text = terminal_views.domains('deny ads.example.com')
    assert '硬拒' in text


def test_domains_remove_missing_reports_clearly():
    assert '没有这条规则' in terminal_views.domains('remove nope.example.com')


def test_domains_rejects_bad_input():
    assert '用法' in terminal_views.domains('allow')
    assert '用法' in terminal_views.domains('maybe x.com')
    assert 'top-level' in terminal_views.domains('allow *.com')


def test_domains_usage_mentions_wildcard_semantics():
    usage = terminal_views.USAGE
    assert '不含根域本身' in usage


# ---------- /audit ----------

def test_audit_empty_state(tmp_path, monkeypatch):
    import harness

    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    monkeypatch.setattr(harness, 'SESSIONS_DIR', sessions)
    assert '没有安全相关事件' in terminal_views.audit('')


def test_audit_lists_events_with_key_fields(tmp_path, monkeypatch):
    import harness

    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    monkeypatch.setattr(harness, 'SESSIONS_DIR', sessions)
    store = harness.SessionEventStore.open(sessions / 'session_a.jsonl')
    store.append('policy_denied', tool='bash', code='deny_pattern',
                 blocked_by='policy', reason='Error: denied command shutdown')

    text = terminal_views.audit('')
    assert '策略拒绝' in text
    assert 'tool=bash' in text
    assert 'blocked_by=policy' in text
    assert 'denied command shutdown' in text


def test_audit_all_lists_filterable_types():
    text = terminal_views.audit('all')
    assert '策略拒绝' in text and 'approval_auto' in text


def test_audit_unknown_type_shows_usage(tmp_path, monkeypatch):
    import harness

    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'none')
    assert '用法' in terminal_views.audit('not-a-type')


def test_audit_type_filter(tmp_path, monkeypatch):
    import harness

    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    monkeypatch.setattr(harness, 'SESSIONS_DIR', sessions)
    store = harness.SessionEventStore.open(sessions / 'session_a.jsonl')
    store.append('policy_denied', tool='bash', code='deny_pattern')
    store.append('screen_captured', sha1='abc', bytes=1)

    text = terminal_views.audit('policy_denied')
    assert '策略拒绝' in text
    assert 'screen_captured' not in text


# ---------- 规则增删 ----------

def test_add_rule_writes_and_reloads():
    rule = rules.add_rule('allow', ['npm test'], note='只读测试')
    assert rule.decision == 'allow'
    loaded = rules.load()
    assert loaded.error == ''
    assert [(r.decision, r.prefixes) for r in loaded.rules] == [
        ('allow', ('npm test',))]
    assert loaded.rules[0].note == '只读测试'


def test_add_same_rule_replaces_instead_of_duplicating():
    rules.add_rule('allow', ['npm test'], note='一')
    rules.add_rule('allow', ['npm test'], note='二')
    loaded = rules.load()
    assert len(loaded.rules) == 1
    assert loaded.rules[0].note == '二'


def test_add_rule_accepts_a_single_string_prefix():
    rule = rules.add_rule('deny', 'npm publish')
    assert rule.prefixes == ('npm publish',)


@pytest.mark.parametrize('decision', ['maybe', '', 'ALLOWX'])
def test_add_rule_rejects_bad_decision(decision):
    with pytest.raises(rules.RuleError, match='decision'):
        rules.add_rule(decision, ['x'])


def test_add_rule_requires_prefix():
    with pytest.raises(rules.RuleError, match='prefix'):
        rules.add_rule('allow', [])


def test_add_rule_refuses_to_overwrite_a_broken_file():
    """文件坏了直接覆盖等于丢掉用户写的全部规则，必须拒绝。"""
    rules.DURABLE_PATH.write_text('rules: [', encoding='utf-8')
    with pytest.raises(rules.RuleError, match='unusable'):
        rules.add_rule('allow', ['x'])


def test_remove_rule_by_prefix():
    rules.add_rule('allow', ['npm test'])
    rules.add_rule('deny', ['npm publish'])

    removed = rules.remove_rule('npm test')
    assert [r.prefixes for r in removed] == [('npm test',)]
    assert [r.prefixes for r in rules.load().rules] == [('npm publish',)]


def test_remove_rule_can_filter_by_decision():
    rules.add_rule('allow', ['npm test'])
    rules.add_rule('deny', ['npm test'])

    removed = rules.remove_rule('npm test', decision='deny')
    assert [r.decision for r in removed] == ['deny']
    assert [r.decision for r in rules.load().rules] == ['allow']


def test_remove_missing_rule_returns_empty():
    assert rules.remove_rule('nope') == []


# ---------------------------------------------------------------- 可读性

def test_audit_uses_human_labels(tmp_path, monkeypatch):
    """原始事件名（desktop_snapshot_failed）是给代码读的，不是给人读的。"""
    import harness
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    for index in range(2):
        (tmp_path / f'session_{index}.jsonl').write_text(
            '{"type":"policy_denied","tool":"bash","reason":"x"}\n',
            encoding='utf-8')
    text = terminal_views.audit('')
    assert '策略拒绝' in text
    assert 'policy_denied' not in text


def test_audit_all_maps_raw_names_to_labels():
    """列表视图用中文，但排查时要原始标识符，所以 /audit all 给对照。"""
    text = terminal_views.audit('all')
    assert 'policy_denied' in text
    assert '策略拒绝' in text


def test_audit_shows_reason_for_any_event(tmp_path, monkeypatch):
    """desktop_snapshot_failed 的全部信息都在 reason 里。

    原来只有 policy_denied 显示 reason，于是这类记录只剩一个事件名。
    """
    import harness
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    (tmp_path / 'session_a.jsonl').write_text(
        '{"type":"desktop_snapshot_failed","reason":"no foreground window"}\n',
        encoding='utf-8')
    text = terminal_views.audit('')
    assert '桌面快照失败' in text
    assert 'no foreground window' in text


def test_audit_survives_mojibake_history(tmp_path, monkeypatch):
    """历史脏数据（解码替换符）不能让整条命令崩。

    GBK 控制台连 U+FFFD 都编不了。
    """
    import harness
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    (tmp_path / 'session_b.jsonl').write_text(
        '{"type":"desktop_snapshot_failed","reason":"\ufffd\ufffd\ufffd\ufffd\ufffd"}\n',
        encoding='utf-8')
    text = terminal_views.audit('')
    text.encode('gbk')          # 编码失败会抛异常
    assert '无法显示' in text
