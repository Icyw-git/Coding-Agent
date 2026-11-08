"""四个策略路径以前没有任何测试守着。

按「每个 `Decision(...)` 的码有没有在任何测试里出现过」扫了一遍，剩四个：

| 码 | 为什么重要 |
| --- | --- |
| `forget_memory` | 删记忆不可逆（没有回收站），影响之后每个会话 |
| `remember_memory` | 写长期记忆是长期副作用 |
| `rule_ask` | 用户规则强制「先问」，用来抵消更上层的自动放行 |
| `rule_denied` | 用户规则拒；收窄永远允许，不受档位开关影响 |

`exec_policy` 里 `forget` 的注释写着「即使 `approval_policy=never` 也不该默默删掉
用户的记忆」——这是条不变量，但之前没有断言。改错了不会报错，只会静默删数据。
"""

import pytest

import exec_policy
import rules
from exec_policy import ALLOW, ASK, DENY, Decision


@pytest.mark.parametrize('policy', ['on-request', 'untrusted', 'never'])
@pytest.mark.parametrize('tool,code,args', [
    ('forget', 'forget_memory', {'name': 'deploy'}),
    ('remember', 'remember_memory', {'text': '周三不看代码'}),
])
def test_memory_tools_are_never_silently_allowed(monkeypatch, policy, tool, code, args):
    """任何档位下都不能「不问就写/删记忆」。

    `never` 档的语义是「别问，直接拒」——于是这两个在 `never` 下变成 DENY，
    仍然满足「不默默执行」。
    """
    monkeypatch.setenv('HARNESS_APPROVAL_POLICY', policy)
    monkeypatch.setattr(exec_policy, '_POLICY_CACHE', None, raising=False)
    decision = exec_policy.load_policy().evaluate(tool, args, workspace='.')
    assert decision.action != ALLOW, f'{policy} 档下 {tool} 被静默放行了'
    assert decision.code == code


def _ruleset(*items):
    parsed = rules.parse({'version': 1, 'rules': [
        {'decision': d, 'prefixes': p} for d, p in items]})
    return rules.RuleSet(rules=parsed)


def test_ask_rule_on_an_already_asking_decision_keeps_the_original_code():
    """规则只在三个 ruleable code 上生效，而这三个**默认就已经是 ask**。

    于是 `apply()` 里 `return Decision('ask', 'rule_ask', …)` 那一支目前到不了
    （`if decision.action == 'ask': return decision` 先命中）。这里把这个现状钉住：
    如果哪天有人加一个「可被规则作用、但默认 allow」的 code，这条会红，
    那时才需要 `_REASON_ZH['rule_ask']` 和那个分支自己的测试。
    """
    ruleset = _ruleset((rules.ASK, ['npm publish']))
    base = Decision(ASK, 'destructive', 'potentially destructive command')
    result = rules.apply(base, ruleset, 'bash', {'command': 'npm publish'})
    assert result.action == ASK
    assert result.code == 'destructive'      # 原判定不被改写


def test_rule_denied_denies_even_when_would_allow():
    """deny 是收窄方向：不受「档位不许规则放宽」的开关影响。"""
    ruleset = _ruleset((rules.DENY, ['npm publish']))
    base = Decision(ASK, 'destructive', 'potentially destructive command')
    for allow_permitted in (True, False):
        result = rules.apply(base, ruleset, 'bash', {'command': 'npm publish'},
                             allow_permitted=allow_permitted)
        assert result.action == DENY
        assert result.code == 'rule_denied'
