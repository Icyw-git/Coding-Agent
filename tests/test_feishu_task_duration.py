"""后台任务卡片的用时。

`feishu_gateway._format_task_duration` 有完整实现和 docstring——「排队中 → 动态已用时
（进行中）→ 总用时；旧记录缺 duration_ms 时按 started_at/ended_at 动态计算」——但
**没有任何调用方**，卡片实际走的是 `feishu_task_card._duration` 的简化版：只认
`duration_s` / `elapsed_s` / `duration_ms`，都没有就返回 `—`。

`TaskRecord` 的运行中任务 `duration_ms` 就是 `None`，所以飞书任务卡片上**正在跑的任务
显示「用时 —」**，而不是已跑多久。
"""

import feishu_task_card


def _card_line(task):
    return feishu_task_card._task_line(task)


def test_running_task_shows_elapsed_not_dash():
    """运行中：按 started_at 算已用时，并说明还在进行中。"""
    import time

    line = _card_line({'id': 'task_x', 'status': 'running',
                       'started_at': time.time() - 125})
    assert '用时 —' not in line, line
    assert '02:05' in line, line
    assert '进行中' in line, line


def test_queued_task_says_queued():
    line = _card_line({'id': 'task_q', 'status': 'queued'})
    assert '排队中' in line, line
    assert '用时 —' not in line, line


def test_finished_task_uses_recorded_duration():
    line = _card_line({'id': 'task_d', 'status': 'succeeded',
                       'started_at': 100.0, 'ended_at': 160.0,
                       'duration_ms': 60000})
    assert '01:00' in line, line
    assert '进行中' not in line, line


def test_unknown_stays_dash():
    """什么时间都没有就老实说不知道——别猜一个数字。"""
    assert '用时 —' in _card_line({'id': 'task_u', 'status': 'succeeded'})


def test_empty_text_fallback_is_chinese():
    """卡片发不出去时走文本兜底，那也是用户读的。"""
    text = feishu_task_card.tasks_text([])
    assert '(no tasks)' not in text, text
    assert '没有后台任务' in text, text
