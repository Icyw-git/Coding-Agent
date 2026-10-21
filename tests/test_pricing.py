"""pricing：默认价格表、缓存价、环境变量覆盖与未知模型。"""
from decimal import Decimal

import pricing


def test_default_pricing_estimates_cost_per_million_tokens():
    result = pricing.estimate_cost('deepseek-v4-flash',
                                   prompt=1_000_000, completion=1_000_000)

    assert result['cost'] == Decimal('1.5100')   # 0.50 + 1.01
    assert result['currency'] == 'CNY'
    assert result['source'] == 'estimated'


def test_cached_tokens_fall_back_to_input_rate():
    result = pricing.estimate_cost('deepseek-v4-flash', prompt=1_000_000,
                                   completion=0, cached=500_000)

    # 无 cached_input 价时缓存部分按 input 价计：500k*0.5 + 500k*0.5
    assert result['cost'] == Decimal('0.5000')


def test_environment_override_adds_model(monkeypatch):
    monkeypatch.setenv('LLM_PRICING_JSON', '{"my-model": '
                                            '{"input": "2", "output": "4", '
                                            '"currency": "USD"}}')

    result = pricing.estimate_cost('my-model', prompt=1_000_000, completion=0)

    assert result['cost'] == Decimal('2.0000')
    assert result['currency'] == 'USD'


def test_unknown_model_is_unavailable():
    assert pricing.estimate_cost('unknown-model', prompt=10, completion=10) is None


def test_invalid_override_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv('LLM_PRICING_JSON', 'not json')

    result = pricing.estimate_cost('deepseek-chat', prompt=1_000_000, completion=0)

    assert result['cost'] == Decimal('1.0100')


def test_reasoning_is_not_double_charged(monkeypatch):
    monkeypatch.setenv('LLM_PRICING_JSON',
                       '{"r-model":{"input":"1","output":"2",'
                       '"reasoning_output":"2","currency":"USD"}}')
    result = pricing.estimate_cost('r-model', prompt=0, completion=1_000_000,
                                   reasoning=500_000)
    assert result['cost'] == Decimal('2.0000')   # 500k*2 + 500k*2


def test_reasoning_can_have_premium_rate(monkeypatch):
    monkeypatch.setenv('LLM_PRICING_JSON',
                       '{"r-model":{"input":"1","output":"2",'
                       '"reasoning_output":"3","currency":"USD"}}')
    result = pricing.estimate_cost('r-model', prompt=0, completion=1_000_000,
                                   reasoning=500_000)
    assert result['cost'] == Decimal('2.5000')   # 500k*2 + 500k*3


def test_cache_write_uses_configured_rate(monkeypatch):
    monkeypatch.setenv('LLM_PRICING_JSON',
                       '{"w-model":{"input":"1","output":"2",'
                       '"cache_write":"1.25","currency":"USD"}}')
    result = pricing.estimate_cost('w-model', prompt=1_000_000, completion=0,
                                   cached=0, cache_write=1_000_000)
    assert result['cost'] == Decimal('1.2500')
