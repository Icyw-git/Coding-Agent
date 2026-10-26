"""用户规则文件：前缀匹配、作用域、与硬拒的边界、损坏容错。"""
import pytest

import rules
from exec_policy import ASK, DENY, Decision, Policy

ALLOW = 'allow'


def _ruleset(*items, scope=rules.SCOPE_GLOBAL):
    parsed = rules.parse({'version': 1, 'rules': [
        {'decision': decision, 'prefixes': prefixes,
         **({'tools': tools} if tools else {}),
         **({'note': note} if note else {}),
         **({'scope': scope} if scope != rules.SCOPE_GLOBAL else {})}
        for decision, prefixes, tools, note in items
    ]})
    return rules.RuleSet(rules=parsed)


# ---------- 前缀匹配 ----------

@pytest.mark.parametrize('prefix,command,expected', [
    ('git status', 'git status', True),
    ('git status', 'git status -s', True),
    ('git status', 'git   status   -s', True),      # 多余空白折叠
    ('git status', 'git statusx', False),           # 词边界
    ('git status', 'echo git status', False),       # 不是子串匹配
    ('npm test', 'npm test -- --watch', True),
    ('ls', 'ls; rm -rf build', False),              # 多段：只看第一段不够
])
def test_prefix_matching(prefix, command, expected):
    ruleset = _ruleset((ALLOW, [prefix], None, ''))
    assert rules.explain(ruleset, 'bash', {'command': command})[0] == (
        ALLOW if expected else '')


def test_multi_segment_requires_every_segment_to_match():
    ruleset = _ruleset((ALLOW, ['ls', 'pwd'], None, ''))
    assert rules.explain(ruleset, 'bash', {'command': 'ls && pwd'})[0] == ALLOW
    assert rules.explain(ruleset, 'bash', {'command': 'ls && rm -rf x'})[0] == ''


def test_quoted_command_is_normalized():
    ruleset = _ruleset((ALLOW, ['git status'], None, ''))
    assert rules.explain(ruleset, 'bash', {'command': '"git status"'})[0] == ALLOW


# ---------- 决策语义 ----------

def test_deny_beats_allow_in_the_same_ruleset():
    ruleset = _ruleset((ALLOW, ['npm test'], None, ''),
                       ('deny', ['npm test --publish'], None, 'never publish'))
    verdict, reason = rules.explain(ruleset, 'bash',
                                    {'command': 'npm test --publish'})
    assert verdict == 'deny'
    assert reason == 'never publish'


def test_allow_only_relaxes_ask():
    ruleset = _ruleset((ALLOW, ['git status'], None, ''))
    ask = Decision(ASK, 'destructive', 'maybe destructive')
    assert rules.apply(ask, ruleset, 'bash',
                       {'command': 'git status'}).action == ALLOW
    allowed = Decision(ALLOW)
    assert rules.apply(allowed, ruleset, 'bash',
                       {'command': 'git status'}).action == ALLOW


def test_allow_never_relaxes_hard_denials():
    ruleset = _ruleset((ALLOW, ['rm -rf'], None, ''))
    for code in ('secret_path', 'deny_pattern', 'system_path', 'domain_denied',
                 'desktop_control_approval_required'):
        decision = Decision(DENY, code, 'hard deny')
        assert rules.apply(decision, ruleset, 'bash',
                           {'command': 'rm -rf x'}).action == DENY


def test_allow_never_bypasses_sandbox_tiers():
    """档位是用户的显式选择：read-only / untrusted 不能被规则改成放行。"""
    ruleset = _ruleset((ALLOW, ['rm -rf'], None, ''))
    for code in ('sandbox_read_only', 'untrusted', 'outside_workspace_write',
                 'network_egress', 'secret_egress', 'screen_access',
                 'clipboard_access', 'desktop_control'):
        decision = Decision(ASK, code, 'needs approval')
        assert rules.apply(decision, ruleset, 'bash',
                           {'command': 'rm -rf x'}).action == ASK, code


def test_ruleable_codes_are_exactly_the_noise_classes():
    assert rules.RULEABLE_CODES == {'destructive', 'opaque_command',
                                    'external_tool'}


def test_ask_rule_can_force_a_question_on_an_allowed_call():
    ruleset = _ruleset(('ask', ['git push'], None, 'push 前确认'))
    decision = Decision(ASK, 'destructive', 'maybe destructive')
    result = rules.apply(decision, ruleset, 'bash', {'command': 'git push'})
    assert result.action == ASK


def test_ask_rule_also_works_on_ruleable_codes():
    ruleset = _ruleset(('ask', ['npm test'], None, '测试前确认'))
    decision = Decision(ASK, 'destructive', 'hit npm')
    result = rules.apply(decision, ruleset, 'bash', {'command': 'npm test'})
    assert result.action == ASK and result.code == 'destructive'


def test_rules_do_not_change_already_denied_decisions():
    ruleset = _ruleset(('deny', ['git status'], None, ''))
    decision = Decision(DENY, 'some_other_code', 'already denied')
    assert rules.apply(decision, ruleset, 'bash',
                       {'command': 'git status'}).action == DENY


# ---------- 工具范围 ----------

def test_tool_scoping():
    ruleset = _ruleset((ALLOW, ['anything'], ['web_fetch'], ''))
    assert rules.explain(ruleset, 'web_fetch', {})[0] == ALLOW
    assert rules.explain(ruleset, 'bash', {'command': 'anything'})[0] == ''


def test_wildcard_tool_matches_everything():
    ruleset = _ruleset((ALLOW, ['x'], ['*'], ''))
    assert rules.explain(ruleset, 'anything', {})[0] == ALLOW


# ---------- 作用域 ----------

def test_scope_is_preserved_and_filterable():
    parsed = rules.parse({'version': 1, 'rules': [
        {'decision': ALLOW, 'prefixes': ['a'], 'scope': 'session'},
        {'decision': 'deny', 'prefixes': ['b']},
    ]})
    ruleset = rules.RuleSet(rules=parsed)
    assert [rule.prefixes for rule in ruleset.for_scope(rules.SCOPE_SESSION)] == [
        ('a',)]
    assert [rule.prefixes for rule in ruleset.for_scope(rules.SCOPE_GLOBAL)] == [
        ('b',)]


# ---------- 加载与容错 ----------

def test_missing_file_is_an_empty_ruleset(tmp_path):
    ruleset = rules.load(tmp_path / 'nope.yaml')
    assert ruleset.rules == [] and ruleset.error == ''


def test_corrupt_file_does_not_raise(tmp_path, capsys):
    path = tmp_path / 'rules.yaml'
    path.write_text('rules: [\n  - decision: allow\n', encoding='utf-8')
    ruleset = rules.load(path)
    assert ruleset.rules == []
    assert ruleset.error
    assert '忽略全部规则' in capsys.readouterr().out


def test_invalid_content_is_reported(tmp_path):
    path = tmp_path / 'rules.yaml'
    path.write_text('version: 1\nrules:\n  - decision: maybe\n'
                    '    prefixes: [x]\n', encoding='utf-8')
    ruleset = rules.load(path)
    assert ruleset.rules == [] and 'decision' in ruleset.error


@pytest.mark.parametrize('payload', [
    {'version': 2, 'rules': []},
    {'version': 1, 'rules': 'nope'},
    {'version': 1, 'rules': [{'decision': ALLOW}]},
    {'version': 1, 'rules': [{'decision': ALLOW, 'prefixes': []}]},
    {'version': 1, 'rules': [{'decision': ALLOW, 'prefixes': [''], }]},
    {'version': 1, 'rules': [{'decision': ALLOW, 'prefixes': ['x'],
                              'scope': 'galaxy'}]},
])
def test_parse_rejects_malformed(payload):
    with pytest.raises(rules.RuleError):
        rules.parse(payload)


def test_write_roundtrip(tmp_path):
    path = tmp_path / 'rules.yaml'
    original = rules.parse({'version': 1, 'rules': [
        {'decision': ALLOW, 'prefixes': ['git status'], 'note': '只读',
         'scope': 'session'},
        {'decision': 'deny', 'prefixes': ['npm publish'], 'tools': ['bash']},
    ]})
    rules.write(original, path)

    loaded = rules.load(path)
    assert loaded.error == ''
    assert [(rule.decision, rule.prefixes) for rule in loaded.rules] == [
        (ALLOW, ('git status',)), ('deny', ('npm publish',))]
    assert loaded.rules[0].note == '只读'
    assert loaded.rules[0].scope == 'session'


def test_describe_reports_counts():
    ruleset = _ruleset((ALLOW, ['a'], None, ''), ('deny', ['b'], None, ''))
    assert '允许 1' in ruleset.describe() and '拒绝 1' in ruleset.describe()
    assert rules.RuleSet().describe() == '规则文件：无'


# ---------- 与策略层组合 ----------

def test_rules_apply_on_top_of_real_policy_decisions(tmp_path, monkeypatch):
    """规则只改「问不问」：真实策略里 destructive 会 ASK，allow 规则可放行它。"""
    ruleset = _ruleset((ALLOW, ['npm test'], None, ''))
    policy = Policy(sandbox_mode='workspace-write', approval_policy='on-request')
    decision = policy.evaluate('bash', {'command': 'npm test'},
                               destructive=['npm'], deny_list=[])
    assert decision.action == ASK

    result = rules.apply(decision, ruleset, 'bash', {'command': 'npm test'})
    assert result.action == ALLOW and result.code == 'rule_allowed'


def test_rule_deny_applies_to_a_ruleable_ask():
    ruleset = _ruleset(('deny', ['npm test'], None, ''))
    decision = Decision(ASK, 'destructive', 'hit npm')
    assert rules.apply(decision, ruleset, 'bash',
                       {'command': 'npm test'}).action == DENY


def test_rule_deny_does_not_touch_unruleable_allows():
    """规则只作用于「噪音类」询问。普通放行的命令即使写了 deny 规则也不改判 ——
    用户想禁止一条平时根本不用审批的命令，应该走黑名单，而不是规则文件。"""
    ruleset = _ruleset(('deny', ['ls'], None, ''))
    decision = Decision(ALLOW)
    assert rules.apply(decision, ruleset, 'bash',
                       {'command': 'ls'}).action == ALLOW

def test_allow_rule_is_ignored_when_allow_not_permitted():
    """read-only / untrusted 档：规则里的 allow 不生效，deny 仍然生效。"""
    ruleset = _ruleset((ALLOW, ['rm -rf'], None, ''),
                       ('deny', ['shutdown'], None, ''))
    ask = Decision(ASK, 'destructive', 'maybe destructive')
    assert rules.apply(ask, ruleset, 'bash', {'command': 'rm -rf x'},
                       allow_permitted=False).action == ASK

    denied = rules.apply(ask, ruleset, 'bash', {'command': 'shutdown now'},
                         allow_permitted=False)
    assert denied.action == DENY


def test_absolute_path_invocation_matches_the_same_prefix():
    """`/usr/bin/git status` 与 `git status` 在策略层是同一件事，规则层也要一致。

    策略层判破坏性词时先做 basename 归一；规则层若不归一，用户写了 `git status`
    却仍被问一次绝对路径调用，会以为规则没生效。
    """
    ruleset = _ruleset((ALLOW, ['git status'], None, ''))
    assert rules.explain(ruleset, 'bash',
                         {'command': '/usr/bin/git status -s'})[0] == ALLOW
    assert rules.explain(ruleset, 'bash',
                         {'command': '"/usr/bin/git" status'})[0] == ALLOW


def test_absolute_path_normalisation_does_not_widen_the_rule():
    ruleset = _ruleset((ALLOW, ['git status'], None, ''))
    assert rules.explain(ruleset, 'bash',
                         {'command': '/usr/bin/git log'})[0] == ''
    assert rules.explain(ruleset, 'bash',
                         {'command': '/usr/bin/git statusx'})[0] == ''


def test_bare_command_is_not_rewritten():
    assert rules._basename_first_word('git status -s') == 'git status -s'
