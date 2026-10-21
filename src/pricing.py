"""模型价格与成本估算：内置默认表 + `LLM_PRICING_JSON` 覆盖。

价格单位是「每 1M tokens」，默认币种 CNY。未知模型不猜价格：返回 `None`，
UI 显示为「不可用」而不是 0——把 0 当成真实花费比不显示更有害。
"""
from __future__ import annotations

import json
import os
from decimal import Decimal

DEFAULT_PRICING = {
    'deepseek-v4-flash': {
        'input': '0.50', 'output': '1.01', 'cached_input': None,
        'reasoning_output': None, 'currency': 'CNY',
    },
    'deepseek-flash': {
        'input': '0.50', 'output': '1.01', 'cached_input': None,
        'reasoning_output': None, 'currency': 'CNY',
    },
    'deepseek-chat': {
        'input': '1.01', 'output': '2.02', 'cached_input': None,
        'reasoning_output': None, 'currency': 'CNY',
    },
    'deepseek-reasoner': {
        'input': '2.02', 'output': '7.92', 'cached_input': None,
        'reasoning_output': '7.92', 'currency': 'CNY',
    },
}

_FIELDS = ('input', 'output', 'cached_input', 'reasoning_output')


def _decimal(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def load_pricing() -> dict:
    """内置表 + 环境变量覆盖；非法 JSON 静默回退到内置表。"""
    pricing = {name: dict(spec) for name, spec in DEFAULT_PRICING.items()}
    raw = os.getenv('LLM_PRICING_JSON', '').strip()
    if not raw:
        return pricing
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError:
        return pricing
    if not isinstance(configured, dict):
        return pricing
    for model, spec in configured.items():
        if not isinstance(spec, dict):
            continue
        merged = dict(pricing.get(str(model).lower(), {}))
        for key, value in spec.items():
            if key in _FIELDS:
                merged[key] = None if value is None else _decimal(value)
            else:
                merged[key] = value
        pricing[str(model).lower()] = merged
    return pricing


def estimate_cost(model: str, *, prompt: int, completion: int,
                  cached: int = 0, reasoning: int = 0,
                  pricing: dict | None = None) -> dict | None:
    """按 token 用量估算成本；未知模型返回 None。"""
    pricing = pricing if pricing is not None else load_pricing()
    spec = pricing.get((model or '').casefold())
    if not spec:
        return None
    input_rate = _decimal(spec.get('input'))
    output_rate = _decimal(spec.get('output'))
    if input_rate is None or output_rate is None:
        return None
    cached_rate = _decimal(spec.get('cached_input'))
    if cached_rate is None:
        cached_rate = input_rate
    reasoning_rate = _decimal(spec.get('reasoning_output'))
    if reasoning_rate is None:
        reasoning_rate = output_rate

    prompt = max(0, int(prompt))
    completion = max(0, int(completion))
    cached = min(max(0, int(cached)), prompt)
    reasoning = max(0, int(reasoning))
    fresh = prompt - cached
    cost = ((Decimal(fresh) * input_rate
             + Decimal(cached) * cached_rate
             + Decimal(completion) * output_rate
             + Decimal(reasoning) * reasoning_rate) / Decimal(1_000_000))
    return {
        'cost': cost.quantize(Decimal('0.0001')),
        'currency': str(spec.get('currency') or 'CNY'),
        'source': 'estimated',
    }
