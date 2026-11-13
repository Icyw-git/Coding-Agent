"""tool call 并行执行测试：只读白名单并行、可变串行兜底、结果原序写回。

运行: python test_tool_parallel.py
"""
import contextvars
import json
import sys
import threading
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


# ============================================================
# R0-d：并行工具必须继承父回合的执行上下文
# ============================================================
# 缺陷：`_run_tool_batch` 的 worker 里调 `contextvars.copy_context()`——那时线程
# 上下文是**空**的，父回合的 workspace / session / turn / taint / 一次性授权都丢。
# 设计文档 §2.2 / §10 的探针记录：两个 read 都从 B 退到了全局回退值。


def _tool_call(call_id, name='read'):
    return type('TC', (), {
        'id': call_id,
        'function': type('F', (), {'name': name, 'arguments': '{}'})(),
    })()


def test_parallel_tools_inherit_the_turn_workspace(tmp_path, monkeypatch):
    """父回合 workspace=B、全局回退=A → 两个并行工具都必须读到 B。

    同时用一个自定义 ContextVar 验证「非 workspace 的上下文」也传到了线程里
    （设计文档要求分别验收 workspace / session / taint / 取消）。
    """
    import tool_dispatch
    import workspace_context

    fallback = tmp_path / 'A'
    selected = tmp_path / 'B'
    fallback.mkdir()
    selected.mkdir()

    marker = contextvars.ContextVar('probe_marker', default='-')
    barrier = threading.Barrier(2, timeout=10)
    seen = []

    def fake_exec(tool_call, **_kwargs):
        barrier.wait()      # 两个必须同时在跑：证明这是并行批，且没被串行化
        seen.append((tool_call.id,
                     workspace_context.current_workspace(fallback),
                     marker.get()))
        return ('read', {}, {'role': 'tool', 'tool_call_id': tool_call.id,
                             'content': 'ok'}, [])

    monkeypatch.setattr(harness, 'exec_tool_call', fake_exec)
    monkeypatch.setattr(tool_dispatch, 'PARALLEL_SAFE_TOOLS', frozenset({'read'}))
    store = SessionEventStore(tmp_path / 'sessions', session_id='par-ctx')
    messages = []
    calls = [_tool_call('c1'), _tool_call('c2')]

    token = marker.set('set-by-parent')
    try:
        with workspace_context.use_workspace(selected):
            tool_dispatch._run_tool_batch(calls, store, messages)
    finally:
        marker.reset(token)

    assert sorted(row[0] for row in seen) == ['c1', 'c2'], seen   # 并行完成顺序不定
    for call_id, workspace, mark in seen:
        assert workspace == selected, (
            f'{call_id} 读到 {workspace}，退回了全局回退值 {fallback}')
        assert mark == 'set-by-parent', f'{call_id} 丢失自定义上下文（{mark}）'


def test_parallel_reads_merge_their_taint_back_into_the_turn(tmp_path, monkeypatch):
    """worker 内新产生的污点必须回流到回合上下文。

    并发时每个工具跑在自己的上下文副本里，`taint.mark()` 只改副本。不回收的话
    串行与并行两种路径行为不一致：读了敏感文件之后，同一回合的出网检查看不见它。
    """
    import taint
    import tool_dispatch

    barrier = threading.Barrier(2, timeout=10)

    def fake_exec(tool_call, **_kwargs):
        barrier.wait()
        taint.mark(f'sensitive-{tool_call.id}')
        return ('read', {}, {'role': 'tool', 'tool_call_id': tool_call.id,
                             'content': 'ok'}, [])

    monkeypatch.setattr(harness, 'exec_tool_call', fake_exec)
    monkeypatch.setattr(tool_dispatch, 'PARALLEL_SAFE_TOOLS', frozenset({'read'}))
    store = SessionEventStore(tmp_path / 'sessions', session_id='par-taint')

    taint.reset()
    tool_dispatch._run_tool_batch([_tool_call('c1'), _tool_call('c2')], store, [])

    merged = set(taint.sources())
    assert {'sensitive-c1', 'sensitive-c2'} <= merged, merged
    taint.reset()


def test_one_shot_grant_is_not_broadcast_to_siblings(tmp_path, monkeypatch):
    """一次性凭据不能随上下文快照复制给兄弟调用。

    修上下文传播时发现：`copy_context()` 会把 `hooks._GRANTS`（一次性授权）也复制
    过去，于是两个并行工具各消费同一份凭据——“批准一次”变成“放行两次”。
    设计契约：parallel worker 从空凭据集开始，各自走本次调用的正常审批。
    """
    import hooks
    import tool_dispatch

    barrier = threading.Barrier(2, timeout=10)
    seen = []

    def fake_exec(tool_call, **_kwargs):
        barrier.wait()
        seen.append((tool_call.id, hooks.take_grant('read', {'path': 'x'})))
        return ('read', {}, {'role': 'tool', 'tool_call_id': tool_call.id,
                             'content': 'ok'}, [])

    monkeypatch.setattr(harness, 'exec_tool_call', fake_exec)
    monkeypatch.setattr(tool_dispatch, 'PARALLEL_SAFE_TOOLS', frozenset({'read'}))
    store = SessionEventStore(tmp_path / 'sessions', session_id='par-grant')

    hooks.issue_grant('read', {'path': 'x'})
    try:
        tool_dispatch._run_tool_batch([_tool_call('c1'), _tool_call('c2')], store, [])
        assert [took for _, took in seen] == [False, False], seen
        # 父回合那份凭据要留着：它属于父上下文的调用，不该被并行批吃掉
        assert hooks.take_grant('read', {'path': 'x'}) is True
    finally:
        hooks.take_grants()


def test_parallel_tools_inherit_turn_restrictions_and_do_not_cross_talk(tmp_path, monkeypatch):
    """回合限制（会话/定时只读/主动性）要带进去；兄弟之间的写入不能互相串。

    「工作区对了」不等于全通过：设计文档要求这几项分别验收。
    """
    import hooks
    import tool_dispatch

    barrier = threading.Barrier(2, timeout=10)
    probe = contextvars.ContextVar('sibling_probe', default='untouched')
    seen = {}

    def fake_exec(tool_call, **_kwargs):
        seen[tool_call.id] = (hooks.current_session_id(),
                              hooks.scheduled_read_only(),
                              hooks.proactive_turn())
        if tool_call.id == 'c1':
            probe.set('written-by-c1')      # 只改自己那份上下文
        barrier.wait()
        seen[f'{tool_call.id}-probe'] = probe.get()
        return ('read', {}, {'role': 'tool', 'tool_call_id': tool_call.id,
                             'content': 'ok'}, [])

    monkeypatch.setattr(harness, 'exec_tool_call', fake_exec)
    monkeypatch.setattr(tool_dispatch, 'PARALLEL_SAFE_TOOLS', frozenset({'read'}))
    store = SessionEventStore(tmp_path / 'sessions', session_id='par-restrict')

    with hooks.use_session('sess-42'), hooks.use_scheduled_turn(read_only=True), \
            hooks.proactive_scope(True):
        tool_dispatch._run_tool_batch([_tool_call('c1'), _tool_call('c2')], store, [])

    assert seen['c1'] == ('sess-42', True, True), seen['c1']
    assert seen['c2'] == ('sess-42', True, True), seen['c2']
    assert seen['c1-probe'] == 'written-by-c1', 'c1 自己的写入应当可见'
    assert seen['c2-probe'] == 'untouched', 'c1 的写入不该串到 c2 的上下文'
    assert probe.get() == 'untouched', 'worker 的写入不该影响父上下文'
