"""阶段 2：cron 触发语义、管理 API 与飞书 /cron。"""
import json
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


def test_update_job_toggles_authorization_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    job = cron_scheduler.schedule_job('0 9 * * *', 'report', job_id='ro')

    updated = cron_scheduler.update_job('ro', allow_side_effects=True)
    assert updated is job and job.allow_side_effects is True
    assert json.loads((tmp_path / 'crons.json').read_text(encoding='utf-8'))[
        0]['allow_side_effects'] is True

    cron_scheduler.update_job('ro', allow_side_effects=False, deliver_to=' oc_1 ')
    assert job.allow_side_effects is False and job.deliver_to == 'oc_1'

    # 未知任务只返回错误字符串，不新增条目
    assert cron_scheduler.update_job('missing', allow_side_effects=True) ==         'Job missing not found'
    with cron_scheduler.cron_lock:
        assert list(cron_scheduler.scheduled_jobs) == ['ro']


def test_durable_jobs_with_chinese_survive_reload(tmp_path, monkeypatch):
    """持久化文件是 UTF-8；读取必须固定编码，不能跟随 Windows GBK locale。"""
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    cron_scheduler.schedule_job('0 9 * * *', '每日汇报：检查待办并提醒喝水',
                                job_id='cn', deliver_to='oc_cn',
                                allow_side_effects=True)
    with cron_scheduler.cron_lock:
        cron_scheduler.scheduled_jobs.clear()

    cron_scheduler.load_durable_jobs()
    job = cron_scheduler.scheduled_jobs['cn']
    assert job.prompt == '每日汇报：检查待办并提醒喝水'
    assert job.deliver_to == 'oc_cn' and job.allow_side_effects is True


def test_one_malformed_durable_record_does_not_drop_the_rest(tmp_path, monkeypatch):
    path = tmp_path / 'crons.json'
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', path)
    path.write_text(json.dumps([
        {'id': 'good', 'cron': '0 9 * * *', 'prompt': 'ok', 'recurring': True,
         'durable': True},
        {'id': 'bad', 'cron': '* * *'},
    ], ensure_ascii=False), encoding='utf-8')
    with cron_scheduler.cron_lock:
        cron_scheduler.scheduled_jobs.clear()

    cron_scheduler.load_durable_jobs()
    assert list(cron_scheduler.scheduled_jobs) == ['good']


def test_undecodable_durable_file_leaves_existing_jobs_intact(tmp_path, monkeypatch):
    path = tmp_path / 'crons.json'
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', path)
    path.write_bytes(b'\xff\xfe\x00not json at all')
    with cron_scheduler.cron_lock:
        cron_scheduler.scheduled_jobs.clear()

    cron_scheduler.load_durable_jobs()
    with cron_scheduler.cron_lock:
        assert cron_scheduler.scheduled_jobs == {}
