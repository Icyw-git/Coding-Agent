"""工具的审计事件必须能出现在 `/audit` 里。

`tool_delete` / `tool_move` / `tool_remind` 的 `_notify(audit, 'delete_trashed', …)`
最终走 `tools._screen_audit` → `store.append(event, …)`，所以事件**已经写进会话流**了。

但 `/audit` 读的是 `audit.collect()` → `audit.project()`，后者按 `AUDIT_EVENTS` 白名单
过滤，未登记的类型返回 `None` 被丢掉。于是这 11 个事件：

- 磁盘上有（会话 jsonl 里查得到）；
- `audit.EVENT_LABELS` 里有中文名（说明本来就打算给人看）；
- `/audit` 里**永远看不到**。

删除与移动是这套系统里风险最高的两个操作，它们的留痕恰好不可见。
"""

import re
from pathlib import Path

import audit

SRC = Path(__file__).resolve().parent.parent / 'src'
TOOLS = ('tool_delete.py', 'tool_move.py', 'tool_remind.py')


def _notified_events(name: str) -> set[str]:
    text = (SRC / name).read_text(encoding='utf-8')
    return set(re.findall(r"_notify\(\s*audit\s*,\s*'([a-z_]+)'", text))


def test_every_notified_event_is_in_the_whitelist():
    orphans = {}
    for name in TOOLS:
        missing = _notified_events(name) - set(audit.AUDIT_EVENTS)
        if missing:
            orphans[name] = sorted(missing)
    assert not orphans, f'这些事件写了但 /audit 看不到：{orphans}'


def test_every_notified_event_has_a_chinese_label():
    for name in TOOLS:
        for event in _notified_events(name):
            assert event in audit.EVENT_LABELS, f'{event} 没有中文名'


def test_a_delete_event_actually_surfaces(tmp_path):
    """端到端：写一条 delete_trashed，collect 必须能拿到。"""
    import json

    store = tmp_path / 'session_x.jsonl'
    store.write_text(json.dumps({
        'type': 'delete_trashed', 'seq': 1, 'ts': 1.0, 'session_id': 'x',
        'count': 3, 'method': 'recycle-bin'}, ensure_ascii=False) + '\n',
        encoding='utf-8')

    events = audit.collect(tmp_path, limit=10)
    assert [e['type'] for e in events] == ['delete_trashed']
    assert events[0]['count'] == 3
