"""配置读取层（settings.py）。

这一层不是"把环境变量换成配置文件"，而是**收敛读取点**：默认值写一次、
非法值处理一致、将来接配置文件时调用点不用改。

关键约束：**不缓存**。`/sandbox` 换档靠写 `os.environ`、`load_policy()` 即读即用，
缓存会破坏"改完下一条调用就生效"。
"""
from __future__ import annotations

import pytest

import settings

# ---------------------------------------------------------------- flag

@pytest.mark.parametrize('value', ['1', 'true', 'TRUE', 'yes', 'on', 'enabled'])
def test_flag_true_values(monkeypatch, value):
    monkeypatch.setenv('T_FLAG', value)
    assert settings.flag('T_FLAG')


@pytest.mark.parametrize('value', ['0', 'false', 'off', 'no', 'disabled'])
def test_flag_false_values(monkeypatch, value):
    monkeypatch.setenv('T_FLAG', value)
    assert not settings.flag('T_FLAG', default=True)


def test_flag_unset_uses_default(monkeypatch):
    monkeypatch.delenv('T_FLAG', raising=False)
    assert settings.flag('T_FLAG', True)
    assert not settings.flag('T_FLAG', False)


def test_flag_garbage_falls_back_not_raises(monkeypatch):
    """配置写错不该让一个已批准的操作失败。"""
    monkeypatch.setenv('T_FLAG', 'maybe')
    assert settings.flag('T_FLAG', True)
    assert not settings.flag('T_FLAG', False)


def test_flag_empty_uses_default(monkeypatch):
    """空串等于没设——与既有语义一致。"""
    monkeypatch.setenv('T_FLAG', '   ')
    assert settings.flag('T_FLAG', True)


def test_flag_reads_live_environment(monkeypatch):
    """不缓存：改完立刻生效（/sandbox 的语义）。"""
    monkeypatch.setenv('T_FLAG', '0')
    assert not settings.flag('T_FLAG')
    monkeypatch.setenv('T_FLAG', '1')
    assert settings.flag('T_FLAG')


# ---------------------------------------------------------------- number

def test_number_basic(monkeypatch):
    monkeypatch.setenv('T_NUM', '42')
    assert settings.number('T_NUM', 10) == 42


def test_number_integer_mode(monkeypatch):
    monkeypatch.setenv('T_NUM', '42')
    assert isinstance(settings.number('T_NUM', 10, integer=True), int)


def test_number_clamps_to_minimum(monkeypatch):
    """超时写成 0 应该被收敛到最小合法值，而不是让服务起不来。"""
    monkeypatch.setenv('T_NUM', '0')
    assert settings.number('T_NUM', 300, minimum=1) == 1


def test_number_clamps_to_maximum(monkeypatch):
    monkeypatch.setenv('T_NUM', '99999')
    assert settings.number('T_NUM', 300, maximum=3600) == 3600


def test_number_garbage_uses_default(monkeypatch):
    monkeypatch.setenv('T_NUM', 'abc')
    assert settings.number('T_NUM', 300) == 300


def test_number_unset_uses_default(monkeypatch):
    monkeypatch.delenv('T_NUM', raising=False)
    assert settings.number('T_NUM', 7.5) == 7.5


def test_number_negative_clamped(monkeypatch):
    monkeypatch.setenv('T_NUM', '-5')
    assert settings.number('T_NUM', 10, minimum=0) == 0


# ---------------------------------------------------------------- text / csv

def test_text_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv('T_TEXT', raising=False)
    assert settings.text('T_TEXT', 'fallback') == 'fallback'


def test_text_strips_whitespace(monkeypatch):
    monkeypatch.setenv('T_TEXT', '  value  ')
    assert settings.text('T_TEXT') == 'value'


def test_text_empty_means_unset(monkeypatch):
    monkeypatch.setenv('T_TEXT', '')
    assert settings.text('T_TEXT', 'fallback') == 'fallback'


def test_csv_splits_and_dedupes(monkeypatch):
    monkeypatch.setenv('T_CSV', ' a, b ,,a')
    assert settings.csv('T_CSV') == ['a', 'b']


def test_csv_accepts_semicolons(monkeypatch):
    monkeypatch.setenv('T_CSV', 'a;b')
    assert settings.csv('T_CSV') == ['a', 'b']


def test_csv_empty(monkeypatch):
    monkeypatch.delenv('T_CSV', raising=False)
    assert settings.csv('T_CSV') == []


# ---------------------------------------------------------------- 安全档位

def test_policy_env_reads(monkeypatch):
    monkeypatch.setenv('HARNESS_SANDBOX_MODE', 'read-only')
    assert settings.policy_env('HARNESS_SANDBOX_MODE') == 'read-only'


def test_policy_env_rejects_non_policy_keys():
    """档位键有专门的可信来源，不能拿这个函数读任意变量。"""
    with pytest.raises(ValueError, match='not a policy key'):
        settings.policy_env('HARNESS_DEBUG')


def test_policy_keys_are_exactly_the_three():
    assert settings.POLICY_KEYS == {
        'HARNESS_SANDBOX_MODE',
        'HARNESS_APPROVAL_POLICY',
        'HARNESS_APPROVAL_ALL',
    }


def test_no_setter_for_policy():
    """刻意没有 setter：绕过 set_tier 或启动环境改档位等于提权。"""
    for name in dir(settings):
        assert 'set_policy' not in name
        assert 'set_tier' not in name


# ---------------------------------------------------------------- describe

def test_describe_lists_only_set_values(monkeypatch):
    """把 79 个默认值全铺出来很难看出哪一项被改过。"""
    monkeypatch.setenv('T_ONE', 'x')
    monkeypatch.delenv('T_TWO', raising=False)
    rows = settings.describe(['T_ONE', 'T_TWO'])
    assert rows == [{'name': 'T_ONE', 'value': 'x'}]


# ---------------------------------------------------------------- 与模块的契约

def test_runtime_toggles_agree_with_modules(monkeypatch):
    """settings.flag 与各模块自己的 enabled() 必须给出同一个答案。"""
    import memory_redact
    import never_do
    import proactive
    import taint
    import untrusted

    for value, expected in (('1', True), ('0', False)):
        monkeypatch.setenv('HARNESS_NEVER_DO', value)
        monkeypatch.setenv('HARNESS_UNTRUSTED_MARK', value)
        monkeypatch.setenv('HARNESS_TAINT_EGRESS', value)
        monkeypatch.setenv('HARNESS_REDACT_MEMORY_SECRETS', value)
        assert never_do.enabled() is expected
        assert untrusted.enabled() is expected
        assert taint.enabled() is expected
        assert memory_redact.enabled() is expected

    monkeypatch.setenv('HARNESS_PROACTIVE', '1')
    assert proactive.enabled()
    monkeypatch.setenv('HARNESS_PROACTIVE', '0')
    assert not proactive.enabled()
