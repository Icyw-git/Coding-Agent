"""统一的投递契约：一份记录形状、一套重试分类、逐接收人确认。

为什么需要它
------------
cron 结果、心跳摘要、回合通知原本是三份**形状相似但字段不同**的待发队列：

```
cron_delivery: id / chat_id / job_id / text / created_at   （条数上限 20）
heartbeat:     occurrence_id / text / created_at            （条数上限 20）
```

共同的缺口：

- 失败只是「留在 pending」——没有退避、没有死信。一个永久失效的接收人（机器人被踢出
  群、chat 解绑）会**每轮被重试**，永远重试下去。
- 上限只有一个条数；字节与保留时长没人管，也没有「为什么被裁」的记录。
- 逐接收人确认靠调用方自己写 for 循环，容易漏掉「A 成功、B 失败」这种情形。

契约（``next-design`` §5.2）：同一种记录字段 + 重试分类 + 逐接收人确认 + 失败退避 +
死信可查可重发；上限要**条数 / 字节 / 期限三条各自可见且有界**。

两条诚实的限制
--------------
1. **不承诺外部恰好一次**。发送与 ack 之间崩溃仍会重发一次；``delivery_id`` 只能减少
   重复，减少不了"已经发出去了但没记上"这个窗口。所以记录里必须留 ``source_ref``——
   重发的是**同一份结果**，任务本身不会重跑。
2. **不迁移全部历史**。这个模块只管新的投递；消费者逐个接过来，旧文件继续能读。

存储仍是「文件 + 原子替换」：单消费者的写入频率是「每分钟几条」，SQLite 在这里不划算。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

#: 最多保留多少条待发/待重试记录。
MAX_ITEMS = 200
#: 队列总字节上限（按键入文本的 UTF-8 长度算）。
MAX_BYTES = 1_000_000
#: 保留时长：超过这个时间的记录不再发送（也不再计入待发）。
TTL_SECONDS = 7 * 24 * 3600
#: 死信保留多少条（含原因，供人查/重发）。
MAX_DEAD = 50

#: 失败多少次后进死信。退避是 1/2/4/8… 分钟，所以 5 次大约 30 分钟内放弃。
MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 1800

#: ``pending`` 从未尝试；``retrying`` 失败过且在等下一次；``dead`` 已放弃。
STATES = ('pending', 'retrying', 'dead')


def _empty() -> dict:
    return {'version': 1, 'items': [],
            'overflow': {'items': 0, 'bytes': 0, 'ttl': 0}}


def _read(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    items = [item for item in (data.get('items') or [])
             if isinstance(item, dict) and item.get('delivery_id')]
    overflow = data.get('overflow')
    if not isinstance(overflow, dict):
        overflow = {'items': 0, 'bytes': 0, 'ttl': 0}
    return {'version': 1, 'items': items,
            'overflow': {key: int(overflow.get(key) or 0)
                         for key in ('items', 'bytes', 'ttl')}}


def _write(path, data: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0),
                   encoding='utf-8')
    tmp.replace(target)


def _bytes(text: str) -> int:
    return len(str(text or '').encode('utf-8'))


def _backoff(attempts: int) -> float:
    return min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** max(0, attempts - 1)))


def enqueue(path, *, recipient: str, text: str, source_ref: str = '',
            kind: str = 'result', now: float | None = None) -> dict:
    """登记一条待投递内容，返回记录。

    ``source_ref`` 指向**生成这份内容的那个东西**（回合 id / occurrence / job id）：
    重发时靠它找回原文，而不是重跑一遍任务。
    """
    stamp = time.time() if now is None else float(now)
    row = {
        'delivery_id': uuid.uuid4().hex[:12],
        'recipient': str(recipient or ''),
        'text': str(text or ''),
        'source_ref': str(source_ref or ''),
        'kind': str(kind or ''),
        'state': 'pending',
        'attempts': 0,
        'next_retry_at': stamp,
        'created_at': stamp,
        'bytes': _bytes(text),
        'last_error': '',
    }
    data = _read(path)
    data['items'].append(row)
    _enforce_limits(data, stamp)
    _write(path, data)
    return dict(row)


def _enforce_limits(data: dict, stamp: float) -> None:
    """三条上限各自记账。淘汰顺序：过期 → 已死 → 最旧。

    过期优先淘汰是安全的（本来就不该再发）；**死信先于 pending 被淘汰**是因为
    死信已经放弃发送，留着只占地方。反过来淘汰 pending 会让“还能发出去的内容”先丢。

    条数与字节分开跑各自的循环：合在一个循环里会把“超字节”记成 `items`，
    于是看统计的人分不清是哪个上限在起作用。
    """
    alive = []
    for item in data['items']:
        if stamp - float(item.get('created_at') or 0) > TTL_SECONDS:
            data['overflow']['ttl'] += 1
            continue
        alive.append(item)

    def _total_bytes() -> int:
        return sum(int(item.get('bytes') or 0) for item in alive)

    def _evict_one(reason: str, *, prefer_dead: bool) -> bool:
        """挑一条淘汰。返回是否真的删了东西。"""
        order = (lambda item: item.get('state') == 'dead') if prefer_dead \
            else (lambda item: True)
        index = next((i for i, item in enumerate(alive) if order(item)), None)
        if index is None:
            return False
        alive.pop(index)
        data['overflow'][reason] += 1
        return True

    # 先给死信腾位置（它已放弃发送），再动真正还在排队的
    while len(alive) > MAX_ITEMS and _evict_one('items', prefer_dead=True):
        pass
    while len(alive) > MAX_ITEMS and _evict_one('items', prefer_dead=False):
        pass
    while _total_bytes() > MAX_BYTES and _evict_one('bytes', prefer_dead=True):
        pass
    while _total_bytes() > MAX_BYTES and _evict_one('bytes', prefer_dead=False):
        pass

    dead = [item for item in alive if item.get('state') == 'dead']
    if len(dead) > MAX_DEAD:
        keep = set(id(item) for item in dead[-MAX_DEAD:])
        alive = [item for item in alive
                 if item.get('state') != 'dead' or id(item) in keep]
    data['items'] = alive


def due(path, *, recipient: str | None = None, now: float | None = None,
        limit: int = 20) -> list[dict]:
    """到点该发的记录（按创建时间从旧到新）。

    ``recipient`` 给定时只看这个接收人——逐接收人确认靠它，避免"发给 A 成功就当
    整批成功"。
    """
    stamp = time.time() if now is None else float(now)
    rows = []
    for item in _read(path)['items']:
        if item.get('state') == 'dead':
            continue
        if stamp - float(item.get('created_at') or 0) > TTL_SECONDS:
            continue
        if float(item.get('next_retry_at') or 0) > stamp:
            continue
        if recipient is not None and item.get('recipient') != recipient:
            continue
        rows.append(dict(item))
    rows.sort(key=lambda item: float(item.get('created_at') or 0))
    return rows[:max(0, limit)]


def ack(path, delivery_id: str, *, now: float | None = None) -> bool:
    """确认已发送。未知/已确认返回 False（幂等）。"""
    data = _read(path)
    kept = [item for item in data['items']
            if item.get('delivery_id') != delivery_id]
    if len(kept) == len(data['items']):
        return False
    data['items'] = kept
    _write(path, data)
    return True


def fail(path, delivery_id: str, *, error: str = '',
         now: float | None = None) -> dict:
    """发送失败：计入退避；次数用完进死信（不再每轮重试）。"""
    stamp = time.time() if now is None else float(now)
    data = _read(path)
    for item in data['items']:
        if item.get('delivery_id') != delivery_id:
            continue
        attempts = int(item.get('attempts') or 0) + 1
        item['attempts'] = attempts
        item['last_error'] = ' '.join(str(error or '').split())[:200]
        if attempts >= MAX_ATTEMPTS:
            item['state'] = 'dead'
            item['next_retry_at'] = stamp
        else:
            item['state'] = 'retrying'
            item['next_retry_at'] = stamp + _backoff(attempts)
        _write(path, data)
        return dict(item)
    return {}


def dead_letters(path, *, limit: int = MAX_DEAD) -> list[dict]:
    """已放弃的投递（供人查；可 ``retry_dead`` 重发）。"""
    rows = [dict(item) for item in _read(path)['items']
            if item.get('state') == 'dead']
    rows.sort(key=lambda item: float(item.get('created_at') or 0), reverse=True)
    return rows[:max(0, limit)]


def retry_dead(path, delivery_id: str, *, now: float | None = None) -> bool:
    """把一条死信重新放回待发（人工决定后）。尝试次数**归零**重算。"""
    stamp = time.time() if now is None else float(now)
    data = _read(path)
    for item in data['items']:
        if item.get('delivery_id') != delivery_id or item.get('state') != 'dead':
            continue
        item['state'] = 'pending'
        item['attempts'] = 0
        item['next_retry_at'] = stamp
        item['last_error'] = ''
        _write(path, data)
        return True
    return False


def stats(path, *, now: float | None = None) -> dict:
    """队列概况：条数 / 字节 / 各状态 / 最老一条 / 三种溢出各自多少。"""
    stamp = time.time() if now is None else float(now)
    data = _read(path)
    live = [item for item in data['items']
            if stamp - float(item.get('created_at') or 0) <= TTL_SECONDS]
    by_state = {}
    for item in live:
        state = str(item.get('state') or 'pending')
        by_state[state] = by_state.get(state, 0) + 1
    oldest = min((float(item.get('created_at') or 0) for item in live),
                 default=0.0)
    expired = len(data['items']) - len(live)
    overflow = dict(data['overflow'])
    overflow['ttl'] += expired
    return {'count': len(live),
            'bytes': sum(int(item.get('bytes') or 0) for item in live),
            'by_state': by_state,
            'oldest_at': oldest,
            'overflow': overflow,
            'limits': {'items': MAX_ITEMS, 'bytes': MAX_BYTES,
                       'ttl_seconds': TTL_SECONDS}}
