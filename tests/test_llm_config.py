"""供应商注册表、角色解析与客户端缓存的单元测试。"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import llm_config


@pytest.fixture(autouse=True)
def _clean_cache():
    llm_config.reset_cache()
    yield
    llm_config.reset_cache()


LEGACY_ENV = {
    'LLM_API_KEY': 'legacy-key',
    'LLM_BASE_URL': 'https://legacy.test/v1',
    'LLM_MODEL_ID': 'legacy-model',
}

CONFIG_YAML = '''
providers:
  default:
    base_url: https://main.test/v1
    api_key_env: MAIN_KEY
  budget:
    base_url: https://budget.test/v1
    api_key_env: BUDGET_KEY
models:
  main-model:
    provider: default
    context_window: 131072
    capabilities:
      reasoning: true
  cheap-model:
    provider: default
    model: cheap-api
  remote-model:
    provider: budget
    model: remote-api
roles:
  main: main-model
  summary: cheap-model
fallbacks:
  main: [cheap-model, remote-model]
'''


def _config_env(**extra):
    env = {'MAIN_KEY': 'main-key', 'BUDGET_KEY': 'budget-key',
           'HARNESS_LLM_CONFIG': CONFIG_YAML}
    env.update(extra)
    return env


# ---------- 旧变量兼容 ----------


def test_legacy_env_resolves_main_and_inherits_every_role():
    settings = llm_config.load_settings(LEGACY_ENV)
    main = settings.resolve_for('main')

    assert (main.provider, main.model) == ('default', 'legacy-model')
    assert main.base_url == 'https://legacy.test/v1'
    assert main.api_key == 'legacy-key'
    for role in ('summary', 'memory', 'title', 'approval'):
        target = settings.resolve_for(role)
        assert (target.provider, target.model) == ('default', 'legacy-model')
        assert target.role == role


def test_legacy_fallback_model_stays_on_the_main_provider():
    settings = llm_config.load_settings({**LEGACY_ENV, 'FALLBACK_MODEL_ID': 'backup'})
    main = settings.resolve_for('main')
    fallbacks = settings.fallback_targets('main', main)

    assert [target.model for target in fallbacks] == ['backup']
    assert fallbacks[0].provider == 'default'


def test_load_settings_is_pure_when_environ_is_given(monkeypatch):
    monkeypatch.setenv('LLM_MODEL_ID', 'from-process-env')
    settings = llm_config.load_settings({'LLM_API_KEY': 'key', 'LLM_MODEL_ID': 'explicit'})
    assert settings.model_for('main') == 'explicit'


# ---------- 新配置格式 ----------


def test_yaml_config_registry_roles_and_capabilities():
    settings = llm_config.load_settings(_config_env())
    main = settings.resolve_for('main')
    summary = settings.resolve_for('summary')

    assert main.ref == 'default/main-model'
    assert main.context_window == 131072
    assert main.supports('tools') and main.supports('reasoning')
    assert not main.supports('images')
    assert summary.ref == 'default/cheap-api'
    assert summary.context_window is None
    assert settings.context_window_for('main-model') == 131072
    assert settings.context_window_for('cheap-api') is None


def test_inline_json_config_is_accepted():
    payload = json.dumps({
        'providers': {'default': {'api_key_env': 'KEY', 'base_url': 'https://json.test'}},
        'models': {'m': {'provider': 'default'}},
        'roles': {'main': 'm'},
    })
    settings = llm_config.load_settings({'KEY': 'k', 'HARNESS_LLM_CONFIG': payload})
    target = settings.resolve_for('main')
    assert (target.ref, target.base_url) == ('default/m', 'https://json.test')


def test_config_file_is_read_from_disk(tmp_path):
    path = tmp_path / 'llm.yaml'
    path.write_text(CONFIG_YAML, encoding='utf-8')
    settings = llm_config.load_settings({**_config_env(), 'HARNESS_LLM_CONFIG': str(path)})
    assert settings.source == str(path)
    assert settings.model_for('main') == 'main-model'


def test_provider_prefixed_reference_beats_bare_name_resolution():
    settings = llm_config.load_settings(_config_env())
    target = settings.resolve('budget/other-model')
    assert (target.provider, target.model) == ('budget', 'other-model')
    assert target.api_key == 'budget-key'


def test_unknown_prefix_is_kept_as_a_model_id_for_openrouter_style_names():
    settings = llm_config.load_settings(_config_env())
    target = settings.resolve('openrouter/anthropic/claude')
    assert (target.provider, target.model) == ('default', 'openrouter/anthropic/claude')


def test_ambiguous_bare_model_is_reported_without_a_default_provider():
    config = '''
providers:
  one: {base_url: https://one.test, api_key_env: K1}
  two: {base_url: https://two.test, api_key_env: K2}
models: {m: {provider: one}}
roles: {main: m}
'''
    settings = llm_config.load_settings(
        {'K1': 'a', 'K2': 'b', 'HARNESS_LLM_CONFIG': config})
    with pytest.raises(llm_config.ConfigError, match='ambiguous'):
        settings.resolve('unregistered')


def test_inline_api_key_is_rejected():
    config = 'providers: {p: {api_key: secret-value}}'
    with pytest.raises(llm_config.ConfigError) as excinfo:
        llm_config.load_settings({'HARNESS_LLM_CONFIG': config})
    assert 'api_key_env' in str(excinfo.value)


def test_unknown_top_level_role_and_capability_are_rejected():
    with pytest.raises(llm_config.ConfigError, match='unknown keys'):
        llm_config.load_settings({'HARNESS_LLM_CONFIG': 'providerz: {}'})

    with pytest.raises(llm_config.ConfigError, match='at least one provider'):
        llm_config.load_settings({'HARNESS_LLM_CONFIG': 'providers: {}'})

    base = '''
providers: {default: {api_key_env: K}}
models: {m: {provider: default}}
'''
    with pytest.raises(llm_config.ConfigError, match='unknown role'):
        llm_config.load_settings({'HARNESS_LLM_CONFIG': base + 'roles: {planner: m}\n'})

    bad_capability = '''
providers: {default: {api_key_env: K}}
models: {m: {provider: default, capabilities: {tool_use: true}}}
'''
    with pytest.raises(llm_config.ConfigError, match='unknown capability'):
        llm_config.load_settings({'HARNESS_LLM_CONFIG': bad_capability})


# ---------- 优先级 ----------


def test_env_role_override_beats_config_and_main_beats_legacy_model():
    env = _config_env(**{
        'LLM_MODEL_ID': 'legacy-model',
        'HARNESS_MODEL_MAIN': 'cheap-model',
        'HARNESS_MODEL_TITLE': 'remote-model',
    })
    settings = llm_config.load_settings(env)
    assert settings.resolve_for('main').ref == 'default/cheap-api'
    assert settings.resolve_for('title').ref == 'budget/remote-api'
    assert settings.resolve_for('summary').ref == 'default/cheap-api'


def test_config_main_beats_legacy_llm_model_id():
    settings = llm_config.load_settings(_config_env(LLM_MODEL_ID='legacy-model'))
    assert settings.resolve_for('main').model == 'main-model'


def test_judge_env_maps_to_approval_role_but_explicit_role_wins():
    env = _config_env(HARNESS_JUDGE_MODEL='judge-model')
    assert llm_config.load_settings(env).model_for('approval') == 'judge-model'

    with_role = CONFIG_YAML.replace(
        'fallbacks:', '  approval: remote-model\nfallbacks:')
    env_with_role = _config_env(
        HARNESS_JUDGE_MODEL='judge-model', HARNESS_LLM_CONFIG=with_role)
    assert llm_config.load_settings(env_with_role).resolve_for('approval').ref == 'budget/remote-api'


# ---------- 回退 ----------


def test_cross_provider_fallback_requires_explicit_opt_in():
    settings = llm_config.load_settings(_config_env())
    main = settings.resolve_for('main')

    same_provider = settings.fallback_targets('main', main)
    assert [target.model for target in same_provider] == ['cheap-api']

    enabled = llm_config.load_settings(_config_env(HARNESS_ALLOW_CROSS_PROVIDER_FALLBACK='1'))
    both = enabled.fallback_targets('main', enabled.resolve_for('main'))
    assert [target.ref for target in both] == ['default/cheap-api', 'budget/remote-api']


def test_fallback_entries_never_repeat_the_current_target():
    settings = llm_config.load_settings(_config_env(HARNESS_ALLOW_CROSS_PROVIDER_FALLBACK='1'))
    current = settings.resolve('cheap-model', role='main')
    assert [target.model for target in settings.fallback_targets('main', current)] == ['remote-api']


# ---------- 启动校验 ----------


def test_validate_main_reports_missing_key_and_rejects_tool_less_main():
    settings = llm_config.load_settings(_config_env(MAIN_KEY=''))
    with pytest.raises(llm_config.ConfigError, match='MAIN_KEY'):
        settings.validate_main()

    no_tools = '''
providers: {default: {api_key_env: K}}
models: {m: {provider: default, capabilities: {tools: false}}}
roles: {main: m}
'''
    settings = llm_config.load_settings({'K': 'k', 'HARNESS_LLM_CONFIG': no_tools})
    with pytest.raises(llm_config.ConfigError, match='requires tool calling'):
        settings.validate_main()


def test_missing_main_model_is_actionable():
    settings = llm_config.load_settings({})
    with pytest.raises(llm_config.ConfigError, match='LLM_MODEL_ID'):
        settings.resolve_for('main')


# ---------- 客户端缓存 ----------


class _FakeOpenAI:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_: 'ok'))
        self.__class__.instances.append(self)


@pytest.fixture()
def fake_openai(monkeypatch):
    _FakeOpenAI.instances = []
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=_FakeOpenAI))
    return _FakeOpenAI


def test_client_cache_is_keyed_by_provider_base_url_key_and_timeout(fake_openai):
    settings = llm_config.load_settings(LEGACY_ENV)
    target = settings.resolve_for('main')

    first = llm_config.get_client(target)
    second = llm_config.get_client(target)
    assert first is second
    assert fake_openai.instances[0].kwargs['api_key'] == 'legacy-key'

    changed = llm_config.ModelTarget(
        role='main', provider='default', model='m',
        base_url=target.base_url, api_key_env=target.api_key_env,
        timeout=target.timeout, capabilities=target.capabilities, _api_key='other-key')
    assert llm_config.get_client(changed) is not first
    assert len(fake_openai.instances) == 2
    assert fake_openai.instances[1].kwargs['api_key'] == 'other-key'


def test_get_client_reports_missing_key_instead_of_sending_placeholder(fake_openai):
    settings = llm_config.load_settings({'LLM_MODEL_ID': 'm'})
    with pytest.raises(llm_config.ConfigError, match='LLM_API_KEY'):
        llm_config.get_client(settings.resolve_for('main'))
    assert fake_openai.instances == []


def test_client_without_api_key_env_uses_placeholder(fake_openai):
    config = '''
providers: {local: {base_url: 'http://localhost:11434/v1', api_key_env: ''}}
models: {m: {provider: local, capabilities: {tools: true}}}
roles: {main: m}
'''
    settings = llm_config.load_settings({'HARNESS_LLM_CONFIG': config})
    target = settings.resolve_for('main')
    settings.validate_main()
    assert llm_config.get_client(target).kwargs['api_key'] == 'not-needed'


def test_lazy_client_rebuilds_when_provider_credentials_change(fake_openai, monkeypatch):
    monkeypatch.setenv('LLM_MODEL_ID', 'm')
    monkeypatch.setenv('LLM_API_KEY', 'first-key')
    lazy = llm_config.LazyClient('main')
    lazy.warmup()
    assert fake_openai.instances[0].kwargs['api_key'] == 'first-key'

    monkeypatch.setenv('LLM_API_KEY', 'second-key')
    lazy.warmup()
    assert len(fake_openai.instances) == 2
    assert fake_openai.instances[1].kwargs['api_key'] == 'second-key'


def test_lazy_client_defers_construction_and_supports_instance_warmup(fake_openai, monkeypatch):
    monkeypatch.setenv('LLM_MODEL_ID', 'm')
    monkeypatch.setenv('LLM_API_KEY', 'k')
    lazy = llm_config.LazyClient('main')
    assert fake_openai.instances == []

    lazy.warmup()
    assert len(fake_openai.instances) == 1

    replaced = []
    monkeypatch.setattr(lazy, 'warmup', lambda: replaced.append(1))
    lazy.warmup()
    assert replaced == [1]


# ---------- 设置缓存 ----------


def test_get_settings_rebuilds_when_relevant_env_changes(monkeypatch):
    monkeypatch.setenv('LLM_MODEL_ID', 'first')
    monkeypatch.setenv('LLM_API_KEY', 'k')
    assert llm_config.get_settings().model_for('main') == 'first'
    first = llm_config.get_settings()

    monkeypatch.setenv('LLM_MODEL_ID', 'second')
    assert llm_config.get_settings().model_for('main') == 'second'
    assert llm_config.get_settings() is not first


def test_get_settings_rebuilds_when_config_file_changes(monkeypatch, tmp_path):
    monkeypatch.setenv('LLM_API_KEY', 'k')
    monkeypatch.delenv('LLM_MODEL_ID', raising=False)
    path = tmp_path / 'llm.yaml'
    path.write_text(
        'providers: {default: {api_key_env: LLM_API_KEY}}\n'
        'models: {first: {provider: default}}\n'
        'roles: {main: first}\n', encoding='utf-8')
    monkeypatch.setenv('HARNESS_LLM_CONFIG', str(path))
    assert llm_config.get_settings().model_for('main') == 'first'

    path.write_text(
        'providers: {default: {api_key_env: LLM_API_KEY}}\n'
        'models: {second-model: {provider: default}}\n'
        'roles: {main: second-model}\n', encoding='utf-8')
    assert llm_config.get_settings().model_for('main') == 'second-model'


def test_model_target_ref_is_provider_and_api_model():
    settings = llm_config.load_settings(_config_env())
    assert settings.resolve_for('summary').ref == 'default/cheap-api'
    assert settings.source == '<inline>'
