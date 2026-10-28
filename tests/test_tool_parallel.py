"""tool call 并行执行测试：只读白名单并行、可变串行兜底、结果原序写回。

运行: python test_tool_parallel.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness
import hooks
import test_context_manage as tcm
from session_store import SessionEventStore


class Recorder:
    """假工具：记录每次执行的 (start, end) 时间窗与调用参数，可指定耗时。"""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.spans = []

    def __call__(self, **kwargs):
        start = time.perf_counter()
        time.sleep(self.delay)
        self.spans.append((start, time.perf_counter()))
        return 'out:' + json.dumps(kwargs, sort_keys=True, ensure_ascii=False)


def _memory_isolated(tmp_path, monkeypatch):
    mem = tmp_path / 'mem'
    monkeypatch.setattr(harness, 'MEMORY_DIR', mem)
    monkeypatch.setattr(harness, 'MEMORY_INDEX', mem / 'MEMORY.md')


def _tool_results(store):
    return [(e['message'].get('tool_call_id'), e['message']['content'])
            for e in store.read_events()
            if e.get('type') == 'message' and (e.get('message') or {}).get('role') == 'tool']


def test_readonly_batch_runs_in_parallel_and_in_order(tmp_path, monkeypatch):
    rec = Recorder(delay=0.25)
    monkeypatch.setattr(harness, 'tool_registry', {'read': rec})
    _memory_isolated(tmp_path, monkeypatch)
    store = SessionEventStore(tmp_path, session_id='par')
    fake = tcm.FakeCompletions([
        tcm.make_tool_response([
            ('c1', 'read', '{"path": "a"}'),
            ('c2', 'read', '{"path": "b"}'),
            ('c3', 'read', '{"path": "c"}'),
        ]),
        tcm.make_text_response('done'),
        tcm.make_text_response('[]'),   # 结束提炼
    ])
    monkeypatch.setattr(harness.client.chat.completions, 'create', fake.create)
    harness.agent_loop([{'role': 'user', 'content': '并行读三个文件'}],
                       session_store=store)
    harness.wait_for_housekeeping(30)

    # 结果按模型给定的原序写回
    assert [content for _, content in _tool_results(store)] == ['out:{"path": "a"}',
                                                                'out:{"path": "b"}',
                                                                'out:{"path": "c"}']
    # started 事件原序、数量正确
    started = [e['name'] for e in store.read_events() if e.get('type') == 'tool_started']
    assert started == ['read', 'read', 'read']
    # 并行：三个执行窗口相互重叠（串行则必然首尾相接）
    spans = rec.spans
    assert len(spans) == 3
    assert max(s for s, _ in spans) < min(e for _, e in spans)


def test_readonly_bash_batch_runs_in_parallel(tmp_path, monkeypatch):
    """整批 bash 且每条命令都在只读白名单内 → 与只读文件工具一样并行；
    混入任一非只读命令（解释器/重定向/写命令）→ 整批串行兜底。"""
    rec = Recorder(delay=0.25)
    monkeypatch.setattr(harness, 'tool_registry', {'bash': rec})
    _memory_isolated(tmp_path, monkeypatch)
    store = SessionEventStore(tmp_path, session_id='bash-par')
    fake = tcm.FakeCompletions([
        tcm.make_tool_response([
            ('b1', 'bash', '{"command": "git status"}'),
            ('b2', 'bash', '{"command": "git log --oneline"}'),
            ('b3', 'bash', '{"command": "ls"}'),
        ]),
        tcm.make_text_response('done'),
        tcm.make_text_response('[]'),
    ])
    monkeypatch.setattr(harness.client.chat.completions, 'create', fake.create)
    harness.agent_loop([{'role': 'user', 'content': '并行跑三条只读命令'}],
                       session_store=store)
    harness.wait_for_housekeeping(30)

    spans = rec.spans
    assert len(spans) == 3
    assert max(s for s, _ in spans) < min(e for _, e in spans), "只读 bash 批应并行"
    # 原序写回（并行不改变事件流顺序契约）
    assert [content for _, content in _tool_results(store)] == ['out:{"command": "git status"}',
                                                                'out:{"command": "git log --oneline"}',
                                                                'out:{"command": "ls"}']


def test_bash_batch_with_mutable_command_serializes(tmp_path, monkeypatch):
    """批内任一命令不在只读白名单（解释器/重定向）→ 整批串行。

    注意命令选例：'python -m pytest' 会命中 is_slow_operation 的慢命令路由进
    后台任务，与本测的串并行无关——用 'python -c'（opaque ASK、非慢命令）。"""
    rec = Recorder(delay=0.15)
    monkeypatch.setattr(harness, 'tool_registry', {'bash': rec})
    # 本测的是调度串并行，不测审批：解释器命令会触发 ASK，直接放行
    monkeypatch.setattr(hooks.GATE, 'decide',
                        lambda tool, args, reason, **kw: (True, 'approved-by-test'))
    _memory_isolated(tmp_path, monkeypatch)
    store = SessionEventStore(tmp_path, session_id='bash-serial')
    fake = tcm.FakeCompletions([
        tcm.make_tool_response([
            ('b1', 'bash', '{"command": "git status"}'),
            ('b2', 'bash', '{"command": "python -c \\"import sys\\""}'),
        ]),
        tcm.make_text_response('done'),
        tcm.make_text_response('[]'),
    ])
    monkeypatch.setattr(harness.client.chat.completions, 'create', fake.create)
    harness.agent_loop([{'role': 'user', 'content': '串行兜底'}],
                       session_store=store)
    harness.wait_for_housekeeping(30)

    spans = rec.spans
    assert len(spans) == 2
    assert min(e for _, e in spans) <= max(s for s, _ in spans), "含解释器命令 → 串行"


def test_mixed_batch_falls_back_to_serial(tmp_path, monkeypatch):
    rec_read = Recorder(delay=0.2)
    rec_bash = Recorder(delay=0.2)
    monkeypatch.setattr(harness, 'tool_registry', {'read': rec_read, 'bash': rec_bash})
    _memory_isolated(tmp_path, monkeypatch)
    store = SessionEventStore(tmp_path, session_id='serial')
    fake = tcm.FakeCompletions([
        tcm.make_tool_response([
            ('c1', 'read', '{"path": "a"}'),
            ('c2', 'bash', '{"command": "echo hi"}'),
        ]),
        tcm.make_text_response('done'),
        tcm.make_text_response('[]'),
    ])
    monkeypatch.setattr(harness.client.chat.completions, 'create', fake.create)
    harness.agent_loop([{'role': 'user', 'content': '先读后跑'}],
                       session_store=store)
    harness.wait_for_housekeeping(30)

    spans = rec_read.spans + rec_bash.spans
    assert len(spans) == 2
    # 串行：执行窗口首尾相接、互不重叠
    assert min(e for _, e in spans) <= max(s for s, _ in spans)
    # 顺序保持：read 先于 bash
    assert [content for _, content in _tool_results(store)] == ['out:{"path": "a"}',
                                                                'out:{"command": "echo hi"}']


def test_single_tool_call_unchanged(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(harness, 'tool_registry', {'read': rec})
    _memory_isolated(tmp_path, monkeypatch)
    store = SessionEventStore(tmp_path, session_id='single')
    fake = tcm.FakeCompletions([
        tcm.make_tool_response([('c1', 'read', '{"path": "only"}')]),
        tcm.make_text_response('done'),
        tcm.make_text_response('[]'),
    ])
    monkeypatch.setattr(harness.client.chat.completions, 'create', fake.create)
    harness.agent_loop([{'role': 'user', 'content': '单工具'}],
                       session_store=store)
    harness.wait_for_housekeeping(30)
    assert _tool_results(store) == [('c1', 'out:{"path": "only"}')]
