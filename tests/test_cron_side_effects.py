"""定时任务的副作用授权：默认只读、显式开放、后台回合不读 stdin。"""
from datetime import datetime

import pytest

import cron_scheduler
import harness
import heartbeat
import hooks


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    root = tmp_path / 'ws'
    root.mkdir()

    class Config:
        DENY_LIST = ['sudo']
        DESTRUCTIVE = ['rm ', 'del ']

        @staticmethod
        def get_workdir():
            return root

        @staticmethod
        def active_checkpoint_manager():
            return None

    monkeypatch.setattr(hooks, '_h', lambda: Config)
    monkeypatch.setattr(hooks.GATE, 'backend', 'remote')
    monkeypatch.setattr(hooks.GATE, 'timeout', 0.2)
    hooks.GATE.clear()
    return root


def test_job_defaults_to_read_only_and_survives_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scheduler, 'DURABLE_PATH', tmp_path / 'crons.json')
    default = cron_scheduler.schedule_job('0 9 * * *', 'report')
    writable = cron_scheduler.schedule_job('0 10 * * *', 'patch',
                                           allow_side_effects=True)
    assert default.allow_side_effects is False
    assert writable.allow_side_effects is True

    cron_scheduler.load_durable_jobs()
    with cron_scheduler.cron_lock:
        assert cron_scheduler.scheduled_jobs[default.id].allow_side_effects is False
        assert cron_scheduler.scheduled_jobs[writable.id].allow_side_effects is True


def test_read_only_turn_denies_writes_network_and_coordination(workspace):
    with hooks.use_scheduled_turn(read_only=True):
        assert hooks.scheduled_read_only() is True
        # 工作区内的写文件在默认档是 ALLOW；只读任务必须升级成拒绝。
        assert 'scheduled task is read-only' in hooks.enforce(
            'write', {'path': 'notes.md', 'content': 'x'}, interactive=True)
        assert 'scheduled task is read-only' in hooks.enforce(
            'bash', {'command': 'touch marker'}, interactive=False)
        assert 'scheduled task is read-only' in hooks.enforce(
            'web_fetch', {'url': 'https://example.com'}, interactive=False)
        assert 'scheduled task is read-only' in hooks.enforce(
            'screen_capture', {'screen': 'primary'}, interactive=True)
        assert 'scheduled task is read-only' in hooks.enforce(
            'screen_ocr', {}, interactive=False)
        assert 'scheduled task is read-only' in hooks.enforce(
            'task', {'description': 'write it'}, interactive=False)
        # 会话元数据与授权也不许后台回合改：不经过文件/命令策略，必须显式列入。
        assert 'scheduled task is read-only' in hooks.enforce(
            'rename_session', {'title': 'x'}, interactive=False)
        assert 'scheduled task is read-only' in hooks.enforce(
            'revoke_permissions', {}, interactive=False)
        # 只读操作照常放行：读文件、只读命令、只读工具。
        assert hooks.enforce('read', {'path': 'notes.md'}, interactive=False) is None
        assert hooks.enforce('bash', {'command': 'ls'}, interactive=False) is None
        assert hooks.enforce('todo_read', {}, interactive=False) is None

    # 离开定时回合后恢复默认档：工作区内写文件不再被拦。
    assert hooks.scheduled_read_only() is False
    assert hooks.enforce('write', {'path': 'notes.md', 'content': 'x'},
                         interactive=True) is None


def test_side_effect_job_approves_through_queue_not_stdin(workspace, monkeypatch):
    asked = []

    def queued(tool, args, reason, **kw):
        asked.append((tool, reason))
        return True, 'approved'

    def stdin(*_args, **_kwargs):
        raise AssertionError('定时回合不能读 stdin')

    monkeypatch.setattr(hooks.GATE, 'decide', stdin)
    monkeypatch.setattr(hooks.GATE, 'decide_queued', queued)

    with hooks.use_scheduled_turn(read_only=False):
        assert hooks.scheduled_read_only() is False
        # permission_hook 在定时回合里必须走队列；用需要审批的命令验证，
        # 工作区内普通写文件在默认档本来就不询问。
        assert hooks.permission_hook('bash', {'command': 'rm old.txt'}) is None
    assert [tool for tool, _ in asked] == ['bash']


def test_cron_turn_runs_in_strictest_mode(monkeypatch):
    calls = []

    def fake_loop(messages, **kwargs):
        calls.append((messages, hooks.scheduled_read_only()))

    monkeypatch.setattr(harness, 'agent_loop', fake_loop)
    read_only_job = cron_scheduler.schedule_job('* * * * *', 'readonly', durable=False)
    writable_job = cron_scheduler.schedule_job('* * * * *', 'writable',
                                               durable=False,
                                               allow_side_effects=True)
    cron_scheduler.tick(datetime.now())
    assert {job.id for job in cron_scheduler.cron_queue} \
        == {read_only_job.id, writable_job.id}

    harness.run_agent_turn_locked()
    assert len(calls) == 1
    # 混批按最严档：不能因为同批有可写任务就给只读任务放行。
    assert calls[0][1] is True
    assert 'readonly' in calls[0][0][0]['content']
    assert 'writable' in calls[0][0][0]['content']


def test_writable_only_turn_is_not_forced_read_only(monkeypatch):
    modes = []
    monkeypatch.setattr(harness, 'agent_loop',
                        lambda messages, **kwargs: modes.append(
                            hooks.scheduled_read_only()))
    cron_scheduler.schedule_job('* * * * *', 'writable', durable=False,
                                allow_side_effects=True)
    cron_scheduler.tick(datetime.now())
    harness.run_agent_turn_locked()
    assert modes == [False]


def test_running_turn_defers_read_only_jobs_but_runs_heartbeat(monkeypatch):
    ran = []
    monkeypatch.setattr(heartbeat, 'run_due',
                        lambda job, **kwargs: ran.append(job.id))

    readonly = cron_scheduler.schedule_job('* * * * *', 'readonly', durable=False)
    writable = cron_scheduler.schedule_job('* * * * *', 'writable', durable=False,
                                           allow_side_effects=True)
    cron_scheduler.tick(datetime.now())
    hb_job = cron_scheduler.CronJob(id=heartbeat.JOB_ID, cron='* * * * *',
                                    prompt=heartbeat.PROMPT, recurring=True,
                                    durable=False)
    with cron_scheduler.cron_lock:
        cron_scheduler.cron_queue.append(hb_job)

    claimed = harness._consume_cron_for_running_turn()
    assert [job.id for job in claimed] == [writable.id]
    assert ran == [heartbeat.JOB_ID]
    with cron_scheduler.cron_lock:
        assert [job.id for job in cron_scheduler.cron_queue] == [readonly.id]


def test_read_only_scheduled_turn_consumes_only_heartbeat(monkeypatch):
    monkeypatch.setattr(heartbeat, 'run_due', lambda job, **kwargs: None)
    writable = cron_scheduler.schedule_job('* * * * *', 'writable', durable=False,
                                           allow_side_effects=True)
    hb_job = cron_scheduler.CronJob(id=heartbeat.JOB_ID, cron='* * * * *',
                                    prompt=heartbeat.PROMPT, recurring=True,
                                    durable=False)
    with cron_scheduler.cron_lock:
        cron_scheduler.cron_queue.extend([writable, hb_job])

    with hooks.use_scheduled_turn(read_only=True):
        assert harness._consume_cron_for_running_turn() == []
    with cron_scheduler.cron_lock:
        assert [job.id for job in cron_scheduler.cron_queue] == [writable.id]
