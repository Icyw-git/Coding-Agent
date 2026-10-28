"""`remind` 工具层：一次性提醒。

一条提醒 = 一次性 cron 任务（`recurring=False`，触发后自动清理）+ 既有投递。
不新建调度设施——cron 表、notify、飞书投递、审计都已就位，这里缺的只是
**自然语言时间解析**（`reminders.py`）和一次性语义。

为什么不做"静态消息直投"
------------------------
那需要另建一条投递路径（心跳的 outbox 是它自己专用的）。复用 cron 的代价是
触发时跑一次模型（约 2 秒），换来的是提醒可以带上下文（"顺便，昨天那个任务
还没收尾"）。对个人使用这点成本可接受，而少一条投递路径意味着少一处会不同步的
状态。
"""
from __future__ import annotations


def _notify(audit, event: str, **payload) -> None:
    if audit is None:
        return
    try:
        audit(event, **payload)
    except Exception:
        pass


def remind(text: str, when: str, *, schedule=None, now=None,
           deliver_to: str = '', audit=None) -> str:
    """在指定时间提醒一件事。

    ``schedule`` 供测试注入（签名与 ``cron_scheduler.schedule_job`` 一致）。
    """
    import reminders

    body = ' '.join(str(text or '').split())
    if not body:
        return 'Error: text is required (what should the reminder say?)'

    try:
        target = reminders.parse_when(when, now=now)
        reminders.validate_future(target, now=now)
    except reminders.ReminderTimeError as exc:
        _notify(audit, 'remind_rejected', reason=str(exc)[:200])
        return f'Error: {exc}'

    human = reminders.describe(target, now=now)
    cron = reminders.to_cron(target)
    # prompt 要自带上下文：定时回合是独立会话，看不到当前对话。
    prompt = (f'提醒用户：{body}。直接把这个提醒说清楚即可，不要扩展成新任务。'
              f'（这条提醒设定时约定为 {human}）')

    if schedule is None:
        from cron_scheduler import schedule_job as schedule
    result = schedule(cron, prompt, recurring=False, deliver_to=deliver_to,
                      allow_side_effects=False)
    if isinstance(result, str) and result.strip():
        # schedule_job 用返回字符串表示错误
        _notify(audit, 'remind_failed', reason=result[:200])
        return f'Error: {result}'

    _notify(audit, 'remind_scheduled', when=target.isoformat(timespec='minutes'),
            cron=cron, chars=len(body))
    return (f'Reminder set for {human}.\n'
            f'  「{body}」\n'
            'It fires once and then removes itself. '
            'Use /crons to see or cancel pending reminders.')
