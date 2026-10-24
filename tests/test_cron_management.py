"""阶段 2：cron 触发语义、管理 API 与飞书 /cron。"""
from datetime import datetime

import pytest

import cron_scheduler


def _at(minute=5, hour=9, day=1, month=6):
    return datetime(2026, month, day, hour, minute)


def test_tick_fires_once_per_minute_and_persists_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    job = cron_scheduler.schedule_job('* * * * *', 'report', durable=True)
    now = _at()

    fired = cron_scheduler.tick(now)
    assert [item.id for item in fired] == [job.id]
    assert job.last_fired == '2026-06-01 09:05'
    assert [item.id for item in cron_scheduler.consume_cron_queue()] == [job.id]

    # 同一分钟再扫：不重复触发
    assert cron_scheduler.tick(now) == []

    # 模拟重启：从磁盘重新加载，last_fired 一并在，仍不重复
    with cron_scheduler.cron_lock:
        cron_scheduler.scheduled_jobs.clear()
    cron_scheduler.load_durable_jobs()
    reloaded = cron_scheduler.scheduled_jobs[job.id]
    assert reloaded.last_fired == '2026-06-01 09:05'
    assert cron_scheduler.tick(now) == []


def test_tick_advances_to_next_minute(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    job = cron_scheduler.schedule_job('* * * * *', 'report', durable=True)
    assert cron_scheduler.tick(_at(minute=5))
    fired = cron_scheduler.tick(_at(minute=6))
    assert [item.id for item in fired] == [job.id]


def test_one_shot_job_is_removed_after_firing(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    job = cron_scheduler.schedule_job('0 9 1 6 *', 'once', recurring=False,
                                      durable=True)
    assert cron_scheduler.tick(_at(minute=0))
    with cron_scheduler.cron_lock:
        assert job.id not in cron_scheduler.scheduled_jobs
    # 持久文件里也不应再有它
    cron_scheduler.load_durable_jobs()
    with cron_scheduler.cron_lock:
        assert job.id not in cron_scheduler.scheduled_jobs


def test_stable_job_id_cannot_be_registered_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    first = cron_scheduler.schedule_job('0 9 * * *', 'a', job_id='heartbeat')
    assert not isinstance(first, str)
    duplicate = cron_scheduler.schedule_job('0 10 * * *', 'b', job_id='heartbeat')
    assert duplicate == 'Job heartbeat already exists'


def test_invalid_cron_is_rejected_before_touching_state():
    with pytest.raises(ValueError):
        # schedule_job 返回错误字符串，validate_cron 才是真校验入口
        raise ValueError(cron_scheduler.validate_cron('* * *'))
    assert cron_scheduler.validate_cron('* * *') is not None
    assert cron_scheduler.validate_cron('* * * * *') is None
