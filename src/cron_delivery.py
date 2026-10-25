"""定时任务结果的投递 outbox：定时回合写，飞书网关领走。

为什么单独一条通道
------------------
普通 cron 回合在后台新建会话，不绑定聊天，watcher 看不到它；结果只留在
会话日志里，手机上什么都收不到。这里让任务在创建时记下目标聊天
（``CronJob.deliver_to``），回合结束后把最终答复（或失败原因）写进 outbox，
网关轮询到再发送。

投递语义与心跳一致
------------------
至少一次：先入队，网关发送成功后才 ack；发送失败保留 pending 下一轮重试。
发送与 ack 之间崩溃会重复推送一次，不会漏发。outbox 有上限，长期离线的
网关只保留最近若干条。目标聊天必须仍是网关已绑定的聊天，否则不发送 ——
避免任务被用来向任意 chat_id 发消息。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import paths

OUTBOX_PATH = paths.cache_path('cron_results', 'outbox.json')
OUTBOX_LIMIT = 20
# 飞书文本消息远小于这个量级；截断而不是让发送端报错，用户至少能看到开头。
MAX_TEXT_CHARS = 12000

_LOCK = threading.RLock()


def item_id(chat_id: str, job_id: str, occurrence: str) -> str:
    """确定性 id：同一批任务、同一触发分钟只入队一次。"""
    raw = f'{chat_id}|{job_id}|{occurrence}'.encode('utf-8')
    return 'cr_' + hashlib.sha1(raw).hexdigest()[:16]


def result_text(job_ids, answer: str) -> str:
    header = f'⏰ 定时任务 · {", ".join(job_ids)}'
    body = (answer or '').strip() or '（无输出）'
    return _truncate(f'{header}\n\n{body}')


def failure_text(job_ids, exc: BaseException) -> str:
    detail = f'{type(exc).__name__}: {exc}'.strip()
    if len(detail) > 300:
        detail = detail[:300] + '…'
    header = f'⚠️ 定时任务失败 · {", ".join(job_ids)}'
    return _truncate(f'{header}\n{detail}')


def queue(chat_id: str, text: str, *, job_id: str = '',
          occurrence: str = '') -> dict | None:
    """入队一条待投递结果；缺聊天/正文时返回 None（静默任务）。"""
    chat_id = (chat_id or '').strip()
    text = (text or '').strip()
    if not chat_id or not text:
        return None
    occurrence = occurrence or str(int(time.time()))
    entry = {'id': item_id(chat_id, job_id, occurrence),
             'chat_id': chat_id,
             'job_id': job_id,
             'text': _truncate(text),
             'created_at': time.time()}
    with _LOCK:
        data = _read_outbox()
        if any(item.get('id') == entry['id'] for item in data['pending']):
            return dict(entry)
        data['pending'].append(entry)
        data['pending'] = data['pending'][-OUTBOX_LIMIT:]
        _write_outbox(data)
    return dict(entry)


def pending() -> list[dict]:
    """尚未被网关 ack 的结果（GET /v1/cron-results）。"""
    with _LOCK:
        return [dict(item) for item in _read_outbox()['pending']]


def ack(item_id_: str) -> bool:
    """网关发送成功后确认；未知/已确认的 id 返回 False（幂等）。"""
    with _LOCK:
        data = _read_outbox()
        kept = [item for item in data['pending'] if item.get('id') != item_id_]
        if len(kept) == len(data['pending']):
            return False
        data['pending'] = kept
        _write_outbox(data)
        return True


def _truncate(text: str) -> str:
    text = (text or '').strip()
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + '\n…（内容过长，已截断）'


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    tmp.replace(path)


def _read_outbox() -> dict:
    data = _read_json(OUTBOX_PATH)
    items = data.get('pending')
    if not isinstance(items, list):
        return {'version': 1, 'pending': []}
    cleaned = [item for item in items
               if isinstance(item, dict) and item.get('id')
               and item.get('chat_id') and item.get('text')]
    return {'version': 1, 'pending': cleaned}


def _write_outbox(data: dict) -> None:
    _write_json(OUTBOX_PATH, {'version': 1, 'pending': data['pending']})
