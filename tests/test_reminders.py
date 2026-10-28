"""`remind`：一次性提醒与自然语言时间解析。

为什么单独做时间解析：`schedule_cron` 要 5 段 cron 表达式。个人助手的典型用法是
「明天下午三点提醒我交周报」——让模型翻译 cron 可以，但时区边界易错，且用户看到的
确认文案是 `0 15 3 * *`，**无法验证**自己说的是不是这个意思。
"""
from __future__ import annotations

from datetime import datetime

import pytest

import reminders
import tool_remind

#: 固定"当前时间"：周三 2026-10-28 14:30。
NOW = datetime(2026, 10, 28, 14, 30)


def _parse(text):
    return reminders.parse_when(text, now=NOW)


# ---------------------------------------------------------------- 相对时间

@pytest.mark.parametrize('text,expected', [
    ('in 2h', datetime(2026, 10, 28, 16, 30)),
    ('30m', datetime(2026, 10, 28, 15, 0)),
    ('in 1d', datetime(2026, 10, 29, 14, 30)),
    ('+3h', datetime(2026, 10, 28, 17, 30)),
])
def test_relative_forms(text, expected):
    assert _parse(text) == expected


def test_sub_minute_rounds_up_to_one_minute():
    """cron 是分钟粒度：不足一分钟若按 0 算，提醒会静默丢掉。"""
    assert _parse('10s') == datetime(2026, 10, 28, 14, 31)


def test_relative_strips_seconds():
    assert _parse('in 2h').second == 0


# ---------------------------------------------------------------- 绝对时间

@pytest.mark.parametrize('text,expected', [
    ('2026-11-04 15:00', datetime(2026, 11, 4, 15, 0)),
    ('2026-11-04T15:00', datetime(2026, 11, 4, 15, 0)),
    ('2026-11-04', datetime(2026, 11, 4, 9, 0)),      # 只给日期 → 默认 09:00
])
def test_absolute_forms(text, expected):
    assert _parse(text) == expected


def test_invalid_date_is_rejected():
    with pytest.raises(reminders.ReminderTimeError, match='invalid date'):
        _parse('2026-02-30 10:00')


# ---------------------------------------------------------------- 时分与顺延

def test_time_only_in_the_future_today():
    assert _parse('15:00') == datetime(2026, 10, 28, 15, 0)


def test_time_only_already_passed_rolls_to_tomorrow():
    """只给时分且已过：顺延到明天，而不是报错——"3 点提醒我"下午 4 点说的
    时候显然指明天。"""
    assert _parse('09:00') == datetime(2026, 10, 29, 9, 0)


def test_am_pm_forms():
    assert _parse('3pm') == datetime(2026, 10, 28, 15, 0)
    assert _parse('9am') == datetime(2026, 10, 29, 9, 0)   # 已过 9 点 → 明天


def test_invalid_clock_is_rejected():
    with pytest.raises(reminders.ReminderTimeError):
        _parse('25:00')


# ---------------------------------------------------------------- 中文表达

@pytest.mark.parametrize('text,expected', [
    ('明天下午3点', datetime(2026, 10, 29, 15, 0)),
    ('明天 9:00', datetime(2026, 10, 29, 9, 0)),
    ('今天 17:00', datetime(2026, 10, 28, 17, 0)),
    ('后天 8:30', datetime(2026, 10, 30, 8, 30)),
    ('晚上8点', datetime(2026, 10, 28, 20, 0)),
    ('明天下午3点30', datetime(2026, 10, 29, 15, 30)),
])
def test_chinese_forms(text, expected):
    assert _parse(text) == expected


def test_day_word_without_time_defaults_to_morning():
    assert _parse('明天') == datetime(2026, 10, 29, 9, 0)


def test_tomorrow_in_english():
    assert _parse('tomorrow 9am') == datetime(2026, 10, 29, 9, 0)


# ---------------------------------------------------------------- 拒绝说不清的

@pytest.mark.parametrize('text', [
    '', '   ', '晚点', '下周找个时间', 'soon', 'later',
])
def test_unparseable_is_rejected_with_usage(text):
    with pytest.raises(reminders.ReminderTimeError) as excinfo:
        _parse(text)
    # 必须给出可用写法，而不是只说"看不懂"
    text = str(excinfo.value)
    assert 'Usable forms' in text or 'missing time' in text


# ---------------------------------------------------------------- 未来校验

def test_past_time_is_rejected():
    with pytest.raises(reminders.ReminderTimeError, match='in the past'):
        reminders.validate_future(datetime(2020, 1, 1), now=NOW)


def test_future_time_passes():
    reminders.validate_future(datetime(2026, 12, 1), now=NOW)


# ---------------------------------------------------------------- cron 与回显

def test_to_cron_is_one_shot_shape():
    """5 段、周字段为 *：具体日期由日/月锁定，不会每周重放。"""
    assert reminders.to_cron(datetime(2026, 11, 4, 15, 0)) == '0 15 4 11 *'


def test_describe_is_human_readable():
    """用户看到的必须是"11 月 4 日 15:00"，不是 cron 串。"""
    text = reminders.describe(datetime(2026, 11, 4, 15, 0), now=NOW)
    assert '11 月 4 日' in text
    assert '15:00' in text
    assert '*' not in text


def test_describe_reports_relative_distance():
    assert '分钟后' in reminders.describe(datetime(2026, 10, 28, 14, 45), now=NOW)
    assert '小时后' in reminders.describe(datetime(2026, 10, 28, 17, 30), now=NOW)
    assert '天后' in reminders.describe(datetime(2026, 11, 4, 15, 0), now=NOW)


# ---------------------------------------------------------------- 工具层

@pytest.fixture
def scheduled():
    """记录 schedule_job 的调用参数。"""
    calls = []

    def fake(cron, prompt, **kwargs):
        calls.append({'cron': cron, 'prompt': prompt, **kwargs})
        return None

    return calls, fake


def test_remind_schedules_one_shot(scheduled):
    calls, fake = scheduled
    out = tool_remind.remind('交周报', '明天下午3点', schedule=fake, now=NOW)
    assert 'Reminder set for' in out
    assert '交周报' in out
    assert len(calls) == 1
    assert calls[0]['cron'] == '0 15 29 10 *'
    assert calls[0]['recurring'] is False
    # 提醒不该获得副作用权限：触发时按只读策略跑
    assert calls[0]['allow_side_effects'] is False


def test_remind_prompt_carries_context(scheduled):
    """定时回合是独立会话，看不到当前对话，所以提示要自带上下文。"""
    calls, fake = scheduled
    tool_remind.remind('交周报', 'tomorrow 9am', schedule=fake, now=NOW)
    prompt = calls[0]['prompt']
    assert '提醒用户' in prompt
    assert '交周报' in prompt
    # 明确不要扩展成新任务
    assert '不要扩展' in prompt


def test_remind_confirmation_shows_readable_time(scheduled):
    _calls, fake = scheduled
    out = tool_remind.remind('喝水', 'in 1h', schedule=fake, now=NOW)
    assert '15:30' in out
    assert '*' not in out


def test_remind_rejects_empty_text(scheduled):
    calls, fake = scheduled
    assert 'text is required' in tool_remind.remind('', '明天', schedule=fake, now=NOW)
    assert calls == []


def test_remind_rejects_past_time(scheduled):
    calls, fake = scheduled
    out = tool_remind.remind('x', '2020-01-01 09:00', schedule=fake, now=NOW)
    assert out.startswith('Error:')
    assert 'in the past' in out
    assert calls == []


def test_remind_reports_scheduler_error(scheduled):
    def failing(cron, prompt, **kwargs):
        return 'invalid cron'

    out = tool_remind.remind('x', '明天', schedule=failing, now=NOW)
    assert out.startswith('Error: invalid cron')


def test_remind_passes_delivery_target(scheduled):
    calls, fake = scheduled
    tool_remind.remind('x', '明天', schedule=fake, now=NOW, deliver_to='oc_chat')
    assert calls[0]['deliver_to'] == 'oc_chat'


def test_errors_are_english_but_keep_chinese_examples():
    """错误文案用英文（与其它工具一致、模型读取更可靠），
    但示例保留中文写法——用户是用中文说的时间，能抄的例子才叫可执行。"""
    with pytest.raises(reminders.ReminderTimeError) as excinfo:
        _parse('晚点')
    text = str(excinfo.value)
    assert 'cannot parse time' in text
    assert '明天下午3点' in text


def test_past_time_error_uses_neutral_stamp():
    """错误句里不能嵌中文回显：两个语言混在一句里谁读都别扭。"""
    with pytest.raises(reminders.ReminderTimeError) as excinfo:
        reminders.validate_future(datetime(2020, 1, 1, 9), now=NOW)
    text = str(excinfo.value)
    assert '2020-01-01 09:00' in text
    assert '月' not in text
    assert '周一' not in text


def test_confirmation_says_it_removes_itself(scheduled):
    """用户会担心「设了会不会一直提醒」——要主动说清。"""
    _calls, fake = scheduled
    out = tool_remind.remind('喝水', 'in 1h', schedule=fake, now=NOW)
    assert 'removes itself' in out
    assert '/crons' in out
