"""LLM 供应商 / 模型注册表与角色解析。

设计约束：

- 旧变量 ``LLM_API_KEY`` / ``LLM_BASE_URL`` / ``LLM_MODEL_ID`` 原样可用。未提供
  ``HARNESS_LLM_CONFIG`` 时，它们等价于一个名为 ``default`` 的 OpenAI-compatible
  provider 与一个未注册模型；所有角色继承 ``main``，行为与改造前一致。
- 新配置是一个 YAML/JSON 文件（或内联文本），结构为 ``providers`` / ``models`` /
  ``roles`` / ``fallbacks``。角色先支持 ``main``、``summary``、``memory``、
  ``title``、``approval``，未配置的角色继承 ``main``。
- API key 只能通过环境变量名（``api_key_env``）引用，配置里出现字面量 key 直接报错，
  避免密钥被顺手提交进仓库。
- 客户端按 (provider, base_url, key, timeout) 缓存；解析与缓存都可在测试里用
  ``load_settings`` / ``reset_cache`` 隔离。
- 解析结果完全由传入的 environ 决定（``environ=None`` 时才读 ``os.environ``），
  这样启动校验可以拿显式字典做纯函数判断。
"""
from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

ROLES = ('main', 'summary', 'memory', 'title', 'approval')
DEFAULT_PROVIDER = 'default'
DEFAULT_API_KEY_ENV = 'LLM_API_KEY'
DEFAULT_TIMEOUT = 120.0
ROLE_ENV = {role: f'HARNESS_MODEL_{role.upper()}' for role in ROLES}

DEFAULT_CAPABILITIES = {
    'stream': True,
    'tools': True,
    'usage': True,
    'reasoning': False,
    'images': False,
}
_ALLOWED_TOP_LEVEL = {
    'version', 'providers', 'models', 'roles', 'fallbacks', 'fallback_cross_provider',
}
_ALLOWED_PROVIDER_KEYS = {'base_url', 'api_key_env', 'timeout'}
_ALLOWED_MODEL_KEYS = {'provider', 'model', 'context_window', 'capabilities'}
_SIGNATURE_ENV_KEYS = (
    'LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL_ID', 'FALLBACK_MODEL_ID',
    'HARNESS_JUDGE_MODEL', 'HARNESS_LLM_CONFIG', 'HARNESS_ALLOW_CROSS_PROVIDER_FALLBACK',
    *ROLE_ENV.values(),
)


class ConfigError(RuntimeError):
    """配置缺失或格式错误；消息面向终端用户，应可直接照做。"""


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str | None = None
    api_key_env: str = DEFAULT_API_KEY_ENV
    timeout: float = DEFAULT_TIMEOUT

    def api_key(self, environ: Mapping[str, str]) -> str:
        return environ.get(self.api_key_env, '') if self.api_key_env else ''


@dataclass(frozen=True)
class ModelConfig:
    """注册表条目。``name`` 是引用名，``model`` 是发给供应商的真实模型 id。"""

    name: str
    provider: str = DEFAULT_PROVIDER
    model: str | None = None
    context_window: int | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)

    @property
    def api_model(self) -> str:
        return self.model or self.name


@dataclass(frozen=True)
class ModelTarget:
    """一次调用最终落到哪个 provider、哪个模型、用哪个 key。"""

    role: str
    provider: str
    model: str
    base_url: str | None = None
    api_key_env: str = DEFAULT_API_KEY_ENV
    timeout: float = DEFAULT_TIMEOUT
    context_window: int | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)
    _api_key: str = field(default='', repr=False, compare=False)

    @property
    def ref(self) -> str:
        return f'{self.provider}/{self.model}'

    @property
    def api_key(self) -> str:
        return self._api_key

    def supports(self, feature: str) -> bool:
        return bool(self.capabilities.get(feature, False))


class LLMSettings:
    """不可变的解析结果。解析只用构造时传入的 environ。"""

    def __init__(self, *, providers, models, roles, fallbacks, environ,
                 source='env', fallback_cross_provider=False):
        self.providers = providers
        self.models = models
        self.roles = roles
        self.fallbacks = fallbacks
        self.source = source
        self.fallback_cross_provider = fallback_cross_provider
        self._environ = environ

    # ---------- 解析 ----------

    def resolve(self, ref: str | ModelTarget, *, role: str = 'main',
                provider_hint: str | None = None) -> ModelTarget:
        """把 ``name`` / ``provider/model`` 解析成可调用的目标。"""
        if isinstance(ref, ModelTarget):
            return replace(ref, role=role)
        name = str(ref or '').strip()
        if not name:
            raise ConfigError(f'role {role!r}: empty model reference')

        cfg = self.models.get(name)
        if cfg is None and '/' in name:
            prefix, rest = name.split('/', 1)
            if prefix in self.providers and rest.strip():
                cfg = ModelConfig(name=rest, provider=prefix)
        provider_name = self._provider_for(cfg, provider_hint, name)
        provider = self.providers[provider_name]
        capabilities = dict(DEFAULT_CAPABILITIES)
        if cfg is not None:
            capabilities.update(cfg.capabilities)
        model = cfg.api_model if cfg is not None else name
        return ModelTarget(
            role=role,
            provider=provider_name,
            model=model,
            base_url=provider.base_url,
            api_key_env=provider.api_key_env,
            timeout=provider.timeout,
            context_window=cfg.context_window if cfg is not None else None,
            capabilities=capabilities,
            _api_key=provider.api_key(self._environ),
        )

    def _provider_for(self, cfg: ModelConfig | None, hint: str | None,
                      name: str) -> str:
        if cfg is not None:
            if cfg.provider not in self.providers:
                raise ConfigError(
                    f'model {cfg.name!r} references unknown provider {cfg.provider!r}')
            return cfg.provider
        if hint and hint in self.providers:
            return hint
        if DEFAULT_PROVIDER in self.providers:
            return DEFAULT_PROVIDER
        if len(self.providers) == 1:
            return next(iter(self.providers))
        known = ', '.join(sorted(self.providers)) or '(none)'
        raise ConfigError(
            f'cannot resolve model {name!r}: provider is ambiguous; '
            f'use provider/model (known providers: {known})')

    def resolve_for(self, role: str = 'main') -> ModelTarget:
        if role not in ROLES:
            raise ConfigError(f'unknown model role {role!r}; expected one of {", ".join(ROLES)}')

        if role == 'main':
            ref = (self._environ.get(ROLE_ENV['main'])
                   or self.roles.get('main')
                   or self._environ.get('LLM_MODEL_ID'))
            if not ref:
                raise ConfigError(
                    'no model configured for role main; set LLM_MODEL_ID or '
                    'HARNESS_MODEL_MAIN or roles.main in HARNESS_LLM_CONFIG')
            return self.resolve(ref, role='main')

        main = self.resolve_for('main')
        ref = (self._environ.get(ROLE_ENV[role])
               or self.roles.get(role)
               or (self._environ.get('HARNESS_JUDGE_MODEL') if role == 'approval' else None))
        if not ref:
            return replace(main, role=role)
        return self.resolve(ref, role=role, provider_hint=main.provider)

    def model_for(self, role: str = 'main') -> str:
        return self.resolve_for(role).model

    def context_window_for(self, model: str) -> int | None:
        for cfg in self.models.values():
            if model in (cfg.name, cfg.api_model) and cfg.context_window:
                return cfg.context_window
        return None

    def fallback_targets(self, role: str = 'main',
                         current: ModelTarget | None = None) -> list[ModelTarget]:
        """返回已配置且允许使用的回退目标（按声明顺序、去重）。"""
        refs = self.fallbacks.get(role, ())
        targets: list[ModelTarget] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            try:
                target = self.resolve(
                    ref, role=role, provider_hint=current.provider if current else None)
            except ConfigError:
                continue
            key = (target.provider, target.model)
            if key in seen:
                continue
            if current is not None:
                if key == (current.provider, current.model):
                    continue
                if (not self.fallback_cross_provider
                        and target.provider != current.provider):
                    continue
            seen.add(key)
            targets.append(target)
        return targets

    def validate_main(self) -> ModelTarget:
        target = self.resolve_for('main')
        missing = [target.api_key_env] if target.api_key_env and not target.api_key else []
        if missing:
            raise ConfigError(
                'Missing required configuration: ' + ', '.join(missing)
                + '. Set it in .env before starting Harness Agent.')
        if not target.supports('tools'):
            raise ConfigError(
                f'main model {target.ref!r} declares tools=false; '
                'the agent loop requires tool calling')
        return target


def _mapping(value: Any, label: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ConfigError(f'{label} must be a mapping, got {type(value).__name__}')
    return value


def _clean_str(value: Any, label: str, *, allow_empty=True) -> str:
    if not isinstance(value, str):
        raise ConfigError(f'{label} must be a string')
    text = value.strip()
    if not text and not allow_empty:
        raise ConfigError(f'{label} must not be empty')
    return text


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f'{label} must be a positive integer')
    return value


def _positive_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f'{label} must be a positive number')
    return float(value)


def _parse_providers(raw: Any, environ: Mapping[str, str]) -> dict[str, ProviderConfig]:
    providers: dict[str, ProviderConfig] = {}
    for name, value in _mapping(raw, 'providers').items():
        name = _clean_str(name, 'provider name', allow_empty=False)
        if '/' in name:
            raise ConfigError(f'provider name {name!r} must not contain "/"')
        entry = _mapping(value, f'provider {name!r}')
        unknown = set(entry) - _ALLOWED_PROVIDER_KEYS - {'api_key'}
        if unknown:
            raise ConfigError(f'provider {name!r}: unknown keys {", ".join(sorted(unknown))}')
        if 'api_key' in entry:
            raise ConfigError(
                f'provider {name!r}: inline api_key is not supported; '
                'use api_key_env with the name of an environment variable')
        base_url = entry.get('base_url')
        if base_url is not None:
            base_url = _clean_str(base_url, f'provider {name!r}.base_url') or None
        elif name == DEFAULT_PROVIDER:
            base_url = _clean_str(
                environ.get('LLM_BASE_URL', ''), 'LLM_BASE_URL') or None
        api_key_env = entry.get('api_key_env', DEFAULT_API_KEY_ENV)
        if api_key_env is None:
            api_key_env = ''
        api_key_env = _clean_str(api_key_env, f'provider {name!r}.api_key_env')
        timeout = _positive_float(
            entry.get('timeout', DEFAULT_TIMEOUT), f'provider {name!r}.timeout')
        providers[name] = ProviderConfig(
            name=name, base_url=base_url, api_key_env=api_key_env, timeout=timeout)
    if not providers:
        raise ConfigError('providers must define at least one provider')
    return providers


def _parse_models(raw: Any, providers: Mapping[str, ProviderConfig]) -> dict[str, ModelConfig]:
    models: dict[str, ModelConfig] = {}
    default_provider = (DEFAULT_PROVIDER if DEFAULT_PROVIDER in providers
                        else next(iter(providers)) if len(providers) == 1 else None)
    for name, value in _mapping(raw, 'models').items():
        name = _clean_str(name, 'model name', allow_empty=False)
        if isinstance(value, str):
            value = {'provider': value}
        entry = _mapping(value, f'model {name!r}')
        unknown = set(entry) - _ALLOWED_MODEL_KEYS
        if unknown:
            raise ConfigError(f'model {name!r}: unknown keys {", ".join(sorted(unknown))}')
        provider = entry.get('provider', default_provider)
        if not provider:
            raise ConfigError(
                f'model {name!r}: provider is required when several providers are defined')
        provider = _clean_str(provider, f'model {name!r}.provider', allow_empty=False)
        if provider not in providers:
            raise ConfigError(f'model {name!r}: unknown provider {provider!r}')
        api_model = entry.get('model')
        if api_model is not None:
            api_model = _clean_str(api_model, f'model {name!r}.model', allow_empty=False)
        context_window = entry.get('context_window')
        if context_window is not None:
            context_window = _positive_int(context_window, f'model {name!r}.context_window')
        capabilities: dict[str, bool] = {}
        for feature, enabled in _mapping(
                entry.get('capabilities', {}), f'model {name!r}.capabilities').items():
            if feature not in DEFAULT_CAPABILITIES:
                raise ConfigError(
                    f'model {name!r}: unknown capability {feature!r}; '
                    f'expected one of {", ".join(DEFAULT_CAPABILITIES)}')
            if not isinstance(enabled, bool):
                raise ConfigError(f'model {name!r}.capabilities.{feature} must be a boolean')
            capabilities[feature] = enabled
        models[name] = ModelConfig(
            name=name, provider=provider, model=api_model,
            context_window=context_window, capabilities=capabilities)
    return models


def _parse_roles(raw: Any, label: str) -> dict[str, str]:
    roles: dict[str, str] = {}
    for role, ref in _mapping(raw, label).items():
        role = _clean_str(role, f'{label} role', allow_empty=False)
        if role not in ROLES:
            raise ConfigError(
                f'{label}: unknown role {role!r}; expected one of {", ".join(ROLES)}')
        roles[role] = _clean_str(ref, f'{label}.{role}', allow_empty=False)
    return roles


def _parse_fallbacks(raw: Any) -> dict[str, tuple[str, ...]]:
    fallbacks: dict[str, tuple[str, ...]] = {}
    for role, refs in _mapping(raw, 'fallbacks').items():
        role = _clean_str(role, 'fallbacks role', allow_empty=False)
        if role not in ROLES:
            raise ConfigError(
                f'fallbacks: unknown role {role!r}; expected one of {", ".join(ROLES)}')
        if isinstance(refs, str):
            refs = [refs]
        if not isinstance(refs, list):
            raise ConfigError(f'fallbacks.{role} must be a model reference or a list')
        cleaned = []
        for index, ref in enumerate(refs):
            cleaned.append(_clean_str(ref, f'fallbacks.{role}[{index}]', allow_empty=False))
        fallbacks[role] = tuple(cleaned)
    return fallbacks


def _read_config(environ: Mapping[str, str]) -> tuple[Mapping | None, str]:
    raw = str(environ.get('HARNESS_LLM_CONFIG') or '').strip()
    if not raw:
        return None, 'env'
    path = Path(raw).expanduser()
    if path.is_file():
        try:
            text = path.read_text(encoding='utf-8')
        except OSError as exc:
            raise ConfigError(f'cannot read HARNESS_LLM_CONFIG {path}: {exc}') from exc
        source = str(path)
    else:
        text = raw
        source = '<inline>'
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f'cannot parse LLM config from {source}: {exc}') from exc
    if data is None:
        raise ConfigError(f'LLM config from {source} is empty')
    return _mapping(data, f'LLM config from {source}'), source


def _legacy_fallbacks(environ: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    model = str(environ.get('FALLBACK_MODEL_ID') or '').strip()
    return {'main': (model,)} if model else {}


def load_settings(environ: Mapping[str, str] | None = None) -> LLMSettings:
    """按给定环境解析配置。传入 environ 时是纯函数，不读进程环境。"""
    environ = os.environ if environ is None else environ
    data, source = _read_config(environ)
    if data is None:
        providers = {
            DEFAULT_PROVIDER: ProviderConfig(
                name=DEFAULT_PROVIDER,
                base_url=_clean_str(
                    environ.get('LLM_BASE_URL', ''), 'LLM_BASE_URL') or None,
                api_key_env=DEFAULT_API_KEY_ENV,
            ),
        }
        return LLMSettings(
            providers=providers, models={}, roles={},
            fallbacks=_legacy_fallbacks(environ), environ=environ,
            source='env',
            fallback_cross_provider=False,
        )

    unknown = set(data) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise ConfigError(f'LLM config: unknown keys {", ".join(sorted(unknown))}')
    providers = _parse_providers(data.get('providers', {}), environ)
    models = _parse_models(data.get('models', {}), providers)
    roles = _parse_roles(data.get('roles', {}), 'roles')
    fallbacks = _parse_fallbacks(data.get('fallbacks', {}))
    if not fallbacks:
        fallbacks = _legacy_fallbacks(environ)
    cross = data.get('fallback_cross_provider')
    if cross is None:
        cross = str(environ.get('HARNESS_ALLOW_CROSS_PROVIDER_FALLBACK') or '').lower() in {
            '1', 'true', 'yes', 'on'}
    elif not isinstance(cross, bool):
        raise ConfigError('fallback_cross_provider must be a boolean')
    return LLMSettings(
        providers=providers, models=models, roles=roles, fallbacks=fallbacks,
        environ=environ, source=source, fallback_cross_provider=cross,
    )


# ---------- 设置缓存 ----------

_settings_cache: dict[tuple, LLMSettings] = {}
_settings_lock = threading.Lock()
_clients: dict[tuple, Any] = {}
_clients_lock = threading.Lock()


def _config_stamp(raw: str) -> tuple[int, int] | None:
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _env_signature(environ: Mapping[str, str]) -> tuple:
    values = tuple(environ.get(key) for key in _SIGNATURE_ENV_KEYS)
    raw = str(environ.get('HARNESS_LLM_CONFIG') or '')
    return values + (_config_stamp(raw),)


def get_settings() -> LLMSettings:
    """进程级缓存；相关环境变量或配置文件变化时自动重建。"""
    environ = os.environ
    signature = _env_signature(environ)
    with _settings_lock:
        settings = _settings_cache.get(signature)
        if settings is None:
            settings = load_settings(environ)
            _settings_cache.clear()
            _settings_cache[signature] = settings
        return settings


def reset_cache() -> None:
    """测试用：丢弃配置与客户端缓存。"""
    with _settings_lock:
        _settings_cache.clear()
    with _clients_lock:
        _clients.clear()


def get_client(target: ModelTarget | str = 'main'):
    """按目标缓存 OpenAI 兼容客户端；不同 key / base_url 互不复用。"""
    if not isinstance(target, ModelTarget):
        target = get_settings().resolve_for(str(target))
    if target.api_key_env and not target.api_key:
        raise ConfigError(
            f'{target.ref}: environment variable {target.api_key_env} is not set')
    key = (target.provider, target.base_url, target.api_key_env,
           target.api_key, target.timeout)
    with _clients_lock:
        cached = _clients.get(key)
        if cached is not None:
            return cached
        import openai
        kwargs: dict[str, Any] = {
            'api_key': target.api_key or 'not-needed',
            'timeout': target.timeout,
        }
        if target.base_url:
            kwargs['base_url'] = target.base_url
        client = openai.OpenAI(**kwargs)
        _clients[key] = client
        return client


class LazyClient:
    """延迟构造指定角色的客户端，属性访问透明委托。

    openai SDK 的 import 约 1.5s，CLI/测试进程启动时不该为它买单；真正发请求时
    才解析角色并构造客户端。``warmup`` 供入口在后台线程提前预热。
    """

    def __init__(self, role: str = 'main'):
        self.role = role
        self._real = None
        self._key = None
        self._lock = threading.Lock()

    @property
    def target(self) -> ModelTarget:
        return get_settings().resolve_for(self.role)

    def _instance(self):
        target = self.target
        key = (target.provider, target.base_url, target.api_key_env,
               target.api_key, target.timeout)
        if self._real is None or key != self._key:
            with self._lock:
                if self._real is None or key != self._key:
                    self._real = get_client(target)
                    self._key = key
        return self._real

    def __getattr__(self, name):
        return getattr(self._instance(), name)

    def warmup(self):
        self._instance()

    def __repr__(self) -> str:
        return f'<LazyClient role={self.role}>'
