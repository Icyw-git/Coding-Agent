"""错误分类、回退切换、流式工具调用与角色客户端路由。"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import harness
import llm_config
import llm_route
import recovery


@pytest.fixture(autouse=True)
def _clean_caches():
    llm_config.reset_cache()
    yield
    llm_config.reset_cache()


class _StatusError(Exception):
    def __init__(self, status_code, message=''):
        super().__init__(message)
        self.status_code = status_code


class _RateLimitError(Exception):
    pass


class _APIConnectionError(Exception):
    pass


class _APITimeoutError(Exception):
    pass


# ---------- 错误分类 ----------


@pytest.mark.parametrize(('error', 'kind'), [
    (_StatusError(400, 'maximum context length is 128000 tokens'), 'context_length'),
    (_RateLimitError('Prompt is too long for this model'), 'context_length'),
    (_StatusError(401, 'invalid api key'), 'auth'),
    (_StatusError(403, 'forbidden'), 'auth'),
    (_StatusError(429, 'Overloaded'), 'rate_limit'),
    (_RateLimitError('rate limit reached'), 'rate_limit'),
    (_StatusError(529, 'overloaded'), 'overloaded'),
    (_StatusError(500, 'internal error'), 'server'),
    (_StatusError(503, 'unavailable'), 'server'),
    (_StatusError(404, 'model not found'), 'invalid_request'),
    (_StatusError(400, 'bad request'), 'invalid_request'),
    (_APITimeoutError('Request timed out'), 'timeout'),
    (TimeoutError('read timeout'), 'timeout'),
    (_APIConnectionError('connection reset by peer'), 'network'),
    (ValueError('something odd'), 'unknown'),
])
def test_classify_error_is_provider_neutral(error, kind):
    assert recovery.classify_error(error) == kind


def test_429_with_overloaded_text_is_not_misclassified_as_overload():
    """旧实现先匹配 'overloaded'，429 永远走不进 rate-limit 分支。"""
    error = _StatusError(429, 'The engine is currently overloaded')
    assert recovery.classify_error(error) == 'rate_limit'


def test_retry_after_is_read_from_headers_and_bounded():
    response = SimpleNamespace(headers={'retry-after': '2.5'})
    error = _StatusError(429, 'slow down')
    error.response = response
    assert recovery._retry_after(error) == 2.5

    response.headers = {'Retry-After': '9999'}
    assert recovery._retry_after(error) == 60

    response.headers = {'retry-after': 'soon'}
    assert recovery._retry_after(error) is None


# ---------- 重试与回退 ----------


def _state_with_targets(*models):
    targets = [
        llm_config.ModelTarget(role='main', provider='default', model=model)
        for model in models
    ]
    state = recovery.RecoveryState()
    state.current_target = targets[0]
    state.current_model = targets[0].model
    state.fallback_targets = targets[1:]
    state.fallback_index = 0
    return state, targets[0]


def test_non_retryable_error_is_raised_immediately(monkeypatch):
    attempts = []
    state, _ = _state_with_targets('main')

    def fail():
        attempts.append(1)
        raise _StatusError(401, 'invalid api key')

    with pytest.raises(_StatusError):
        recovery.with_retry(fail, state)
    assert len(attempts) == 1


def test_partial_stream_output_is_never_replayed(monkeypatch):
    attempts = []
    state, _ = _state_with_targets('main')

    def fail():
        attempts.append(1)
        state.stream_started = True
        raise _StatusError(503, 'upstream reset')

    with pytest.raises(RuntimeError, match='不再重试'):
        recovery.with_retry(fail, state)
    assert len(attempts) == 1, '已向用户交付文本后不得重试'


def test_server_errors_retry_with_backoff(monkeypatch):
    monkeypatch.setattr(recovery, 'retry_delay', lambda attempt, retry_after=None: 0)
    monkeypatch.setattr(recovery, 'MAX_RETRIES', 3)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _StatusError(503, 'unavailable')
        return 'ok'

    assert recovery.with_retry(flaky, _state_with_targets('main')[0]) == 'ok'
    assert len(calls) == 3


def test_retries_exhausted_switch_to_fallback_model(monkeypatch):
    monkeypatch.setattr(recovery, 'retry_delay', lambda attempt, retry_after=None: 0)
    monkeypatch.setattr(recovery, 'MAX_RETRIES', 3)
    monkeypatch.setattr(recovery, 'FALLBACK_ATTEMPTS', 3)
    state, _ = _state_with_targets('primary', 'backup')
    used = []

    def always_500():
        used.append(state.current_model)
        raise _StatusError(503, 'unavailable')

    with pytest.raises(RuntimeError, match='unavailable'):
        recovery.with_retry(always_500, state)
    assert used[:3] == ['primary', 'primary', 'primary']
    assert used[3:] == ['backup', 'backup', 'backup']
    assert state.current_model == 'backup'


def test_consecutive_overloads_switch_before_exhaustion(monkeypatch):
    monkeypatch.setattr(recovery, 'retry_delay', lambda attempt, retry_after=None: 0)
    monkeypatch.setattr(recovery, 'MAX_CONSECUTIVE_529', 2)
    state, _ = _state_with_targets('primary', 'backup')
    used = []

    def fail_twice_then_ok():
        used.append(state.current_model)
        if len(used) <= 2:
            raise _StatusError(529, 'overloaded')
        return 'ok'

    assert recovery.with_retry(fail_twice_then_ok, state) == 'ok'
    assert used == ['primary', 'primary', 'backup']


def test_fallback_switch_can_be_disabled_by_empty_targets():
    state, _ = _state_with_targets('only')
    assert recovery.switch_fallback(state, 'test') is False
    assert state.current_model == 'only'


# ---------- 流式兼容形状 ----------


def test_stream_completion_supports_injected_client_and_tool_call_deltas():
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, tool_calls=[
                SimpleNamespace(index=0, id='c1', type='function',
                                function=SimpleNamespace(name='read', arguments='{"pa'))]),
            finish_reason=None)], usage=None),
        SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, tool_calls=[
                SimpleNamespace(index=0, id=None, type=None,
                                function=SimpleNamespace(name=None, arguments='th": "a"}'))]),
            finish_reason='tool_calls')], usage=None),
        SimpleNamespace(choices=[], usage=SimpleNamespace(
            prompt_tokens=12, completion_tokens=3, total_tokens=15)),
    ]
    fake_api = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kwargs: iter(chunks))))

    parts = []
    response = harness._stream_completion({'model': 'x'}, parts.append, api=fake_api)

    calls = response.choices[0].message.tool_calls
    assert [(call.id, call.function.name, call.function.arguments) for call in calls] == [
        ('c1', 'read', '{"path": "a"}')]
    assert response.choices[0].finish_reason == 'tool_calls'
    assert response.usage.total_tokens == 15
    assert parts == []


# ---------- 角色客户端路由 ----------


ROLE_CONFIG = '''
providers:
  default: {base_url: https://main.test/v1, api_key_env: MAIN_KEY}
  budget: {base_url: https://budget.test/v1, api_key_env: BUDGET_KEY}
models:
  main-model: {provider: default}
  cheap-model: {provider: budget, model: cheap-api}
roles: {main: main-model, title: cheap-model}
'''


def test_harness_client_for_keeps_test_patch_point_for_main_provider(monkeypatch):
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace()))
    monkeypatch.setattr(harness, 'client', fake)
    assert harness.client_for('summary') is fake
    assert harness.client_for('approval') is fake


def test_session_title_uses_title_role_model(monkeypatch):
    monkeypatch.setenv('HARNESS_LLM_CONFIG', ROLE_CONFIG)
    monkeypatch.setenv('MAIN_KEY', 'main-key')
    monkeypatch.setenv('BUDGET_KEY', 'budget-key')

    calls = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            calls.append((self.kwargs, kwargs))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='多供应商标题'))])

    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=FakeOpenAI))
    monkeypatch.setattr(harness, 'client', SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **_: pytest.fail('main client used')))))

    title = harness.generate_session_title([
        {'role': 'user', 'content': '给多供应商配置补一个会话标题测试'},
        {'role': 'assistant', 'content': '已经实现了 provider、model 与角色的解析。'},
    ])

    assert title == '多供应商标题'
    assert len(calls) == 1
    client_kwargs, request = calls[0]
    assert client_kwargs['base_url'] == 'https://budget.test/v1'
    assert request['model'] == 'cheap-api'


def test_harness_client_for_uses_other_provider_for_configured_role(monkeypatch):
    monkeypatch.setenv('HARNESS_LLM_CONFIG', ROLE_CONFIG)
    monkeypatch.setenv('MAIN_KEY', 'main-key')
    monkeypatch.setenv('BUDGET_KEY', 'budget-key')
    monkeypatch.setenv('HARNESS_ALLOW_CROSS_PROVIDER_FALLBACK', '0')

    created = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=FakeOpenAI))
    assert harness.model_for('main') == 'main-model'
    assert harness.model_for('title') == 'cheap-api'

    main_client = harness.client_for('main')
    title_client = harness.client_for('title')
    assert main_client is harness.client
    assert title_client is not harness.client
    assert title_client.kwargs['base_url'] == 'https://budget.test/v1'
    assert title_client.kwargs['api_key'] == 'budget-key'


def test_llm_route_model_for_uses_legacy_env(monkeypatch):
    monkeypatch.delenv('HARNESS_LLM_CONFIG', raising=False)
    for env_key in llm_config.ROLE_ENV.values():
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv('LLM_API_KEY', 'legacy-key')
    monkeypatch.setenv('LLM_BASE_URL', 'https://legacy.test/v1')
    monkeypatch.setenv('LLM_MODEL_ID', 'legacy-model')

    target = llm_route.target_for('main')
    assert target is not None
    assert target.model == 'legacy-model'
    assert llm_route.model_for('summary') == 'legacy-model'


def test_llm_route_completion_uses_harness_client_patch(monkeypatch):
    monkeypatch.delenv('HARNESS_LLM_CONFIG', raising=False)
    for env_key in llm_config.ROLE_ENV.values():
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv('LLM_API_KEY', 'legacy-key')
    monkeypatch.setenv('LLM_BASE_URL', 'https://legacy.test/v1')
    monkeypatch.setenv('LLM_MODEL_ID', 'legacy-model')

    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[])

    fake = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(harness, 'client', fake)

    llm_route.completion('summary', messages=[{'role': 'user', 'content': 'hi'}])

    assert len(calls) == 1
    assert calls[0]['model'] == 'legacy-model'
    assert calls[0]['messages'] == [{'role': 'user', 'content': 'hi'}]
