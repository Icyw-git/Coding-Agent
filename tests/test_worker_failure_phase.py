"""worker 故障要按「请求走到哪一步」分类，不能按异常文案猜（R0-a 第一条）。

设计契约（next-design §4.1）：

    启动前明确未发 / 可能部分写入 / 已发等待回复
    只有**明确未提交**才允许副作用操作降级（重跑独立进程）
    write/flush 报错也可能已部分提交

现状是 `desktop_control_windows._is_worker_failure()` 按异常文本匹配关键字，而名单里
有 `'timed out'`——一次响应超时会被判成"worker 不可用"，于是降级重跑一次独立进程，
把**可能已经发出**的整批动作再执行一遍。这是本轮要修的第一条缺陷。
"""

import pytest

import desktop_control_windows as desktop
import ps_worker


def test_worker_error_carries_a_phase():
    assert ps_worker.WorkerError('x').phase == 'sent_unknown'      # 默认保守
    assert ps_worker.WorkerError('x', phase='not_sent').phase == 'not_sent'
    # 不认识的阶段名一律按"已发出、结果未知"处理，不能当成可重试
    assert ps_worker.WorkerError('x', phase='whatever').phase == 'sent_unknown'


def _patch_worker(monkeypatch, exc):
    monkeypatch.setattr(desktop, '_run_worker', lambda *a, **k: (_ for _ in ()).throw(exc))
    calls = []
    monkeypatch.setattr(desktop, '_run_standalone',
                        lambda payload, **kwargs: calls.append(dict(payload)) or {'ok': True})
    return calls


@pytest.mark.parametrize('action', ['click', 'sequence', 'type', 'key'])
def test_side_effect_does_not_fall_back_when_the_request_may_have_been_sent(
        monkeypatch, action):
    """超时/EOF/非 JSON：请求可能已经执行，**不许**再起一个进程重跑。"""
    calls = _patch_worker(monkeypatch,
                          ps_worker.WorkerError('worker timed out after 5s',
                                                phase='sent_unknown'))
    with pytest.raises(desktop.DesktopControlError) as excinfo:
        desktop._run({'action': action})
    assert calls == [], f'已经降级重跑了一次：{calls}'
    message = str(excinfo.value)
    assert '未知' in message or 'unknown' in message.lower(), message


def test_side_effect_does_fall_back_when_nothing_was_sent(monkeypatch):
    """worker 起不来（明确未发）：沿用原来的降级，不影响可用性。"""
    calls = _patch_worker(monkeypatch,
                          ps_worker.WorkerError('desktop worker is not running',
                                                phase='not_sent'))
    desktop._run({'action': 'click'})
    assert calls == [{'action': 'click'}], calls


def test_read_only_may_be_requeried_after_a_timeout(monkeypatch):
    """只读操作重查不产生副作用，允许降级（设计文档：只读可按重试分类重新查询）。"""
    calls = _patch_worker(monkeypatch,
                          ps_worker.WorkerError('worker timed out after 5s',
                                                phase='sent_unknown'))
    desktop._run({'action': 'snapshot'})
    assert calls == [{'action': 'snapshot'}], calls


def test_structured_rejection_is_never_retried(monkeypatch):
    """worker 正常回答了 ok:false（窗口变了/坐标越界）：重跑只会得到同样的答案。"""
    calls = _patch_worker(monkeypatch,
                          ps_worker.WorkerError('out_of_window: target moved',
                                                phase='answered'))
    with pytest.raises(desktop.DesktopControlError):
        desktop._run({'action': 'click'})
    assert calls == [], calls


def test_partial_write_is_not_retried_for_side_effects(monkeypatch):
    """写请求时出错（可能已部分提交）：同样不能重跑。"""
    calls = _patch_worker(monkeypatch,
                          ps_worker.WorkerError('stdin closed: pipe broken',
                                                phase='partial_write'))
    with pytest.raises(desktop.DesktopControlError):
        desktop._run({'action': 'type'})
    assert calls == [], calls
