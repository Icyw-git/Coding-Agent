"""默认每日心跳：稳定 ID 注册、只读摘要、occurrence 去重与至少一次投递。

不测「真到 09:00」：时间由 tick(now) 与 run_due(now) 注入，行为与线上路径一致。
"""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

import cron_scheduler
import heartbeat


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, 'STATE_PATH', tmp_path / 'heartbeat' / 'state.json')
    monkeypatch.setattr(heartbeat, 'OUTBOX_PATH', tmp_path / 'heartbeat' / 'outbox.json')
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    monkeypatch.setattr(heartbeat, 'STATUS_TAIL_LIMIT', 50)
    return tmp_path


def _job(last_fired='2026-10-25 09:00'):
    return cron_scheduler.CronJob(id=heartbeat.JOB_ID, cron=heartbeat.DEFAULT_CRON,
                                  prompt=heartbeat.PROMPT, recurring=True,
                                  durable=True, last_fired=last_fired)


def test_ensure_registered_is_idempotent(isolated):
    first = heartbeat.ensure_registered()
    second = heartbeat.ensure_registered()
    assert first.id == second.id == heartbeat.JOB_ID
    with cron_scheduler.cron_lock:
        matches = [job for job in cron_scheduler.scheduled_jobs.values()
                   if job.id == heartbeat.JOB_ID]
    assert len(matches) == 1
    assert matches[0].durable and matches[0].recurring
    assert matches[0].prompt == heartbeat.PROMPT


def test_ensure_registered_keeps_user_edited_cron(isolated):
    with cron_scheduler.cron_lock:
        cron_scheduler.scheduled_jobs[heartbeat.JOB_ID] = _job()
    heartbeat.ensure_registered(cron='0 18 * * *')
    assert cron_scheduler.scheduled_jobs[heartbeat.JOB_ID].cron == heartbeat.DEFAULT_CRON


def test_switch_disables_registration(isolated, monkeypatch):
    monkeypatch.setenv('HARNESS_HEARTBEAT', '0')
    assert heartbeat.ensure_registered() is None
    assert heartbeat.JOB_ID not in cron_scheduler.scheduled_jobs


def test_build_summary_counts_tasks_changes_and_approvals(isolated):
    task_root = isolated / 'tasks'
    task_root.mkdir()
    for index, status in enumerate(
            ['running', 'queued', 'failed', 'completed', 'completed', 'interrupted']):
        (task_root / f'task_{index}.json').write_text(
            json.dumps({'id': f'task_{index}', 'status': status}), encoding='utf-8')
    (task_root / 'task_broken.json').write_text('{not json', encoding='utf-8')
    workspace = isolated / 'ws'
    workspace.mkdir()

    pending = [SimpleNamespace(tool='bash'), SimpleNamespace(tool='bash'),
               SimpleNamespace(tool='write')]
    text = heartbeat.build_summary(
        datetime(2026, 10, 25, 9, 0), workspace=workspace,
        task_root=task_root, pending=pending)

    assert text.startswith('🫀 每日摘要 · 2026-10-25 09:00')
    assert '任务板：运行 2 · 失败 1 · 完成 2 · 其他 1（共 6）' in text
    assert '不是 git 工作区' in text
    assert '待审批：3 项（bash 2 · write 1）' in text
    # 只读摘要：不出现任务命令正文
    assert 'task_broken' not in text


def test_build_summary_without_activity(isolated):
    text = heartbeat.build_summary(datetime(2026, 10, 25, 9, 0),
                                   workspace=isolated, task_root=isolated / 'none',
                                   pending=[])
    assert '任务板：无后台任务' in text
    assert '待审批：无' in text


def test_run_due_queues_once_per_occurrence(isolated):
    job = _job()
    first = heartbeat.run_due(job, datetime(2026, 10, 25, 9, 0),
                              workspace=isolated, task_root=isolated)
    assert 'occurrence' in first and 'text' in first

    second = heartbeat.run_due(job, datetime(2026, 10, 25, 9, 1),
                               workspace=isolated, task_root=isolated)
    assert second['skipped'] == 'already-generated'
    assert len(heartbeat.pending()) == 1
    assert heartbeat.pending()[0]['occurrence_id'] == 'heartbeat:2026-10-25T09:00'


def test_delivery_ack_roundtrip_and_outbox_limit(isolated):
    for minute in range(heartbeat.OUTBOX_LIMIT + 3):
        heartbeat.queue_summary(f'heartbeat:2026-10-25T09:{minute:02d}', f'no.{minute}')
    items = heartbeat.pending()
    assert len(items) == heartbeat.OUTBOX_LIMIT
    newest = items[-1]['occurrence_id']
    assert heartbeat.ack(newest) is True
    assert heartbeat.ack(newest) is False
    assert newest not in [item['occurrence_id'] for item in heartbeat.pending()]


def test_occurrence_uses_fire_minute(isolated):
    job = _job(last_fired='2026-10-25 09:00')
    # 队列晚一分钟消费也归属同一 occurrence，不会生成第二份
    assert heartbeat.occurrence_id(job, datetime(2026, 10, 25, 9, 1)) \
        == 'heartbeat:2026-10-25T09:00'
    assert heartbeat.occurrence_id(_job(last_fired=''), datetime(2026, 10, 25, 9, 3)) \
        == 'heartbeat:2026-10-25T09:03'


def test_cron_turn_generates_heartbeat_without_calling_model(isolated, monkeypatch):
    """心跳 job 在回合入口被拦截：本地摘要入 outbox，不占用模型回合。"""
    import harness

    monkeypatch.setenv('HARNESS_TASK_ROOT', str(isolated / 'tasks'))
    calls = []
    monkeypatch.setattr(harness, 'agent_loop', lambda *a, **k: calls.append(a))

    job = heartbeat.ensure_registered(cron='* * * * *')
    fired = cron_scheduler.tick(datetime.now())
    assert [item.id for item in fired] == [job.id]

    harness.run_agent_turn_locked()
    assert calls == [], '心跳不能作为 prompt 送进模型'
    assert [item['occurrence_id'] for item in heartbeat.pending()] \
        == ['heartbeat:' + job.last_fired.replace(' ', 'T')]


def test_regular_cron_is_still_sent_to_model_alongside_heartbeat(isolated, monkeypatch):
    import harness

    monkeypatch.setenv('HARNESS_TASK_ROOT', str(isolated / 'tasks'))
    calls = []
    monkeypatch.setattr(harness, 'agent_loop',
                        lambda messages, **kwargs: calls.append(messages))

    heartbeat.ensure_registered(cron='* * * * *')
    regular = cron_scheduler.schedule_job('* * * * *', 'do work', durable=False)
    fired = cron_scheduler.tick(datetime.now())
    assert {item.id for item in fired} == {heartbeat.JOB_ID, regular.id}

    harness.run_agent_turn_locked()
    assert len(calls) == 1
    assert calls[0] == [{'role': 'user', 'content': '[Scheduled] do work'}]
    assert heartbeat.pending()


def test_heartbeat_api_lists_and_acks(isolated, tmp_path):
    from fastapi.testclient import TestClient

    from agent_runtime import AgentRuntime
    from harness_api import create_app
    from session_store import SessionEventStore

    heartbeat.queue_summary('heartbeat:2026-10-25T09:00', '🫀 每日摘要 · 测试')
    runtime = AgentRuntime(
        run_turn=lambda messages, **kwargs: 'ok',
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path / 'sessions')
    app = create_app(runtime, api_token='t', task_root=tmp_path / 'tasks',
                     workspace=tmp_path)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer t'}

    listed = client.get('/v1/heartbeat', headers=headers)
    assert listed.status_code == 200
    assert listed.json()['pending'][0]['occurrence_id'] == 'heartbeat:2026-10-25T09:00'

    url = '/v1/heartbeat/heartbeat:2026-10-25T09:00/ack'
    assert client.post(url, headers=headers).status_code == 200
    assert heartbeat.pending() == []
    assert client.post(url, headers=headers).status_code == 404


def test_heartbeat_routes_require_token(isolated, tmp_path):
    from fastapi.testclient import TestClient

    from agent_runtime import AgentRuntime
    from harness_api import create_app
    from session_store import SessionEventStore

    runtime = AgentRuntime(
        run_turn=lambda messages, **kwargs: 'ok',
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path / 'sessions')
    client = TestClient(create_app(runtime, api_token='t',
                                   task_root=tmp_path / 'tasks',
                                   workspace=tmp_path))
    assert client.get('/v1/heartbeat').status_code == 401

