"""自然语言时间解析，用于一次性提醒（`remind`）。

为什么需要
----------
`schedule_cron` 要求 5 段 cron 表达式。个人助手的典型用法是「周三下午三点提醒我
交周报」——让模型自己把这句话翻译成 `0 15 * * 3` 是可以的，但有三处不好：

- 时区/跨月/闰日边界容易错，而错了没人发现；
- 用户看到的确认文案是 `0 15 * * 3`，**无法验证**自己说的是不是这个意思；
- cron 是周期语义，一次性提醒用完还留在表里。

这个模块只做一件事：把常见说法解析成**绝对本地时间**（分钟精度），并给出
人能核对的回显。

设计原则
--------
- **只接受能确定的时间**。说不清的（"晚点""下周找个时间"）直接报错并给出
  可用的写法，而不是猜一个。
- **过去的时间报错**，不静默改成明天——静默改期比报错更让人困惑。
- **相对时间在解析时就定死绝对值**。进程重启后 `in 2h` 的相对基准会漂移，
  存绝对时间才稳定。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

#: 中文/英文的日期偏移词。
_DAY_WORDS = {
    '今天': 0, '今日': 0, 'today': 0,
    '明天': 1, '明日': 1, 'tomorrow': 1,
    '后天': 2,
    '大后天': 3,
}

#: 中文时段词（把"下午3点"的 3 变成 15）。
_MERIDIEM = {
    '凌晨': 0, '早上': 0, '上午': 0, '早晨': 0, 'morning': 0,
    '中午': 12, 'noon': 12,
    '下午': 12, '傍晚': 12, 'afternoon': 12, 'evening': 12,
    '晚上': 12, 'night': 12,
}

_RELATIVE = re.compile(r'(?i)^\s*(?:in\s+)?\+?(\d+)\s*(s|sec|m|min|h|hr|d|day)s?\s*$')
_ISO = re.compile(r'^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?$')
_HHMM = re.compile(r'^(\d{1,2}):(\d{2})$')
#: 中文「3点」「3点30」「15点30分」
_CN_CLOCK = re.compile(r'^(\d{1,2})\s*点\s*(?:(\d{1,2})\s*分?)?$')
#: 英文「3pm」「15:00pm」「9 am」
_AMPM = re.compile(r'(?i)^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$')


class ReminderTimeError(ValueError):
    """时间说法无法确定。文案直接给模型/用户，所以要说清可用写法。"""


_USAGE = ('写不清的时间无法确定。可用：`2026-11-04 15:00`、`15:00`、'
          '`in 2h`、`tomorrow 9am`、`明天下午3点`')


def _apply_meridiem(hour: int, word: str) -> int:
    base = _MERIDIEM.get(word.casefold())
    if base is None:
        return hour
    if base == 12 and hour < 12:
        return hour + 12
    if base == 0 and hour == 12:      # 凌晨12点 = 0 点
        return 0
    return hour


def parse_when(text: str, *, now: datetime | None = None) -> datetime:
    """把时间说法解析成**本地绝对时间**（秒归零）。

    ``now`` 供测试注入；生产用本机当前时间。
    """
    raw = ' '.join(str(text or '').split())
    if not raw:
        raise ReminderTimeError('缺少时间。' + _USAGE)
    current = now or datetime.now()
    lowered = raw.casefold()

    # 1) 相对量：in 2h / 30m
    match = _RELATIVE.match(raw)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).casefold()
        if unit in ('s', 'sec'):
            # cron 是分钟粒度：不足一分钟按一分钟算，否则提醒会静默丢掉。
            delta = timedelta(minutes=1)
        elif unit in ('m', 'min'):
            delta = timedelta(minutes=amount)
        elif unit in ('h', 'hr'):
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(days=amount)
        target = current + delta
        return target.replace(second=0, microsecond=0)

    # 2) 绝对日期（可带时分）
    match = _ISO.match(raw)
    if match:
        year, month, day, hour, minute = match.groups()
        # 只给日期时默认 09:00，与「明天」等其他路径一致。
        # 字面解释是 00:00，但午夜提醒几乎总是误用，而回显会写清是 09:00。
        try:
            return datetime(int(year), int(month), int(day),
                            int(hour if hour is not None else 9),
                            int(minute or 0))
        except ValueError as exc:
            raise ReminderTimeError(f'日期不合法：{raw}（{exc}）') from None

    # 3) 日期词 + 时间（今天/明天/后天，today/tomorrow）
    day_offset = None
    rest = lowered
    for word, offset in _DAY_WORDS.items():
        if word in lowered:
            day_offset = offset
            rest = lowered.replace(word, ' ').strip()
            break

    # 4) 时段词（下午/晚上/morning…）
    meridiem = ''
    for word in _MERIDIEM:
        if word in lowered:
            meridiem = word
            rest = rest.replace(word, ' ').strip()
            break

    rest = rest.strip()
    hour = minute = None
    if rest:
        match = _HHMM.match(rest)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
        else:
            match = _CN_CLOCK.match(rest)
            if match:
                hour, minute = int(match.group(1)), int(match.group(2) or 0)
            else:
                match = _AMPM.match(rest)
                if match:
                    hour, minute = int(match.group(1)), int(match.group(2) or 0)
                    # am/pm 自带时段，忽略中文时段词
                    if match.group(3).casefold() == 'pm' and hour < 12:
                        hour += 12
                    meridiem = ''
                elif rest in ('', ) :
                    pass
                else:
                    raise ReminderTimeError(f'看不懂的时间：{raw}。' + _USAGE)

    if hour is None:
        # 只给了日期没给时间：默认当天 09:00，并在回显里说明。
        hour, minute = 9, 0
    if not 0 <= hour <= 23 or not 0 <= (minute or 0) <= 59:
        raise ReminderTimeError(f'时间不合法：{raw}')

    hour = _apply_meridiem(hour, meridiem) if meridiem else hour
    base = current if day_offset is None else current + timedelta(days=day_offset)
    target = base.replace(hour=min(hour, 23), minute=minute or 0,
                          second=0, microsecond=0)

    if day_offset is None and target <= current:
        # 只给时分且已经过了：顺延到明天，但回显要说清是哪天。
        target += timedelta(days=1)
    return target


def to_cron(when: datetime) -> str:
    """绝对时间 → 一次性 cron 表达式（分 时 日 月 周）。"""
    return f'{when.minute} {when.hour} {when.day} {when.month} *'


def describe(when: datetime, *, now: datetime | None = None) -> str:
    """人能核对的时间回显。**这是这个模块存在的主要理由之一**：
    用户看到的必须是"11 月 4 日 15:00"，而不是 `0 15 4 11 *`。"""
    current = now or datetime.now()
    delta = when - current
    weekday = '一二三四五六日'[when.weekday()]
    stamp = f'{when.month} 月 {when.day} 日（周{weekday}）{when.hour:02d}:{when.minute:02d}'
    if delta.total_seconds() < 0:
        return f'{stamp}（已过去）'
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f'{stamp}（约 {max(1, minutes)} 分钟后）'
    if minutes < 60 * 24:
        return f'{stamp}（约 {minutes // 60} 小时后）'
    return f'{stamp}（约 {minutes // (60 * 24)} 天后）'


def validate_future(when: datetime, *, now: datetime | None = None) -> None:
    """拒绝过去的时间：静默改期比报错更让人困惑。"""
    current = now or datetime.now()
    if when <= current:
        raise ReminderTimeError(
            f'这个时间已经过去了（{describe(when, now=current)}）。请给一个未来的时间。')
