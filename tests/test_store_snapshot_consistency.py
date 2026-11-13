"""R0-f：seq/size 与摘要缓存必须绑定到**同一个**版本。

两个都是"读了旧、记了新"的经典形状：先读内容、后取版本号，把旧内容挂到新版本键上。

1. `SessionEventStore.__init__`：`_read_last_seq()` 与 `self.path.stat()` 是两次读。
   中间另一个写者追加一条，构造出来的 `_known_size` 已经是新大小，而 `_next_seq`
   还是旧的——下一次 `append()` 看到 size 没变就不重读 seq，于是**写出重复 seq**。
2. `agent_runtime._cached_summary`：先 `session_summary()` 再 `stat()`。中间追加一条，
   缓存键就成了新版本，值还是旧摘要（少一条消息）。之后同版本的读取都会命中这个
   陈旧值——而它看起来"有缓存、可用"。
"""

import json

import agent_runtime
from session_store import SessionEventStore


def _append_line(path, session_id, seq):
    event = {'v': 1, 'seq': seq, 'session_id': session_id, 'type': 'message',
             'ts': 0.0, 'message': {'role': 'user', 'content': f'other-{seq}'}}
    with open(path, 'a', encoding='utf-8', newline='\n') as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + '\n')


def test_constructor_snapshot_cannot_reuse_a_sequence_number(tmp_path, monkeypatch):
    """构造器两次读之间插入另一个写者 → 本进程的下一个 seq 仍必须唯一。"""
    path = tmp_path / 'sessions' / 'session_seq.jsonl'
    path.parent.mkdir(parents=True)
    store = SessionEventStore.open(path)
    store.append('message', message={'role': 'user', 'content': 'first'})
    assert store.last_seq == 1

    original = SessionEventStore._read_last_seq

    def racing_read(self):
        seq = original(self)          # 读到 1
        _append_line(self.path, self.session_id, seq + 1)   # 另一写者立刻追加 seq=2
        return seq

    monkeypatch.setattr(SessionEventStore, '_read_last_seq', racing_read)
    fresh = SessionEventStore.open(path)      # 构造器：先读 seq，再 stat
    monkeypatch.setattr(SessionEventStore, '_read_last_seq', original)

    event = fresh.append('message', message={'role': 'user', 'content': 'mine'})
    seqs = [e['seq'] for e in SessionEventStore.open(path).read_events()]
    assert len(seqs) == len(set(seqs)), f'出现重复 seq：{seqs}'
    assert event['seq'] == max(seqs), f'新事件应拿到最大 seq：{seqs}'


def test_summary_cache_is_not_poisoned_by_a_racing_append(tmp_path, monkeypatch):
    """摘要读完、落键前追加一条：不能把旧摘要挂到新版本键上。"""
    path = tmp_path / 'sessions' / 'session_summary.jsonl'
    path.parent.mkdir(parents=True)
    store = SessionEventStore.open(path)
    store.append_message({'role': 'user', 'content': 'one'})
    agent_runtime._summary_cache.clear()

    real = SessionEventStore.session_summary
    state = {'raced': False}

    def racy_summary(self):
        summary = real(self)
        if not state['raced']:
            state['raced'] = True
            _append_line(self.path, self.session_id, self.last_seq + 1)
        return summary

    monkeypatch.setattr(SessionEventStore, 'session_summary', racy_summary)
    agent_runtime._cached_summary(store)
    monkeypatch.setattr(SessionEventStore, 'session_summary', real)

    # 同一版本再读一次：必须看到那条追加，而不是缓存里的旧摘要
    fresh = SessionEventStore.open(path)
    cached = agent_runtime._cached_summary(fresh)
    actual = real(fresh)
    assert cached.get('events') == actual.get('events'), (cached, actual)
