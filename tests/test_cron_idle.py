"""P2d：定时任务的空闲门控（到点不空闲 → 延后 → 空闲补跑一次）。

调度测试全部注入 idle_probe，不启动 PowerShell；API 用独立 router，
不碰真实服务与鉴权中间件。
"""

import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

import api_crons
import cron_scheduler
import feishu_cron_card
import gateway_sim
import tool_schemas
import tool_tasks


def _at(minute=0, hour=9, day=1, month=6):
    return datetime(2026, month, day, hour, minute)


def _snap(seconds, *, locked=False, available=True):
    return {'available': available, 'idle_seconds': seconds, 'locked': locked}


@pytest.fixture(autouse=True)
def _reset_idle_cache():
    cron_scheduler.reset_idle_cache()
    yield
    cron_scheduler.reset_idle_cache()


def _job(cron='0 9 1 6 *', *, recurring=True, idle=600, durable=True):
    job = cron_scheduler.schedule_job(cron, 'nightly report', recurring=recurring,
                                      durable=durable, idle_after_s=idle)
    assert not isinstance(job, str)
    return job


# ---------- 调度语义 ----------


def test_idle_job_fires_immediately_when_already_idle():
    job = _job()

    fired = cron_scheduler.tick(_at(), idle_probe=lambda: _snap(900))

    assert [item.id for item in fired] == [job.id]
    assert job.deferred_since is None


def test_busy_machine_defers_and_does_not_fire():
    job = _job()

    fired = cron_scheduler.tick(_at(), idle_probe=lambda: _snap(30))

    assert fired == []
    assert job.deferred_since == _at().timestamp()
    assert job.last_fired == ''
    assert cron_scheduler.consume_cron_queue() == []


def test_deferred_job_runs_once_after_idle_even_outside_cron_minute():
    job = _job(idle=600)
    cron_scheduler.tick(_at(), idle_probe=lambda: _snap(30))

    # 09:30——cron 已不再匹配（09:00 那分钟），但空闲够了要补跑
    fired = cron_scheduler.tick(_at(minute=30), idle_probe=lambda: _snap(600))

    assert [item.id for item in fired] == [job.id]
    assert job.deferred_since is None
    # 补跑后同一分钟再扫不重复；之后 cron 不再匹配也不再触发
    assert cron_scheduler.tick(_at(minute=30), idle_probe=lambda: _snap(600)) == []
    assert cron_scheduler.tick(_at(minute=31), idle_probe=lambda: _snap(600)) == []


def test_deferred_one_shot_is_removed_after_catch_up():
    job = _job(recurring=False)
    cron_scheduler.tick(_at(), idle_probe=lambda: _snap(0))
    assert job.id in cron_scheduler.scheduled_jobs

    cron_scheduler.tick(_at(minute=20), idle_probe=lambda: _snap(600))

    assert job.id not in cron_scheduler.scheduled_jobs


def test_locked_machine_counts_as_idle():
    job = _job(idle=600)

    fired = cron_scheduler.tick(_at(), idle_probe=lambda: _snap(5, locked=True))

    assert [item.id for item in fired] == [job.id]


def test_unavailable_probe_is_treated_as_busy():
    job = _job()
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError('probe failed')

    fired = cron_scheduler.tick(_at(), idle_probe=broken)

    assert fired == []
    assert job.deferred_since is not None
    assert calls == [1]


def test_explicit_unavailable_snapshot_defers():
    job = _job()

    cron_scheduler.tick(_at(), idle_probe=lambda: _snap(None, available=False))

    assert job.deferred_since is not None


def test_deferral_expires_after_limit_and_drops_occurrence():
    job = _job(recurring=True)
    cron_scheduler.tick(_at(), idle_probe=lambda: _snap(0))

    fired = cron_scheduler.tick(_at(hour=21, minute=5),
                                idle_probe=lambda: _snap(0))

    assert fired == []
    assert job.deferred_since is None
    assert job.id in cron_scheduler.scheduled_jobs   # 周期任务保留，等下个周期
    # 丢弃后同一分钟不会补跑；即便空闲也不会
    assert cron_scheduler.tick(_at(hour=21, minute=6),
                               idle_probe=lambda: _snap(9999)) == []


def test_expired_one_shot_job_is_removed():
    job = _job(recurring=False)
    cron_scheduler.tick(_at(), idle_probe=lambda: _snap(0))

    cron_scheduler.tick(_at(hour=23), idle_probe=lambda: _snap(0))

    assert job.id not in cron_scheduler.scheduled_jobs


def test_ungated_job_never_probes_idle():
    job = _job(idle=0)
    calls = []

    fired = cron_scheduler.tick(_at(), idle_probe=lambda: calls.append(1) or _snap(0))

    assert [item.id for item in fired] == [job.id]
    assert calls == []


def test_gated_job_not_due_never_probes_idle():
    _job(cron='0 3 * * *')          # 03:00，现在 09:00 不匹配
    calls = []

    assert cron_scheduler.tick(_at(), idle_probe=lambda: calls.append(1) or _snap(0)) == []
    assert calls == []


def test_idle_snapshot_caches_probe_calls(monkeypatch):
    calls = []

    def fake_probe(probe=None):
        calls.append(1)
        return _snap(60)

    monkeypatch.setattr(cron_scheduler, '_probe_idle', fake_probe)
    assert cron_scheduler.idle_snapshot()['idle_seconds'] == 60
    assert cron_scheduler.idle_snapshot()['idle_seconds'] == 60
    assert len(calls) == 1

    cron_scheduler.reset_idle_cache()
    cron_scheduler.idle_snapshot()
    assert len(calls) == 2


def test_explicit_probe_bypasses_cache():
    calls = []

    def probe():
        calls.append(1)
        return _snap(1)

    cron_scheduler.idle_snapshot(probe)
    cron_scheduler.idle_snapshot(probe)
    assert len(calls) == 2


def test_invalid_idle_threshold_is_rejected():
    assert 'idle_after_s must be' in cron_scheduler.schedule_job(
        '0 9 * * *', 'x', idle_after_s=-60)
    assert 'idle_after_s must be' in cron_scheduler.schedule_job(
        '0 9 * * *', 'x', idle_after_s=10 ** 9)
    assert cron_scheduler.schedule_job('0 9 * * *', 'x', idle_after_s='oops').startswith(
        'idle_after_s must be')


def test_update_job_changes_gate_and_clears_pending_deferral():
    job = _job()
    cron_scheduler.tick(_at(), idle_probe=lambda: _snap(0))
    assert job.deferred_since is not None

    cron_scheduler.update_job(job.id, idle_after_s=1200)

    assert job.idle_after_s == 1200
    assert job.deferred_since is None
    assert 'idle_after_s must be' in cron_scheduler.update_job(job.id, idle_after_s=10 ** 6)


# ---------- API ----------


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api_crons.build_router())
    return TestClient(app)


def test_api_create_and_patch_idle_minutes():
    client = _client()
    created = client.post('/v1/crons', json={
        'cron': '0 3 * * *', 'prompt': 'nightly', 'idle_after_minutes': 10})
    assert created.status_code == 201
    body = created.json()
    assert body['idle_after_s'] == 600
    assert body['deferred_since'] is None

    patched = client.patch(f"/v1/crons/{body['id']}",
                           json={'idle_after_minutes': 0})
    assert patched.status_code == 200
    assert patched.json()['idle_after_s'] == 0

    listed = client.get('/v1/crons').json()['jobs']
    assert listed[0]['idle_after_s'] == 0


def test_api_rejects_out_of_range_idle_minutes():
    client = _client()
    response = client.post('/v1/crons', json={
        'cron': '0 3 * * *', 'prompt': 'nightly', 'idle_after_minutes': 5000})
    assert response.status_code == 422


def test_gateway_client_sends_idle_minutes():
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(201, json={'id': 'c1'})

    client = gateway_sim.GatewayClient(
        'http://test', 't', transport=httpx.MockTransport(handler))
    try:
        client.create_cron('0 3 * * *', 'nightly', idle_after_minutes=10)
        client.update_cron('c1', idle_after_minutes=0)
    finally:
        client.close()

    assert payloads[0]['idle_after_minutes'] == 10
    assert payloads[1]['idle_after_minutes'] == 0


# ---------- 工具与展示 ----------


def test_tool_schedule_cron_passes_idle_gate(monkeypatch):
    captured = {}

    def fake_schedule(cron, prompt, recurring, durable, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id='c1')

    monkeypatch.setattr(tool_tasks, 'schedule_job', fake_schedule)
    out = tool_tasks.run_schedule_cron('0 3 * * *', 'nightly',
                                       idle_after_minutes=15)

    assert captured['idle_after_s'] == 900
    assert '空闲 ≥15 分钟' in out


def test_tool_schedule_cron_rejects_bad_idle_value(monkeypatch):
    monkeypatch.setattr(tool_tasks, 'schedule_job',
                        lambda *a, **k: pytest.fail('should not be called'))
    assert tool_tasks.run_schedule_cron(
        '0 3 * * *', 'nightly', idle_after_minutes='soon').startswith('Error:')


def test_tool_list_and_card_show_idle_gate(monkeypatch):
    job = SimpleNamespace(id='c1', cron='0 3 * * *', prompt='nightly',
                          recurring=True, durable=True,
                          allow_side_effects=False, deliver_to='',
                          idle_after_s=600)
    monkeypatch.setattr(tool_tasks, 'scheduled_jobs', {'c1': job})
    assert '[空闲 ≥10 分钟]' in tool_tasks.run_list_crons()

    text = feishu_cron_card.cron_text([{
        'id': 'c1', 'cron': '0 3 * * *', 'prompt': 'nightly',
        'recurring': True, 'allow_side_effects': False, 'idle_after_s': 600}])
    assert '空闲≥10分' in text


def test_schedule_cron_schema_exposes_idle_minutes():
    schema = next(item for item in tool_schemas.TOOL_SCHEMAS
                  if item['function']['name'] == 'schedule_cron')
    prop = schema['function']['parameters']['properties']['idle_after_minutes']
    assert prop['type'] == 'integer'
    assert prop['maximum'] == 1440
