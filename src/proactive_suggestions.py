"""建议实体：让「点开始才执行」有一个可绑定的对象，并把反馈接回生产入口。

为什么需要它
------------
`proactive` 一直在生成建议，但建议只是**一段文字**：没有 id、没有归属、没有状态。
后果是设计文档点名的那个缺口——`record_accepted` / `record_ignored` 只在测试里被调用，
**没有生产消费者**。于是：

- 用户点了"忽略"，降频不会发生（承诺了却没实现）；
- "点开始才执行"没有可绑定的对象，也就没法说清"这次执行对应哪条建议"；
- 同一条建议重复点击会变成两次执行。

实体化之后这三件事都能回答：

```
id / workspace / evidence_refs / proposed_action / created_at / recipient / status
proposed → accepted / dismissed / expired
```

几条刻意的选择
--------------
- **接受不是扩大权限**：`accept()` 只拿一个稳定的 `request_id` 去走正常受理
  （`AgentRuntime.send`），照旧审批。建议本身不携带任何工具许可。
- **幂等靠 request_id**：同一建议的 request_id 由 id 决定，重复点击只是再查一次台账。
- **过期不算忽略**：用户根本没看到，拿它降频等于把沉默当拒绝。
- **busy / 未受理保持 proposed**：能重试；只有真的受理上了才标 accepted。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

import paths

STATE_PATH = paths.cache_path('proactive_suggestions.json')

#: 建议的存活时间。过期后按钮点了会被判 `expired`，不再执行、也不算忽略。
TTL_SECONDS = 24 * 3600
#: 保留多少条历史（含已处理的）。计数有界，避免长期使用把文件撑大。
MAX_ITEMS = 100
#: 一条建议最多多少条证据引用。
MAX_EVIDENCE = 5

_STATUSES = ('proposed', 'accepted', 'dismissed', 'expired')


def _now() -> float:
    return time.time()


def _read() -> dict:
    try:
        data = json.loads(Path(STATE_PATH).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'version': 1, 'items': []}
    if not isinstance(data, dict):
        return {'version': 1, 'items': []}
    items = [item for item in (data.get('items') or [])
             if isinstance(item, dict) and item.get('id')]
    return {'version': 1, 'items': items[-MAX_ITEMS:]}


def _write(data: dict) -> None:
    path = Path(STATE_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0),
                       encoding='utf-8')
        tmp.replace(path)
    except OSError:
        # 建议是可选能力：写不进去不该让主流程失败（会由"找不到 id"暴露出来）。
        pass


def _request_id(suggestion_id: str) -> str:
    """接受时用的去重键。由建议 id 决定，所以重复点击天然幂等。"""
    digest = hashlib.sha1(suggestion_id.encode('utf-8')).hexdigest()[:16]
    return f'suggestion:{digest}'


def record(cache_dir, suggestions, *, workspace: str = '', evidence=(),
           recipient: str = '') -> list[dict]:
    """把一批建议文字登记成实体，返回新记录。

    ``cache_dir`` 显式传入而不是回读组合根：这个模块要能在没有 harness 的场景下用
    （诊断脚本、测试）。
    """
    stamp = _now()
    created = []
    for text in suggestions or ():
        clean = ' '.join(str(text or '').split())
        if not clean:
            continue
        created.append({
            'id': uuid.uuid4().hex[:12],
            'workspace': str(workspace or ''),
            'evidence_refs': [str(item) for item in (evidence or ())][:MAX_EVIDENCE],
            'proposed_action': clean[:300],
            'created_at': stamp,
            'expires_at': stamp + TTL_SECONDS,
            'recipient': str(recipient or ''),
            'status': 'proposed',
        })
    if not created:
        return []
    data = _read()
    data['items'] = (data['items'] + created)[-MAX_ITEMS:]
    _write(data)
    return [dict(item) for item in created]


def _load(cache_dir) -> dict:
    """读 + 顺便把过期的标成 expired（不给用户留一堆假 open 的项）。"""
    data = _read()
    stamp = _now()
    changed = False
    for item in data['items']:
        if item.get('status') == 'proposed' and float(item.get('expires_at') or 0) < stamp:
            item['status'] = 'expired'
            changed = True
    if changed:
        _write(data)
    return data


def _find(data: dict, suggestion_id: str) -> dict | None:
    return next((item for item in data['items']
                 if item.get('id') == suggestion_id), None)


def all_items(cache_dir) -> list[dict]:
    return [dict(item) for item in _load(cache_dir)['items']]


def open_items(cache_dir) -> list[dict]:
    """还没处理的建议（``proposed``），最近的在前。"""
    rows = [dict(item) for item in _load(cache_dir)['items']
            if item.get('status') == 'proposed']
    rows.sort(key=lambda item: float(item.get('created_at') or 0), reverse=True)
    return rows


def check_token(cache_dir, suggestion_id: str, *, recipient: str = '') -> bool:
    """按钮 token 校验：必须存在、未过期，且**接收人匹配**。

    绑定接收人是为了防止"把卡片转发给别人点"——建议里带着项目路径与证据引用。
    记录里没写接收人时（本地场景）只校验存在性。
    """
    item = _find(_load(cache_dir), suggestion_id)
    if item is None:
        return False
    if item.get('status') == 'expired':
        return False
    expected = str(item.get('recipient') or '')
    return not expected or expected == str(recipient or '')


def accept(cache_dir, suggestion_id: str, *, accept, recipient: str = '') -> dict:
    """接受一条建议：走**正常受理**，不扩权。

    ``accept`` 是回调 ``(request_id) -> {'status': ..., 'turn_id': ...}``，
    生产上就是 ``AgentRuntime.send`` 那层；``status`` 取
    ``accepted / duplicate / busy / pending / conflict / full``。

    只有受理上了（``accepted`` 或 ``duplicate``）才把建议标成 accepted——
    ``busy`` 说明这次没排上，建议要留在开放列表里等用户再点。
    """
    data = _load(cache_dir)
    item = _find(data, suggestion_id)
    if item is None:
        return {'status': 'unknown', 'turn_id': ''}
    if not check_token(cache_dir, suggestion_id, recipient=recipient):
        if item.get('status') == 'expired':
            return {'status': 'expired', 'turn_id': ''}
        return {'status': 'forbidden', 'turn_id': ''}
    if item.get('status') == 'accepted':
        # 幂等：重复点击直接返回原关联，不再受理一次
        return {'status': 'accepted', 'turn_id': str(item.get('turn_id') or ''),
                'request_id': str(item.get('request_id') or '')}
    if item.get('status') != 'proposed':
        return {'status': str(item.get('status') or ''), 'turn_id': ''}

    request_id = _request_id(suggestion_id)
    try:
        outcome = accept(request_id) or {}
    except Exception as exc:
        # 受理层抛错（冲突/待核对/忙）：建议保持 proposed，让用户能再试
        return {'status': 'error', 'turn_id': '', 'request_id': request_id,
                'reason': f'{type(exc).__name__}: {exc}'}

    status = str(outcome.get('status') or '')
    if status in ('accepted', 'duplicate'):
        item['status'] = 'accepted'
        item['turn_id'] = str(outcome.get('turn_id') or '')
        item['request_id'] = request_id
        item['accepted_at'] = _now()
        _write(data)
        _record_feedback(cache_dir, accepted=True)
        return {'status': 'accepted', 'turn_id': item['turn_id'],
                'request_id': request_id}
    # busy / pending / conflict / full / 其它：不动状态，保持可重试
    return {'status': status or 'unknown', 'turn_id': '',
            'request_id': request_id}


def dismiss(cache_dir, suggestion_id: str, *, recipient: str = '') -> dict:
    """明确忽略（用户点了"不用了"）。只有这一种情况才记负反馈。"""
    data = _load(cache_dir)
    item = _find(data, suggestion_id)
    if item is None:
        return {'status': 'unknown'}
    if not check_token(cache_dir, suggestion_id, recipient=recipient):
        return {'status': 'expired' if item.get('status') == 'expired' else 'forbidden'}
    if item.get('status') == 'expired':
        return {'status': 'expired'}
    if item.get('status') != 'proposed':
        return {'status': str(item.get('status') or '')}

    item['status'] = 'dismissed'
    item['dismissed_at'] = _now()
    _write(data)
    _record_feedback(cache_dir, accepted=False)
    return {'status': 'dismissed'}


def _record_feedback(cache_dir, *, accepted: bool) -> None:
    """把采纳/忽略写回节奏状态——这正是原来断掉的那一环。"""
    try:
        import proactive

        if accepted:
            proactive.record_accepted(cache_dir)
        else:
            proactive.record_ignored(cache_dir)
    except Exception:
        # 反馈记账失败不该让"已经点过的按钮"看起来失败
        pass
