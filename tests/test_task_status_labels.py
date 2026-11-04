"""任务状态的中文名要覆盖两个词表，且只有一个来源。

飞书任务卡片渲染的是 `TaskRecord`（`task_manager`），它的状态集是
`queued / running / succeeded / failed / cancelled / interrupted`；
而卡片里的 `_STATUS_ZH` 只写了 `background_task` 那套
（`running / queued / completed / failed / cancelled`）。

差集的后果是**最常见的那条路径**显示英文：

    _status_zh('succeeded')   → 'succeeded'
    _status_zh('interrupted') → 'interrupted'

`/watch` 更直接：`_status_line` 压根没查表，直接把 `running` / `completed` 印出来。
"""

import background_watch
import feishu_task_card
import task_status


def test_both_vocabularies_are_covered():
    for status in ('queued', 'running', 'succeeded', 'failed',
                   'cancelled', 'interrupted', 'completed'):
        label = task_status.zh(status)
        assert label != status, f'{status} 没有中文名'
        assert label.strip()


def test_unknown_falls_back_to_the_raw_value():
    """不认识的状态要露原值——猜一个中文名会让人以为程序处理了它。"""
    assert task_status.zh('weird') == 'weird'
    assert task_status.zh(None) == '未知'


def test_card_shows_chinese_for_a_successful_task():
    entry = feishu_task_card._task_line(
        {'id': 't1', 'status': 'succeeded', 'duration_ms': 60000})
    assert 'succeeded' not in entry, entry
    assert '成功' in entry or '已完成' in entry, entry


def test_watch_list_shows_chinese_status():
    line = background_watch._status_line(
        {'id': 'bg_1', 'status': 'completed', 'command': 'npm test',
         'created_at': 100.0, 'ended_at': 160.0}, now=200.0)
    assert 'completed' not in line, line
    assert '完成' in line or '成功' in line, line


def test_stop_button_reply_is_chinese(tmp_path, monkeypatch):
    """点任务卡上的「停止」后回的那一句，也要用同一套中文名。

    卡片列表早就中文了（`tasks_card`），这一句原来直接把
    `TaskRecord.status` 拼进去——同一个页面上「已完成」和 `succeeded` 并存。
    """
    import feishu_gateway

    monkeypatch.setattr(feishu_gateway.FeishuGateway, '_reply',
                        lambda self, chat_id, text, **kw: replies.append(text))
    monkeypatch.setattr(feishu_gateway.FeishuGateway, '_show_tasks',
                        lambda self, chat_id, **kw: None)
    replies = []

    gw = feishu_gateway.FeishuGateway.__new__(feishu_gateway.FeishuGateway)
    gw.api = type('Api', (), {
        'stop_task': lambda self, task_id: {'id': task_id, 'status': 'succeeded'},
    })()
    gw._handle_task_action('c1', {'task_action': 'stop', 'task_id': 'bg_1'})

    assert replies == ['任务 bg_1 → 成功'], replies


def test_permission_result_uses_the_same_rendering():
    """审批回执：工具名与原因都走审批面那一套（原来两个都是英文原文）。"""
    import feishu_gateway

    text = feishu_gateway.permission_result({
        'status': 'approved', 'tool': 'desktop_action',
        'reason': 'desktop_action inspects or controls the local foreground '
                  'desktop; each operation requires explicit approval',
        'code': 'desktop_control'})
    assert 'desktop_action inspects' not in text, text
    assert '操作键鼠' in text and '要操作本机键鼠' in text, text


def test_permission_result_drops_an_empty_reason_line():
    """没有原因时不要留一个空的「原因: 」——那是噪音。"""
    import feishu_gateway

    text = feishu_gateway.permission_result(
        {'status': 'timeout', 'tool': 'weird_tool', 'reason': '', 'code': ''})
    assert '原因' not in text, text
    assert '审批超时' in text and 'weird_tool' in text, text
