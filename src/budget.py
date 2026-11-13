"""后台预算：日 token 上限 + 「该不该跳过可选工作」。

为什么要有它
------------
`next-design` §7.2：预算分两级——每回合的 deadline/调用数/输出上限，和**背景日预算**。
前者是回合自己的事；后者要回答一个问题：**今天可选的那些后台工作还值不值得做？**

可选的是：记忆整理、主动建议、自改进候选——不做也不会让用户当下的事情出错。
不可选的是：**审批与安全核验**。省预算是为了少花钱，不是为了少拦一次危险命令；
那两类如果因为"预算用完"被跳过，等于用省钱换安全，方向就错了。

未知用量怎么算
--------------
供应商没回 usage 时，记录里 `tokens` 是 `None`。这里把它计进 `unknown_requests`
而不是 0——**"不知道花了多少"和"没花钱"是两件事**，把未知当 0 会让预算看起来
永远有余量。
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import llm_calls
import paths

#: 与 `llm_calls` 共用同一份记录（不另建埋点）。
LOG_PATH = llm_calls.LOG_PATH
STATE_PATH = paths.cache_path('budget.json')

#: 背景一天允许多少 token。具体值由短期采样后再调——这里给一个保守起点，
#: 并在文档里写清"这是估计，不是账单"。
DAILY_TOKENS = 200_000

#: 这些角色的调用**不受预算约束**：省预算不能省掉安全判断。
PROTECTED_ROLES = frozenset({'approval', 'safety'})

#: 预算不足时可以被跳过的可选工作。
OPTIONAL_ROLES = frozenset({'memory', 'title', 'summary', 'proactive', 'improve'})


def _now() -> float:
    return time.time()


def _today(now: float | None = None) -> str:
    return date.fromtimestamp(_now() if now is None else float(now)).isoformat()


def _read_rows() -> list[dict]:
    return llm_calls.read(LOG_PATH)


def _rows_today(now: float | None = None) -> list[dict]:
    stamp = _now() if now is None else float(now)
    day = _today(stamp)
    rows = []
    for row in _read_rows():
        try:
            ts = float(row.get('ts') or 0)
        except (TypeError, ValueError):
            continue
        if _today(ts) == day:
            rows.append(row)
    return rows


def spent_today(now: float | None = None) -> int:
    """今天**已知**的 token 用量。未知用量的请求不计入（见 `describe`）。"""
    total = 0
    for row in _rows_today(now):
        tokens = row.get('tokens')
        if isinstance(tokens, dict):
            total += int(tokens.get('total') or 0)
    return total


def exhausted(now: float | None = None) -> bool:
    limit = int(DAILY_TOKENS)
    if limit <= 0:
        return True
    return spent_today(now) >= limit


def allows(role: str, now: float | None = None) -> bool:
    """这个角色现在还能不能调模型。

    受保护角色永远返回 True——**即使预算已经耗尽**。可选角色在耗尽时返回 False，
    调用方据此跳过（并说明跳过原因，而不是静默不做）。
    """
    key = str(role or '').strip().casefold()
    if key in PROTECTED_ROLES:
        return True
    if key not in OPTIONAL_ROLES:
        return True          # 不认识的别拦：拦错比放过更麻烦
    return not exhausted(now)


def describe(now: float | None = None) -> str:
    """一行给用户看的预算说明；数字未知时说未知，不编。"""
    stamp = _now() if now is None else float(now)
    rows = _rows_today(stamp)
    known = 0
    unknown = 0
    for row in rows:
        tokens = row.get('tokens')
        if isinstance(tokens, dict):
            known += int(tokens.get('total') or 0)
        else:
            unknown += 1
    limit = int(DAILY_TOKENS)
    if limit <= 0:
        head = '背景预算：已关闭（所有可选后台工作都会跳过）'
    elif known >= limit:
        head = f'背景预算：今天已用完（{known}/{limit} tokens）'
    else:
        head = f'背景预算：今天已用 {known}/{limit} tokens'
    if unknown:
        head += f'；另有 {unknown} 次请求用量未知'
    return head


def _read_state() -> dict:
    try:
        data = json.loads(Path(STATE_PATH).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def note_skip(role: str, now: float | None = None) -> None:
    """记一次"因为预算跳过"，让用户能看出"今天为什么没有建议/整理"。"""
    state = _read_state()
    day = _today(now)
    bucket = state.setdefault('skips', {})
    today = bucket.setdefault(day, {})
    key = str(role or '').strip().casefold() or 'unknown'
    today[key] = int(today.get(key) or 0) + 1
    # 只留最近 7 天：计数器不需要留全史
    for stale in sorted(bucket)[:-7]:
        bucket.pop(stale, None)
    try:
        Path(STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(STATE_PATH).with_suffix('.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=0),
                       encoding='utf-8')
        tmp.replace(STATE_PATH)
    except OSError:
        pass


def skipped_today(now: float | None = None) -> dict:
    return dict((_read_state().get('skips') or {}).get(_today(now), {}))
