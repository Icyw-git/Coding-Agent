import json
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import agent_teams
import background_task
import cron_scheduler
import hooks
import harness
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
