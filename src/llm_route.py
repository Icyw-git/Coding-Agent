"""角色模型路由：把 llm_config 的解析结果接到 harness 的客户端 patch 点。

主循环、记忆/摘要/标题/审批等副通道共用同一套解析规则。测试一直用
``monkeypatch.setattr(harness, 'client', fake)`` 注入客户端，所以这里统一
经 ``harness.client_for`` 取客户端，而不是自己构造。
"""
import os

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
        return harness().client.chat.completions.create(**kwargs)
    kwargs['model'] = target.model
    return client_for(role, target).chat.completions.create(**kwargs)
