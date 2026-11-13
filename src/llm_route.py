"""角色模型路由：把 llm_config 的解析结果接到 harness 的客户端 patch 点。

主循环、记忆/摘要/标题/审批等副通道共用同一套解析规则。测试一直用
``monkeypatch.setattr(harness, 'client', fake)`` 注入客户端，所以这里统一
经 ``harness.client_for`` 取客户端，而不是自己构造。
"""
import os
import time

import llm_config


def harness():
    """延迟取 harness：模块导入期避免循环，调用期读到最新全局状态。"""
    import harness
    return harness


def target_for(role: str = 'main'):
    """解析角色目标；配置缺失/非法时返回 None，由调用方退回旧环境变量。"""
    try:
        return llm_config.get_settings().resolve_for(role)
    except llm_config.ConfigError:
        return None


def model_for(role: str = 'main') -> str:
    target = target_for(role)
    if target is not None:
        return target.model
    return os.getenv('LLM_MODEL_ID') or ''


def client_for(role: str = 'main', target=None):
    """按角色取客户端；同 provider 时由 harness 复用模块级 client。"""
    h = harness()
    return h.client_for(role, target) if hasattr(h, 'client_for') else h.client


def completion(role: str = 'main', **kwargs):
    """按角色发起一次补全，返回 SDK 原始响应。

    没解析到模型时**显式报错**：直接把 ``model=None`` 发给供应商会得到 422
    "invalid type: null, expected a string"，那个错误看不出是配置没加载。
    典型原因是调用方没有先导入 ``harness``（它在导入期做 load_dotenv）。
    """
    target = target_for(role)
    if target is None:
        model = os.getenv('LLM_MODEL_ID')
        if not model:
            raise RuntimeError(
                f'no model configured for role {role!r}: llm_config could not '
                'resolve a target and LLM_MODEL_ID is empty. If this is a '
                'script, import harness first so .env is loaded.')
        kwargs.setdefault('model', model)
        client = harness().client
        model_name, provider = model, ''
    else:
        kwargs['model'] = target.model
        client = client_for(role, target)
        model_name, provider = target.model, getattr(target, 'provider', '')
    # 记录点放在这里——**唯一的 SDK 调用入口**。放到各调用方去记，主回合与副通道
    # 就会各记一遍，统计翻倍。purpose 由调用方通过 kwargs 传（默认等于 role）。
    purpose = str(kwargs.pop('_purpose', '') or role)
    started = time.perf_counter()
    try:
        reply = client.chat.completions.create(**kwargs)
    except Exception as exc:
        _record_call(role, purpose, provider, model_name, started,
                     usage=None, outcome='error', error=str(exc))
        raise
    _record_call(role, purpose, provider, model_name, started,
                 usage=getattr(reply, 'usage', None), outcome='ok')
    return reply


def _record_call(role, purpose, provider, model, started, *, usage,
                 outcome, error='') -> None:
    """写一条统一调用记录。观测是旁路：任何失败都不影响这次调用。"""
    try:
        import llm_calls

        llm_calls.record(role=role, purpose=purpose, provider=provider,
                         model=model,
                         duration_ms=(time.perf_counter() - started) * 1000,
                         usage=usage, outcome=outcome, error=error)
    except Exception:
        pass
