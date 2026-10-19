import json
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

import agent_teams
import background_task
import cron_scheduler
import harness
import hooks
import recovery
import task_system


def test_task_claim_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(task_system, 'TASK_DIR', tmp_path)
    monkeypatch.setattr(task_system, 'TASKS_DIR', tmp_path)
    task = task_system.create_task('Only one owner')
    barrier = threading.Barrier(2)
    results = []

    def claim(owner):
        barrier.wait()
        results.append(task_system.claim_task(task.id, owner))

    threads = [threading.Thread(target=claim, args=(owner,)) for owner in ('alice', 'bob')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum('Claimed' in result for result in results) == 1
    assert task_system.load_task(task.id).owner in {'alice', 'bob'}


def test_message_bus_reads_all_sent_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_teams, 'MAILBOX_DIR', tmp_path)
    bus = agent_teams.MessageBus()

    bus.send('alice', 'lead', 'first')
    bus.send('bob', 'lead', 'second')

    assert [message['content'] for message in bus.read_inbox('lead')] == ['first', 'second']
    assert bus.read_inbox('lead') == []


def test_message_bus_rejects_path_traversal_agent_names(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_teams, 'MAILBOX_DIR', tmp_path)
    bus = agent_teams.MessageBus()
    try:
        bus.send('alice', '..\\escape', 'blocked')
    except ValueError as exc:
        assert 'Invalid agent name' in str(exc)
    else:
        raise AssertionError('path traversal agent name was accepted')


def test_background_failure_is_reported():
    with background_task.background_lock:
        background_task.background_tasks.clear()
        background_task.background_results.clear()

    def raises(**_kwargs):
        raise RuntimeError('boom')

    call = SimpleNamespace(
        id='call-1',
        function=SimpleNamespace(name='bash', arguments=json.dumps({'command': 'bad'})),
    )
    background_task.start_background_task(call, {'bash': raises})

    for _ in range(50):
        notifications = background_task.collect_background_results()
        if notifications:
            break
        time.sleep(0.01)
    else:
        raise AssertionError('background failure was not collected')

    assert '<status>failed</status>' in notifications[0]
    assert 'background bash failed: boom' in notifications[0]


def test_background_non_string_result_is_reported():
    with background_task.background_lock:
        background_task.background_tasks.clear()
        background_task.background_results.clear()
    call = SimpleNamespace(
        id='call-none',
        function=SimpleNamespace(name='noop', arguments=json.dumps({})),
    )
    background_task.start_background_task(call, {'noop': lambda **_kwargs: None})
    for _ in range(50):
        notifications = background_task.collect_background_results()
        if notifications:
            break
        time.sleep(0.01)
    assert '<summary>None</summary>' in notifications[0]


def test_cron_uses_standard_day_or_weekday_matching():
    expression = '0 9 1 * 1'

    assert cron_scheduler.cron_matches(expression, datetime(2026, 6, 1, 9, 0))
    assert cron_scheduler.cron_matches(expression, datetime(2026, 6, 8, 9, 0))
    assert not cron_scheduler.cron_matches(expression, datetime(2026, 6, 2, 9, 0))


def test_durable_cron_save_writes_valid_json(tmp_path, monkeypatch):
    path = tmp_path / 'crons.json'
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', path)
    with cron_scheduler.cron_lock:
        cron_scheduler.scheduled_jobs.clear()
    job = cron_scheduler.schedule_job('* * * * *', 'check status')

    saved = json.loads(path.read_text(encoding='utf-8'))

    assert saved[0]['id'] == job.id
    assert not path.with_suffix('.tmp').exists()


def test_cron_ids_do_not_overwrite_existing_jobs(monkeypatch):
    with cron_scheduler.cron_lock:
        cron_scheduler.scheduled_jobs.clear()
    ids = iter(('same00000001', 'same00000002'))
    monkeypatch.setattr(cron_scheduler.uuid, 'uuid4', lambda: SimpleNamespace(hex=next(ids)))
    first = cron_scheduler.schedule_job('* * * * *', 'one', durable=False)
    second = cron_scheduler.schedule_job('* * * * *', 'two', durable=False)
    assert first.id != second.id
    assert len(cron_scheduler.scheduled_jobs) == 2


def test_corrupt_durable_cron_file_is_reported(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'crons.json'
    path.write_text('{bad', encoding='utf-8')
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', path)
    cron_scheduler.load_durable_jobs()
    assert 'failed to load durable jobs' in capsys.readouterr().out


def test_shell_permission_check_is_case_insensitive(monkeypatch):
    class HarnessConfig:
        DENY_LIST = ['sudo']
        DESTRUCTIVE = ['del ', 'remove-item ', 'clear-content ']

    monkeypatch.setattr(hooks, '_h', lambda: HarnessConfig)
    monkeypatch.setattr('builtins.input', lambda _prompt: 'n')

    assert hooks.permission_hook('bash', {'command': 'DEL file.txt'}) == 'Permission denied by user'
    assert hooks.permission_hook('bash', {'command': 'CLEAR-CONTENT file.txt'}) == 'Permission denied by user'
    assert 'Dangerous command' in hooks.permission_hook('bash', {'command': 'SUDO whoami'})


def test_runtime_config_requires_key_and_model():
    try:
        harness.validate_runtime_config({})
    except RuntimeError as exc:
        assert 'LLM_API_KEY, LLM_MODEL_ID' in str(exc)
    else:
        raise AssertionError('missing model configuration was accepted')


def test_runtime_config_allows_default_openai_base_url():
    harness.validate_runtime_config({'LLM_API_KEY': 'key', 'LLM_MODEL_ID': 'model'})


def test_session_path_resolution_accepts_id_only(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    path = tmp_path / 'session_demo_1.jsonl'
    path.write_text('', encoding='utf-8')
    assert harness.resolve_session_path('demo_1') == path
    try:
        harness.resolve_session_path('..\\outside')
    except ValueError:
        pass
    else:
        raise AssertionError('session path traversal was accepted')


def test_set_workspace_resolves_relative_path_without_chdir(tmp_path, monkeypatch):
    original = harness.WORKDIR
    original_cwd = __import__('os').getcwd()
    monkeypatch.setattr(harness, 'WORKDIR', tmp_path)
    selected = tmp_path / 'project'
    selected.mkdir()
    assert harness.set_workspace('project') == selected.resolve()
    assert harness.WORKDIR == selected.resolve()
    assert __import__('os').getcwd() == original_cwd
    monkeypatch.setattr(harness, 'WORKDIR', original)


def test_format_session_list_reports_event_count(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    store = harness.SessionEventStore(tmp_path, session_id='demo')
    store.append_message({'role': 'user', 'content': 'hello'})
    listing = harness.format_session_list()
    assert 'demo' not in listing
    assert 'hello' in listing
    assert 'events=' not in listing


def test_format_session_list_uses_saved_topic_for_acknowledgment_first_session(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    store = harness.SessionEventStore(tmp_path, session_id='topic')
    store.append_message({'role': 'user', 'content': 'go'})
    store.append_message({'role': 'assistant', 'content': 'We will inspect the checkpoint rewind flow.'})
    store.append_session_title('检查 checkpoint 回滚流程')

    assert '检查 checkpoint 回滚流程' in harness.format_session_list()


def test_format_session_list_never_calls_model_or_mutates_legacy_session(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    store = harness.SessionEventStore(tmp_path, session_id='legacy-tool')
    store.append_message({'role': 'user', 'content': 'x'})
    store.append_message({
        'role': 'assistant',
        'content': None,
        'tool_calls': [{
            'id': 'call-1',
            'type': 'function',
            'function': {'name': 'bash', 'arguments': '{"command":"echo 2"}'},
        }],
    })
    store.append_message({'role': 'tool', 'tool_call_id': 'call-1', 'content': '2'})
    store.append_message({'role': 'assistant', 'content': 'The command printed 2 successfully.'})
    def unexpected(_messages):
        pytest.fail('listing must not call the model')
    monkeypatch.setattr(harness, 'generate_session_title', unexpected)
    before = store.path.read_bytes()

    listing = harness.format_session_list()

    assert 'x' in listing
    assert '未记录' in listing
    assert store.path.read_bytes() == before


def test_generate_session_title_does_not_call_model_for_empty_session(monkeypatch):
    def fail_if_called(**_kwargs):
        raise AssertionError('model should not be called')

    monkeypatch.setattr(harness, 'client', SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fail_if_called))))
    assert harness.generate_session_title([{'role': 'user', 'content': 'x'}]) == '(untitled session)'


def test_session_title_is_persisted_and_legacy_title_is_derived(tmp_path):
    legacy = harness.SessionEventStore(tmp_path, session_id='legacy')
    legacy.append_message({'role': 'user', 'content': 'go'})
    legacy.append_message({'role': 'user', 'content': 'Tool results executed before compaction:\n- read completed'})
    legacy.append_message({'role': 'assistant', 'content': 'I will inspect the project structure and explain the architecture.'})
    assert legacy.session_title() == 'I will inspect the project structure and explain the architecture.'

    legacy.append_session_title('Project summary')
    reopened = harness.SessionEventStore.open(legacy.path)
    assert reopened.session_title() == 'Project summary'


def test_legacy_title_skips_repeated_filler():
    from session_store import _useful_title_line

    assert _useful_title_line('hello hello hello hello hello hello') is None
    assert _useful_title_line('Build a session topic summary') == 'Build a session topic summary'


def test_generate_session_title_ignores_acknowledgment(monkeypatch):
    class Message:
        content = '修复 checkpoint 回滚后的工具调用历史'

    class Completions:
        @staticmethod
        def create(**_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=Message())])

    monkeypatch.setattr(harness, 'client', SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    title = harness.generate_session_title([
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': 'We should fix the checkpoint rewind history and preserve tool pairing.'},
    ])
    assert title == '修复 checkpoint 回滚后的工具调用历史'


def test_generate_session_title_material_includes_goal_and_instruments(monkeypatch):
    """取材改造：输入必须包含首条有效 user（目标）与工具名，且指令明确
    「概括整个会话而非复述最后一条消息」（旧实现 exchanges[-8:] 取尾部，
    长会话标题退化成最后一条助手回复的复述）。"""
    captured = {}

    class Completions:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='缓存命中率诊断'))])

    monkeypatch.setattr(harness, 'client', SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    filler = [{'role': 'assistant', 'content': f'中间步骤 {i}'} for i in range(20)]
    title = harness.generate_session_title([
        {'role': 'user', 'content': '诊断缓存命中率为什么只有 58%'},
        *filler,
        {'role': 'assistant', 'content': None,
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'bash', 'arguments': '{"command":"pytest"}'}}]},
    ])
    assert title == '缓存命中率诊断'
    user_prompt = captured['messages'][-1]['content']
    assert '诊断缓存命中率为什么只有 58%' in user_prompt, "目标（会话开头）必须进输入"
    assert 'bash' in user_prompt, "最近工具名进输入"
    system_prompt = captured['messages'][0]['content']
    assert '不是复述最后一条消息' in system_prompt


def test_generate_session_title_skips_injected_user_messages(monkeypatch):
    """reminder/通知等系统注入不得成为标题取材的第一条 user。"""
    captured = {}

    class Completions:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='重构压缩机制'))])

    monkeypatch.setattr(harness, 'client', SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    harness.generate_session_title([
        {'role': 'user', 'content': '<reminder>Update your todos.</reminder>'},
        {'role': 'user', 'content': '重构压缩机制，保持前缀稳定'},
    ])
    user_prompt = captured['messages'][-1]['content']
    assert '重构压缩机制，保持前缀稳定' in user_prompt
    assert '<reminder>' not in user_prompt


def test_housekeeping_title_regenerates_every_8_turns_and_respects_rename(tmp_path, monkeypatch):
    """首次生成；8 的倍数回合异步重生成；用户 /rename（source='user'）后永不覆盖。"""
    monkeypatch.setattr(harness, 'extract_memories', lambda pre_compress: None)
    monkeypatch.setattr(harness, 'consolidate_memories', lambda: None)
    store = harness.SessionEventStore(tmp_path, session_id='ttl')
    titles = iter(['主题一', '主题二', '主题三'])
    monkeypatch.setattr(harness, 'generate_session_title', lambda messages: next(titles))

    harness._post_turn_housekeeping([], [], store, turn_no=1)
    assert store.saved_session_title() == '主题一'
    harness._post_turn_housekeeping([], [], store, turn_no=5)
    assert store.saved_session_title() == '主题一', "非 8 倍数回合不重生成"
    harness._post_turn_housekeeping([], [], store, turn_no=8)
    assert store.saved_session_title() == '主题二', "第 8 回合重生成"
    # /rename 保护
    store.append_session_title('用户命名', source='user')
    harness._post_turn_housekeeping([], [], store, turn_no=16)
    assert store.saved_session_title() == '用户命名', "用户命名不受重生成覆盖"


def test_saved_session_title_source_defaults_for_legacy_events(tmp_path):
    store = harness.SessionEventStore(tmp_path, session_id='src')
    store.append_session_title('旧式标题')   # 旧事件无 source 字段
    assert store.saved_session_title_source() == 'auto'
    store.append_session_title('手动命名', source='user')
    assert store.saved_session_title_source() == 'user'
    assert store.saved_session_title() == '手动命名'


def test_with_retry_reports_last_error_when_retries_exhausted(monkeypatch):
    """重试耗尽要抛 RuntimeError 并带上最后一次失败原因。

    except 块结束时 Python 会删除 e，旧实现在这条路上引用 e 得到的是 NameError，
    模型侧的真实错误被吞掉；该分支此前零覆盖。
    """
    monkeypatch.setattr(recovery, 'MAX_RETRIES', 3)
    monkeypatch.setattr(recovery, 'retry_delay', lambda attempt, retry_after=None: 0)

    attempts = []

    def always_throttled():
        attempts.append(1)
        raise Exception('HTTP 429: too many requests')

    with pytest.raises(RuntimeError) as excinfo:
        recovery.with_retry(always_throttled, recovery.RecoveryState())

    assert len(attempts) == 3
    assert '429' in str(excinfo.value), "失败原因必须留在异常消息里"
