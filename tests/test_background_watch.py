"""background_watch：/watch 列表、跟踪与超时/中断边界。"""
import background_watch


class _Store:
    def __init__(self, task=None, transitions=None):
        self.task = task
        self.transitions = list(transitions or [])

    def list_background_tasks(self):
        return [dict(self.task)] if self.task else []

    def background_snapshot(self, bg_id):
        if self.task is None or self.task['id'] != bg_id:
            return None
        if self.transitions:
            self.task.update(self.transitions.pop(0))
        return dict(self.task)

    def read_background_result(self, bg_id):
        return dict(self.task), self.task.get('result', '')


def _task(**overrides):
    task = {'id': 'bg_0001', 'status': 'running', 'command': 'pytest -q',
            'created_at': 100.0, 'ended_at': None}
    task.update(overrides)
    return task


def test_format_tasks_empty_and_listing():
    assert '没有后台任务' in background_watch.format_tasks(_Store())

    store = _Store(_task(status='completed', ended_at=160.0,
                         result='done'))
    text = background_watch.format_tasks(store)

    assert 'bg_0001' in text and 'completed' in text
    assert '01:00' in text          # 结束时间 - 开始时间
    assert '--timeout' in text


def test_watch_finished_task_returns_output_immediately():
    store = _Store(_task(status='completed', ended_at=130.0, result='ok'))

    text = background_watch.watch('bg_0001', store, timeout=1)

    assert '[bg_0001] completed' in text
    assert '总用时 00:30' in text
    assert 'ok' in text


def test_watch_follows_running_until_terminal():
    store = _Store(_task(), transitions=[
        {},   # 第一次仍是 running
        {'status': 'completed', 'ended_at': 102.0, 'result': 'built'},
    ])
    emitted = []

    text = background_watch.watch('bg_0001', store, timeout=10,
                                  interval=0.01, emit=emitted.append,
                                  sleep=lambda seconds: None)

    assert emitted and 'running' in emitted[0]
    assert 'built' in text and 'completed' in text


def test_watch_times_out_and_keeps_task_running():
    store = _Store(_task())
    state = {'now': 0.0}

    def clock():
        return state['now']

    def sleep(seconds):
        state['now'] += 1.0

    text = background_watch.watch('bg_0001', store, timeout=0.5,
                                  interval=0.1, clock=clock, sleep=sleep)

    assert '仍在 running' in text
    assert store.task['status'] == 'running'


def test_watch_keyboard_interrupt_detaches():
    store = _Store(_task())

    def sleep(seconds):
        raise KeyboardInterrupt

    text = background_watch.watch('bg_0001', store, timeout=10,
                                  interval=0.01, sleep=sleep)

    assert '已停止跟踪' in text


def test_watch_unknown_task():
    text = background_watch.watch('bg_9999', _Store(), timeout=1)

    assert '未知后台任务' in text


def test_watch_command_parsing():
    store = _Store(_task(status='completed', ended_at=101.0, result='x'))

    assert 'bg_0001' in background_watch.watch_command('bg_0001', store)
    assert '用法' in background_watch.watch_command('bg_0001 --force', store)
    assert '秒数' in background_watch.watch_command('bg_0001 --timeout x', store)
    assert '大于 0' in background_watch.watch_command('bg_0001 --timeout 0', store)


def test_watch_command_defaults_to_latest_task_without_id():
    store = _Store(_task(status='completed', ended_at=101.0, result='latest'))

    # 不需要输入编号：直接跟踪最新任务（已结束则立即输出全文）。
    text = background_watch.watch_command('', store)

    assert 'latest' in text and 'bg_0001' in text


def test_watch_command_empty_store_prints_list_hint():
    assert '没有后台任务' in background_watch.watch_command('', _Store())


def test_watch_command_list_flag_shows_tasks():
    store = _Store(_task(status='completed', ended_at=101.0, result='x'))

    text = background_watch.watch_command('--list', store)

    assert 'bg_0001' in text and 'completed' in text


def test_watch_command_timeout_without_id():
    store = _Store(_task())

    text = background_watch.watch_command('--timeout 0.001', store)

    assert '仍在 running' in text


def test_watch_command_routes_progress_through_emit(monkeypatch):
    store = _Store(_task())
    captured = {}

    def fake_watch(bg_id, store=None, **kwargs):
        captured['emit'] = kwargs.get('emit')
        kwargs['emit']('  ● bg_0001  running  00:01  pytest -q')
        return '  [bg_0001] completed'

    monkeypatch.setattr(background_watch, 'watch', fake_watch)
    emitted = []

    text = background_watch.watch_command('bg_0001 --timeout 5', store,
                                          emit=emitted.append)

    assert text == '  [bg_0001] completed'
    assert emitted == ['  ● bg_0001  running  00:01  pytest -q']


def test_watch_command_extra_running_note_uses_emit(monkeypatch):
    class _MultiStore:
        def list_background_tasks(self):
            return [_task(), _task(id='bg_0002', command='sleep 5')]

    monkeypatch.setattr(background_watch, 'watch', lambda *args, **kwargs: 'ok')
    emitted = []

    assert background_watch.watch_command('', _MultiStore(),
                                          emit=emitted.append) == 'ok'
    assert '另有 1 个任务在运行' in emitted[0]
