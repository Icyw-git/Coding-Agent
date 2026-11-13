"""统一的模型调用记录：一次 SDK 请求一条，主回合与副通道分得开。

为什么需要它
------------
副通道（记忆整理、标题、压缩、审批、建议）都走 `llm_route.completion(role=...)`，
**没有任何一处记录用量**。于是"每天花了多少 token"只能看到主回合那一半，而记忆整理
这类后台调用恰恰是容易被忘记、又确实花钱的那一半。

记录点放在 `llm_route.completion` 里面——那是唯一的 SDK 调用入口。放在这里而不是
各调用方，才能保证**一次请求只记一条**（否则主回合与副通道会各记一遍，统计翻倍）。

几个字段的边界
--------------
- ``model_request_id``：**模型逻辑请求**的 id。与用户入站的 ``request_id`` 不是一回事：
  一次用户回合可能包含多次模型请求（主回答 + 摘要 + 记忆整理…）。
- ``attempt``：本进程可见的尝试序号。SDK 内部的重试**不可观测**，只能标 1；
  不假装已经数清供应商那边到底打了几次。
- ``tokens``：供应商没回 usage 时是 ``None``（**不是 0**）。0 会把成本算低，
  而"不知道"和"没花钱"是两件事。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import paths

LOG_PATH = paths.cache_path('llm_calls.jsonl')

#: 单个日志文件最多多少行。超过就滚动一次（只留最近一半），避免无限增长。
MAX_LINES = 5000


def _now() -> float:
    return time.time()


def new_request_id() -> str:
    return 'mr_' + uuid.uuid4().hex[:16]


def _parse_usage(usage) -> dict | None:
    """把 SDK 的 usage 归一成四个数字；拿不到就返回 None。"""
    if usage is None:
        return None
    try:
        prompt = int(getattr(usage, 'prompt_tokens', 0) or 0)
        completion = int(getattr(usage, 'completion_tokens', 0) or 0)
        total = int(getattr(usage, 'total_tokens', 0) or (prompt + completion))
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    details = getattr(usage, 'prompt_tokens_details', None)
    cached = getattr(details, 'cached_tokens', None) if details is not None else None
    if cached is None:
        cached = getattr(usage, 'prompt_cache_hit_tokens', None)
    return {'prompt': prompt, 'completion': completion, 'total': total,
            'cached': int(cached or 0)}


def record(*, role: str, purpose: str = '', provider: str = '', model: str = '',
           duration_ms: float = 0.0, usage=None, outcome: str = 'ok',
           error: str = '', attempt: int = 1,
           model_request_id: str = '') -> dict:
    """记一条调用。``usage`` 拿不到时 ``tokens`` 落 ``None``（unknown）。"""
    tokens = _parse_usage(usage)
    row = {
        'ts': _now(),
        'model_request_id': model_request_id or new_request_id(),
        'role': str(role or 'main'),
        'purpose': str(purpose or role or ''),
        'provider': str(provider or ''),
        'model': str(model or ''),
        'attempt': max(1, int(attempt or 1)),
        'duration_ms': round(float(duration_ms or 0.0), 3),
        'tokens': tokens,
        'usage_available': tokens is not None,
        'outcome': str(outcome or 'ok'),
        'error': ' '.join(str(error or '').split())[:200],
    }
    _append(row)
    return row


def _append(row: dict) -> None:
    path = Path(LOG_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    except OSError:
        # 观测是旁路：写不进去不能反过来影响一次已经发生的模型调用。
        return
    _rotate_if_needed(path)


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.stat().st_size < 2_000_000:
            return
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        if len(lines) <= MAX_LINES:
            return
        keep = lines[len(lines) // 2:]
        tmp = path.with_suffix('.tmp')
        tmp.write_text('\n'.join(keep) + '\n', encoding='utf-8')
        tmp.replace(path)
    except OSError:
        return


def read(path=None) -> list[dict]:
    """读全部记录（跳过坏行）。调用方通常只关心最近的。"""
    target = Path(path or LOG_PATH)
    rows = []
    try:
        with target.open('r', encoding='utf-8', errors='replace') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def totals_by_role(rows: list[dict]) -> dict:
    """按角色汇总：请求数、token、有没有未知用量的请求。

    ``tokens`` 只累加**已知**用量；``unknown`` 单独计数——把未知当 0 会让成本看起来
    比实际低。
    """
    totals: dict[str, dict] = {}
    for row in rows:
        role = str(row.get('role') or 'main')
        bucket = totals.setdefault(role, {'requests': 0, 'tokens': 0,
                                          'unknown': 0, 'errors': 0})
        bucket['requests'] += 1
        tokens = row.get('tokens')
        if isinstance(tokens, dict):
            bucket['tokens'] += int(tokens.get('total') or 0)
        else:
            bucket['unknown'] += 1
        if row.get('outcome') not in (None, 'ok'):
            bucket['errors'] += 1
    return totals
