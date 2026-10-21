"""tool_tasks：任务/cron 的输出格式与错误路径（依赖按名字替换）。"""
from types import SimpleNamespace

import tool_tasks


def test_list_tasks_empty(monkeypatch):
    monkeypatch.setattr(tool_tasks, 'list_tasks', lambda: [])

    assert 'No tasks' in tool_tasks.run_list_tasks()


def test_list_tasks_formats_owner_and_dependencies(monkeypatch):
    task = SimpleNamespace(id='T1', subject='build', status='pending',
                           blockedBy=['T0'], owner='bob')
    monkeypatch.setattr(tool_tasks, 'list_tasks', lambda: [task])

    output = tool_tasks.run_list_tasks()

    assert 'T1: build [pending] [bob] (blockedBy: T0)' in output


def test_get_task_missing_returns_error(monkeypatch):
    def missing(_task_id):
        raise FileNotFoundError

    monkeypatch.setattr(tool_tasks, 'get_task', missing)

    assert 'not found' in tool_tasks.run_get_task('T9')


def test_schedule_cron_reports_backend_error(monkeypatch):
    monkeypatch.setattr(tool_tasks, 'schedule_job', lambda *args: 'bad cron')

    assert tool_tasks.run_schedule_cron('nope', 'p') == 'Error: bad cron'


def test_schedule_cron_lists_and_cancels(monkeypatch):
    monkeypatch.setattr(tool_tasks, 'schedule_job',
                        lambda *args: SimpleNamespace(id='c1'))
    assert "Scheduled c1" in tool_tasks.run_schedule_cron('* * * * *', 'p')

    job = SimpleNamespace(id='c1', cron='* * * * *', prompt='p',
                          recurring=True, durable=False)
    monkeypatch.setattr(tool_tasks, 'scheduled_jobs', {'c1': job})
    assert '[recurring, session]' in tool_tasks.run_list_crons()

    monkeypatch.setattr(tool_tasks, 'cancel_job',
                        lambda job_id: f'cancelled {job_id}')
    assert tool_tasks.run_cancel_cron('c1') == 'cancelled c1'


def test_list_crons_empty(monkeypatch):
    monkeypatch.setattr(tool_tasks, 'scheduled_jobs', {})

    assert 'No cron jobs' in tool_tasks.run_list_crons()
