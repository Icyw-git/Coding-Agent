"""定时任务结果投递：outbox 语义、回合收尾、失败隔离与来源聊天继承。"""
import threading
from datetime import datetime

import pytest

import cron_delivery
import cron_scheduler
import harness
import hooks
import tool_tasks


@pytest.fixture(autouse=True)
def _isolated_outbox(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_delivery, 'OUTBOX_PATH', tmp_path / 'cron_outbox.json')
    return tmp_path / 'cron_outbox.json'


def _schedule(*args, **kwargs):
    return cron_scheduler.schedule_job(*args, durable=False, **kwargs)


def test_outbox_dedupes_acks_and_caps():
    first = cron_delivery.queue('chat-1', 'hello', job_id='j1',
                                occurrence='2026-10-25T09:00')
    again = cron_delivery.queue('chat-1', 'changed', job_id='j1',
                                occurrence='2026-10-25T09:00')
    assert again['id'] == first['id']
    assert [item['text'] for item in cron_delivery.pending()] == ['hello']

    for index in range(cron_delivery.OUTBOX_LIMIT + 3):
        cron_delivery.queue('chat-1', f'item-{index}', job_id=f'j{index}',
                            occurrence=f'o{index}')
    pending = cron_delivery.pending()
    assert len(pending) == cron_delivery.OUTBOX_LIMIT
    assert all(item['text'].startswith('item-') for item in pending)

    newest = pending[-1]['id']
    assert cron_delivery.ack(newest) is True
    assert cron_delivery.ack(newest) is False
    assert all(item['id'] != newest for item in cron_delivery.pending())


def test_blank_target_or_text_is_silent():
    assert cron_delivery.queue('', 'text') is None
    assert cron_delivery.queue('chat-1', '   ') is None
    assert cron_delivery.pending() == []


def test_long_text_is_truncated():
    entry = cron_delivery.queue('chat-1', 'x' * (cron_delivery.MAX_TEXT_CHARS + 50),
                                job_id='j', occurrence='o')
    assert entry['text'].endswith('（内容过长，已截断）')
    assert len(entry['text']) <= cron_delivery.MAX_TEXT_CHARS + 20


def test_cron_turn_delivers_answer_to_origin_chat(monkeypatch):
    monkeypatch.setattr(harness, 'agent_loop',
                        lambda messages, **kwargs: '今日进展：一切正常')
    job = _schedule('* * * * *', 'report', deliver_to='chat-1')
    cron_scheduler.tick(datetime.now())

    harness.run_agent_turn_locked()

    pending = cron_delivery.pending()
    assert len(pending) == 1
    assert pending[0]['chat_id'] == 'chat-1'
    assert '今日进展：一切正常' in pending[0]['text']
    assert job.id in pending[0]['text']


def test_cron_turn_failure_is_reported_and_swallowed(monkeypatch):
    def boom(messages, **kwargs):
        raise RuntimeError('model exploded')

    monkeypatch.setattr(harness, 'agent_loop', boom)
    _schedule('* * * * *', 'report', deliver_to='chat-1')
    cron_scheduler.tick(datetime.now())

    harness.run_agent_turn_locked()   # 不能把异常抛给 cron-worker

    text = cron_delivery.pending()[0]['text']
    assert '定时任务失败' in text and 'model exploded' in text
    # 定时回合上下文在异常后也必须复位。
    assert hooks.scheduled_turn() is False


def test_cron_turn_without_target_does_not_queue(monkeypatch):
    monkeypatch.setattr(harness, 'agent_loop',
                        lambda messages, **kwargs: 'silent result')
    _schedule('* * * * *', 'report')
    cron_scheduler.tick(datetime.now())

    harness.run_agent_turn_locked()

    assert cron_delivery.pending() == []


def test_targeted_jobs_wait_for_their_own_turn(monkeypatch):
    job = _schedule('* * * * *', 'report', deliver_to='chat-1',
                    allow_side_effects=True)
    cron_scheduler.tick(datetime.now())

    assert harness._consume_cron_for_running_turn() == []
    with cron_scheduler.cron_lock:
        assert [queued.id for queued in cron_scheduler.cron_queue] == [job.id]


def test_worker_survives_a_failing_turn(monkeypatch):
    monkeypatch.setattr(cron_scheduler, 'agent_lock', threading.RLock())
    with cron_scheduler.cron_lock:
        cron_scheduler.cron_queue.extend([
            cron_scheduler.CronJob(id=f'j{i}', cron='* * * * *', prompt='p',
                                   recurring=True, durable=False)
            for i in range(3)
        ])
    calls = []
    stop = threading.Event()

    def run_turn():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('first turn fails')
        if len(calls) >= 2:
            stop.set()

    thread = threading.Thread(
        target=cron_scheduler.queue_processor_loop,
        args=(run_turn, 0.01, stop), daemon=True)
    thread.start()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert len(calls) >= 2   # 第一次失败后循环仍在跑


def test_schedule_cron_inherits_origin_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')

    with hooks.use_origin_chat('chat-9'):
        output = tool_tasks.run_schedule_cron('* * * * *', 'reminder')
    with cron_scheduler.cron_lock:
        jobs = list(cron_scheduler.scheduled_jobs.values())
    assert jobs[-1].deliver_to == 'chat-9'
    assert '结果推送到当前聊天' in output

    tool_tasks.run_schedule_cron('* * * * *', 'silent')
    with cron_scheduler.cron_lock:
        assert list(cron_scheduler.scheduled_jobs.values())[-1].deliver_to == ''
    assert hooks.current_origin_chat() == ''


def test_runtime_send_propagates_origin_chat(tmp_path):
    from agent_runtime import AgentRuntime
    from session_store import SessionEventStore

    seen = []

    def run_turn(messages, **kwargs):
        seen.append(hooks.current_origin_chat())
        return 'ok'

    runtime = AgentRuntime(run_turn=run_turn,
                           session_store_cls=SessionEventStore,
                           sessions_dir=tmp_path / 'sessions')
    session_id = runtime.create_session()
    runtime.send(session_id, 'hello', origin_chat='chat-9')

    assert seen == ['chat-9']
    assert hooks.current_origin_chat() == ''
