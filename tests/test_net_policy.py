"""域名策略：匹配语义、账本 fail-closed、策略层三态。"""
from datetime import date, timedelta

import pytest

import net_grants
import net_policy
from exec_policy import ALLOW, ASK, DENY, Policy

# ---------- 匹配语义 ----------

@pytest.mark.parametrize('pattern,host,expected', [
    ('api.example.com', 'api.example.com', True),
    ('api.example.com', 'API.Example.COM.', True),
    ('api.example.com', 'x.api.example.com', False),
    ('*.example.com', 'a.example.com', True),
    ('*.example.com', 'a.b.example.com', True),
    ('*.example.com', 'example.com', False),      # 根域要单独写
    ('*.example.com', 'notexample.com', False),   # 后缀必须按点边界
    ('*.example.com', '', False),
])
def test_matches(pattern, host, expected):
    assert net_policy.matches(pattern, net_policy.normalize_host(host)) is expected


def test_deny_wins_over_allow():
    rules = (('a.example.com', 'allow'), ('a.example.com', 'deny'))
    assert net_policy.classify('a.example.com', rules) == 'deny'
    assert net_policy.classify('b.example.com', rules) == ''


@pytest.mark.parametrize('raw', ['', '*', 'http://x.com', 'x.com/path', 'x.com:80',
                                 'a.*.com', 'x y.com', '-bad.com'])
def test_rejects_unsafe_patterns(raw):
    with pytest.raises(net_policy.NetPolicyError):
        net_policy.normalize_pattern(raw)


def test_non_ascii_hosts_never_match():
    assert net_policy.normalize_host('例子.com') == ''
    assert net_policy.classify('例子.com', (('*.com', 'allow'),)) == ''


# ---------- 账本 ----------

def test_add_list_remove_and_expiry(tmp_path):
    net_grants.add('*.example.com', note='docs')
    net_grants.add('blocked.example.com', scope='deny')

    rows = net_grants.list_rules()
    assert {row['host'] for row in rows} == {'*.example.com', 'blocked.example.com'}
    assert all(row['active'] for row in rows)
    assert net_grants.counts() == (1, 1)

    net_grants.add('*.example.com', note='updated')
    assert len(net_grants.list_rules()) == 2      # 同模式是更新，不是追加

    removed = net_grants.remove('*.example.com')
    assert removed['host'] == '*.example.com'
    assert net_grants.remove('*.example.com') is None


def test_expired_rules_are_ignored_by_policy():
    net_grants.add('old.example.com', expires=(date.today() + timedelta(days=1)).isoformat())
    assert net_grants.active_rules() == (('old.example.com', 'allow'),)
    future = date.today() + timedelta(days=5)
    assert net_grants.active_rules(today=future) == ()
    assert net_grants.counts(today=future) == (0, 0)


def test_corrupt_ledger_fails_closed(tmp_path, monkeypatch):
    net_grants.DURABLE_PATH.write_text('{not json', encoding='utf-8')
    assert net_grants.active_rules() == ()
    assert net_grants.list_rules() == []
    with pytest.raises(net_grants.NetGrantError):
        net_grants.add('x.example.com')


def test_unknown_version_fails_closed():
    net_grants.DURABLE_PATH.write_text(
        '{"version": 99, "domains": [{"host": "x.com", "scope": "allow"}]}',
        encoding='utf-8')
    assert net_grants.active_rules() == ()


def test_malformed_records_are_skipped_not_fatal():
    net_grants.DURABLE_PATH.write_text(
        '{"version": 1, "domains": [{"host": "*", "scope": "allow"},'
        ' {"host": "ok.example.com", "scope": "allow"},'
        ' {"host": "x.com", "scope": "maybe"},'
        ' {"host": "y.com", "scope": "allow", "expires": "not-a-date"}]}',
        encoding='utf-8')
    assert net_grants.active_rules() == (('ok.example.com', 'allow'),)


def test_invalid_inputs_rejected():
    with pytest.raises(net_policy.NetPolicyError):
        net_grants.add('*')
    with pytest.raises(net_policy.NetPolicyError):
        net_grants.add('http://x.com/path')
    with pytest.raises(net_grants.NetGrantError):
        net_grants.add('x.com', scope='maybe')
    with pytest.raises(net_grants.NetGrantError):
        net_grants.add('x.com', expires='2000-01-01')


def test_session_revoke_only_removes_own_rules():
    net_grants.add('a.example.com', session_id='s1')
    net_grants.add('b.example.com', session_id='s2')
    net_grants.add('c.example.com')

    removed = net_grants.revoke_for_session('s1')
    assert [row['host'] for row in removed] == ['a.example.com']
    assert {row['host'] for row in net_grants.list_rules()} == {
        'b.example.com', 'c.example.com'}


# ---------- 策略层三态 ----------

def _web(url, rules):
    return Policy().evaluate('web_fetch', {'url': url}, net_rules=rules)


def test_policy_asks_for_unknown_domains():
    decision = _web('https://unknown.example.com/a', ())
    assert decision.action == ASK and decision.code == 'network_egress'


def test_policy_allows_listed_domains():
    decision = _web('https://docs.example.com/a', (('*.example.com', 'allow'),))
    assert decision.action == ALLOW and decision.code == 'domain_allowed'


def test_policy_denies_blocked_domains_even_in_full_access():
    rules = (('ads.example.com', 'deny'),)
    for mode in ('read-only', 'workspace-write', 'danger-full-access'):
        decision = Policy(sandbox_mode=mode, approval_policy='never').evaluate(
            'web_fetch', {'url': 'https://ads.example.com/x'}, net_rules=rules)
        assert decision.action == DENY and decision.code == 'domain_denied'


def test_deny_beats_allow_in_policy():
    rules = (('*.example.com', 'allow'), ('secret.example.com', 'deny'))
    assert _web('https://secret.example.com/', rules).action == DENY


def test_invalid_url_is_denied_before_rules():
    decision = _web('ftp://example.com/x', (('*.example.com', 'allow'),))
    assert decision.action == DENY and decision.code == 'invalid_url'


def test_allow_does_not_bypass_private_address_check():
    """策略层放行的是「要不要问」；私网拦截在网络层，与名单无关。"""
    decision = _web('http://127.0.0.1:8000/admin', (('127.0.0.1', 'allow'),))
    assert decision.action == ASK or decision.code == 'domain_allowed'
    # 真正挡住它的地方：审批前的 check_url，名单不参与
    from web_access import WebAccessError, check_url
    with pytest.raises(WebAccessError):
        check_url('http://127.0.0.1:8000/admin')
