"""用 mock 数据测试 harness.py 的上下文压缩逻辑（OpenAI 消息格式）。

运行: python test_compress.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness
from session_store import SessionEventStore

KEEP_RECENT = harness.KEEP_RECENT
PERSIST = harness.PERSIST_THERSHOLD


# ---------- mock 数据构造 ----------

def make_round(i, content_len=50):
    """构造一轮 OpenAI 格式的 工具调用 + 工具结果。"""
    call_id = f"call_{i:03d}"
    assistant = {
        "role": "assistant",
        "content": f"step {i}: running tool",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": "bash", "arguments": f'{{"command": "echo {i}"}}'},
        }],
    }
    tool = {
        "role": "tool",
        "tool_call_id": call_id,
        "content": f"output-{i}: " + ("y" * content_len),
    }
    return assistant, tool


def build_conversation(n_rounds, content_len=50):
    """构造 system + user + N 轮工具调用 + 收尾 user 消息。"""
    msgs = [
        {"role": "system", "content": "You are a test agent"},
        {"role": "user", "content": "do the task"},
    ]
    for i in range(n_rounds):
        assistant, tool = make_round(i, content_len=content_len)
        msgs += [assistant, tool]
    msgs.append({"role": "user", "content": "final answer request"})
    return msgs


def validate_pairs(msgs, label):
    """校验：assistant 的 tool_calls 在下一个非 tool 消息之前必须有完整结果。"""
    pending = {}
    for m in msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                pending[tc["id"]] = False
        elif m.get("role") == "tool":
            if m.get("tool_call_id") not in pending:
                raise AssertionError(f"{label}: orphan tool message {m.get('tool_call_id')}")
            pending[m["tool_call_id"]] = True
        else:
            assert not pending or all(pending.values()), \
                f"{label}: dangling tool_calls {pending}"
            pending = {}
    assert not pending or all(pending.values()), f"{label}: dangling tool_calls at end"


# ---------- 用例 ----------

def test_snip_compact(monkeypatch):
    monkeypatch.setattr(harness, 'CONTEXT_LIMIT', 45_888)   # 固定阈值 50，与窗口配置解耦
    msgs = build_conversation(30)          # 2 + 60 + 1 = 63 条，超过 50
    assert len(msgs) > 50
    out = harness.snip_compact(msgs)
    assert len(out) <= 50, f"snip 后仍有 {len(out)} 条"
    assert any(m.get("role") == "user" and str(m.get("content", "")).startswith("[snipped")
               for m in out), "缺少 [snipped N messages] 标记"
    validate_pairs(out, "snip")


def test_snip_noop_when_short():
    msgs = build_conversation(10)          # 23 条 <= 50
    out = harness.snip_compact(msgs)
    assert out is msgs, "短对话不应产生新列表"
    assert len(out) == 23


def test_snip_compact_tool_groups(monkeypatch):
    """真实会话常见"一个 assistant 带多个工具"分组：裁剪后不得出现孤儿 tool 消息。"""
    monkeypatch.setattr(harness, 'CONTEXT_LIMIT', 45_888)   # 固定阈值 50
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    for i in range(20):
        cids = [f"c{i}_{j}" for j in range(2)]
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": cid, "type": "function",
                                     "function": {"name": "bash", "arguments": "{}"}}
                                    for cid in cids]})
        for cid in cids:
            msgs.append({"role": "tool", "tool_call_id": cid, "content": "y" * 50})
    msgs.append({"role": "user", "content": "final"})     # 2 + 60 + 1 = 63 条
    out = harness.snip_compact(msgs)
    assert len(out) <= 50
    validate_pairs(out, "snip-groups")


def test_snip_compact_head_no_dangling(monkeypatch):
    """头部边界（head_end=3）落在多工具分组中间：裁剪后不得出现 dangling assistant。"""
    monkeypatch.setattr(harness, 'CONTEXT_LIMIT', 45_888)   # 固定阈值 50
    msgs = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "h0", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}},
                        {"id": "h1", "type": "function",
                         "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "h0", "content": "x" * 50},   # 索引 2 → head_end=3 落在这里
        {"role": "tool", "tool_call_id": "h1", "content": "x" * 50},
    ]
    for i in range(24):                                          # 每个 assistant 带 2 个工具
        cids = [f"c{i}_{j}" for j in range(2)]
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": cid, "type": "function",
                                     "function": {"name": "bash", "arguments": "{}"}}
                                    for cid in cids]})
        for cid in cids:
            msgs.append({"role": "tool", "tool_call_id": cid, "content": "y" * 50})
    msgs.append({"role": "user", "content": "final"})            # 4 + 72 + 1 = 77 条
    assert len(msgs) > 50
    out = harness.snip_compact(msgs)
    assert len(out) <= 50
    validate_pairs(out, "snip-head")


def test_detectors():
    a, t = make_round(1)
    assert harness._message_has_tool_use(a)
    assert not harness._message_has_tool_use(t)
    assert harness._is_tool_result_message(t)
    assert not harness._is_tool_result_message(a)


def test_collect_tool_results():
    msgs = build_conversation(5)
    results = harness.collect_tool_results(msgs)
    assert len(results) == 5
    assert all(m["role"] == "tool" for m in results)
    assert all("tool_call_id" in m for m in results)


def test_micro_compact(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'CONTEXT_LIMIT', 400)   # 降低门槛：只验证改写路径本身
    monkeypatch.setattr(harness, 'TOOL_RESULTS_DIR', tmp_path / 'tool_results')
    # 轮数必须 > KEEP_RECENT，否则 eligible<=0、改写断言空转通过
    store = SessionEventStore(tmp_path, session_id='mc')
    for m in build_conversation(6 + KEEP_RECENT, content_len=200):
        store.append_message(m)
    out = harness.micro_compact(store.rebuild_openai_history(), session_store=store)
    tool_msgs = [m for m in out if m["role"] == "tool"]
    assert len(tool_msgs) == 6 + KEEP_RECENT
    for m in tool_msgs[:-KEEP_RECENT]:
        assert str(m["content"]).startswith("[Earlier tool result compacted"), m
        assert "bash(" in m["content"], "占位符必须带工具身份（名称+参数）"
        assert "Full output saved" in m["content"], "完整输出必须落盘（零丢失）"
    for m in tool_msgs[-KEEP_RECENT:]:
        assert str(m["content"]).startswith("output-"), "最近的结果不应被压缩"
    # 决策已持久化为事件：再次 rebuild 得到逐字相同的占位（跨轮/跨重启稳定）
    again = [m["content"] for m in store.rebuild_openai_history() if m["role"] == "tool"]
    assert again == [m["content"] for m in tool_msgs]
    # 落盘文件真实存在且内容完整
    import re
    saved = re.search(r"Full output saved: (.+?)\. Re-run", tool_msgs[0]["content"])
    assert saved and Path(saved.group(1)).read_text(encoding="utf-8").startswith("output-0:")
    # 结构仍有效
    assert all("tool_call_id" in m for m in tool_msgs)


def test_micro_compact_gated_below_half_context():
    # 前缀缓存保护：上下文不过半时不得改写历史（否则 KV 缓存 84%→6% 断崖）
    msgs = build_conversation(12, content_len=200)
    out = harness.micro_compact(msgs)
    tool_msgs = [m for m in out if m["role"] == "tool"]
    assert all(str(m["content"]).startswith("output-") for m in tool_msgs), \
        "低水位下历史必须保持逐字稳定"


def test_micro_compact_hysteresis_keeps_prefix_stable(tmp_path, monkeypatch):
    """滑动窗口每轮越界 1 条：攒批改写（≥4），批间占位逐字稳定（缓存命中关键）。"""
    monkeypatch.setattr(harness, 'CONTEXT_LIMIT', 400)
    monkeypatch.setattr(harness, 'TOOL_RESULTS_DIR', tmp_path / 'tr')
    store = SessionEventStore(tmp_path, session_id='hys')
    for m in build_conversation(14, content_len=200):
        store.append_message(m)

    def step(n_tools):
        # 取前 n_tools 个工具结果对应的历史前缀（模拟会话增长到该轮）
        seen, msgs = 0, []
        for m in store.rebuild_openai_history():
            if m.get('role') == 'tool':
                seen += 1
            msgs.append(m)
            if seen >= n_tools:
                break
        out = harness.micro_compact(msgs, size=99999, session_store=store)
        return [m['content'] for m in out if m.get('role') == 'tool']

    t10 = step(10)   # eligible=4 → 首批改写 4 条
    assert sum(c.startswith('[Earlier tool result compacted') for c in t10) == 4
    t12 = step(12)   # eligible=6，新越界 2 < 4 → 本轮不改写
    assert t12[:4] == t10[:4]                                   # 批间占位逐字稳定
    assert all(c.startswith('output-') for c in t12[4:])        # 新越界保持原文
    t14 = step(14)   # eligible=8，新越界 4 → 第二批
    assert sum(c.startswith('[Earlier tool result compacted') for c in t14) == 8
    assert t14[4:6] != t12[4:6]                                 # 第二批覆盖新越界条目
    # 决策持久化：rebuild 仍得到相同占位（跨重启稳定）
    again = [m['content'] for m in store.rebuild_openai_history() if m.get('role') == 'tool']
    assert again == t14


def test_persist_large_output():
    big = "z" * (PERSIST + 100)
    marker = harness.persist_large_output("call_big", big)
    assert "<persisted-output>" in marker
    assert "Full output:" in marker and "Preview:" in marker
    path = harness.TOOL_RESULTS_DIR / "call_big.txt"
    assert path.exists(), f"大输出未落盘: {path}"
    assert len(path.read_text(encoding="utf-8")) == len(big)
    path.unlink()


def test_tool_result_budget():
    msgs = build_conversation(4)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    created = []
    for m in tool_msgs[:2]:
        m["content"] = "B" * (PERSIST + 1000)
    out = harness.tool_result_budget(msgs, max_bytes=4000)
    out_tools = [m for m in out if m["role"] == "tool"]
    for m in out_tools[:2]:
        assert str(m["content"]).startswith("<persisted-output>"), "超预算输出应被持久化"
        created.append(harness.TOOL_RESULTS_DIR / f"{m['tool_call_id']}.txt")
    # 清理落盘文件
    for p in created:
        if p.exists():
            p.unlink()


# ---------- 窗口参数与单位（前缀缓存诊断后修正） ----------

def test_model_window_deepseek_1m(monkeypatch):
    """DeepSeek 现行名与退役旧名都注册 1M（官方：V4.1-Flash/V4-Pro 上下文 1M，
    旧名路由到同模型）——.env 的 deepseek-flash 不得再回落 64K。"""
    for name in ('deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-pro',
                 'deepseek-chat', 'deepseek-reasoner'):
        assert harness.MODEL_WINDOWS.get(name) == 1_048_576, name
    monkeypatch.setenv('LLM_MODEL_ID', 'deepseek-flash')
    monkeypatch.setenv('LLM_CONTEXT_WINDOW', '')
    assert harness._default_context_limit() == int((1_048_576 - harness.OUTPUT_RESERVE) * harness.CTX_SAFETY)
    monkeypatch.setenv('LLM_MODEL_ID', 'unknown-model')   # 未知模型保守回落
    assert harness._default_context_limit() == int((65536 - harness.OUTPUT_RESERVE) * harness.CTX_SAFETY)
    monkeypatch.setenv('LLM_CONTEXT_WINDOW', '100000')    # env 显式配置优先
    assert harness._default_context_limit() == int((100000 - harness.OUTPUT_RESERVE) * harness.CTX_SAFETY)


def test_estimate_size_units():
    """CJK 单位修正：1.3 字符/token（除法）。旧算法 cjk*1.3 高估 1.69 倍，
    会让全部压缩阈值提前触发。"""
    ascii_est = harness.estimate_size([{'role': 'user', 'content': 'a' * 3500}])
    assert abs(ascii_est - 1000) < 30          # 3500/3.5 = 1000
    cjk_est = harness.estimate_size([{'role': 'user', 'content': '中' * 1300}])
    assert abs(cjk_est - 1000) < 30            # 1300/1.3 = 1000（旧算法 ≈1690）


def test_snip_threshold_scales_with_window(monkeypatch):
    """消息数安全阀随窗口缩放：默认窗口阈值 50，1M 窗口 262——
    大窗口下 snip 不应再中途触发（滑动窗口是前缀缓存杀手）。"""
    msgs = build_conversation(30)              # 63 条
    monkeypatch.setattr(harness, 'CONTEXT_LIMIT', 45_888)
    given = list(msgs)
    out = harness.snip_compact(given)
    assert out is not given and len(out) <= 50 # 默认窗口：照常触发
    monkeypatch.setattr(harness, 'CONTEXT_LIMIT', 1_048_576)
    given2 = list(msgs)
    assert harness.snip_compact(given2) is given2     # 1M 窗口：63 条远未到 262，不触发
    assert harness.snip_compact(given2, max_messages=10) is not given2  # 显式参数仍生效


def test_snip_persisted_and_replayed(tmp_path, monkeypatch):
    """snip 触发即写 history_snipped 索引事件（head_end/tail_start/snipped 三个整数）：
    rebuild 用同一切片表达式还原，与内存形态逐字一致；跨重启（新 store 实例）相同；
    旧版 head/tail 副本事件走兼容分支。"""
    monkeypatch.setattr(harness, 'CONTEXT_LIMIT', 45_888)   # 阈值 50
    store = SessionEventStore(tmp_path, session_id='snip')
    msgs = build_conversation(30)
    for m in msgs:   # 真实时序：消息先落盘，pre_compact 作用于已落盘历史
        store.append_message(m)
    out, _ = harness.pre_compact_messages(store.rebuild_openai_history(),
                                          session_store=store)
    assert any(str(m.get('content', '')).startswith('[snipped ') for m in out)
    events = [e for e in store.read_events() if e['type'] == 'history_snipped']
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev['head_end'], int) and isinstance(ev['tail_start'], int)
    assert ev['snipped'] == ev['tail_start'] - ev['head_end']
    assert 'head' not in ev and 'tail' not in ev, "索引事件不应再携带 head/tail 副本"
    # rebuild 与内存形态逐字一致（结构保证：同一表达式）
    assert [m['content'] for m in store.rebuild_openai_history()] == [m['content'] for m in out]
    # 跨重启
    reopened = SessionEventStore.open(store.path)
    assert [m['content'] for m in reopened.rebuild_openai_history()] == [m['content'] for m in out]
    # 再次触发：新消息先落盘（索引引用的是回放态位置），rebuild 跟随最后一次决策
    for m in [{'role': 'user', 'content': 'x' * 200}] * 8:
        store.append_message(m)
    out2, _ = harness.pre_compact_messages(store.rebuild_openai_history(),
                                           session_store=store)
    events2 = [e for e in store.read_events() if e['type'] == 'history_snipped']
    assert len(events2) == 2
    assert [m['content'] for m in store.rebuild_openai_history()] == [m['content'] for m in out2]


def test_snip_legacy_copy_event_compat(tmp_path):
    """旧版 head/tail 全量副本事件（索引化之前产生）仍可回放。"""
    store = SessionEventStore(tmp_path, session_id='legacy')
    head = [{'role': 'user', 'content': '开头'},
            {'role': 'assistant', 'content': '回复'}]
    tail = [{'role': 'user', 'content': '最近的问题'},
            {'role': 'assistant', 'content': '最近的回答'}]
    store.append('history_snipped', head=head, tail=tail, snipped=5)
    history = store.rebuild_openai_history()
    assert [m['content'] for m in history] == ['开头', '回复',
                                               '[snipped 5 messages]',
                                               '最近的问题', '最近的回答']


def test_snip_hysteresis_between_fires(tmp_path, monkeypatch):
    """snip 触发迟滞（对齐 Codex「越线一次性替换」）：无迟滞时越线后每回合滑动，
    marker 每次变文本 + 尾段位移 → 前缀每回合断裂（实测命中率 6-19%）。
    迟滞：触发后下次触发线 = 压缩后条数 + 阈值/4，两次触发之间逐字稳定。"""
    monkeypatch.setattr(harness, 'CONTEXT_LIMIT', 45_888)   # 阈值 50
    store = SessionEventStore(tmp_path, session_id='hys')
    for m in build_conversation(30):                        # 63 条 > 50 → 首次触发
        store.append_message(m)
    state = {}
    msgs = store.rebuild_openai_history()
    out1, _ = harness.pre_compact_messages(msgs, session_store=store, snip_state=state)
    assert state['fire_at'] == len(out1) + max(8, len(out1) // 4), "触发后武装迟滞线"
    # 基线附近增长（+6 < 迟滞量 12）：不触发，历史逐字稳定（前缀缓存可命中）
    for m in [{'role': 'user', 'content': 'g'}] * 6:
        store.append_message(m)
    msgs = store.rebuild_openai_history()
    out2, _ = harness.pre_compact_messages(msgs, session_store=store, snip_state=state)
    events = [e for e in store.read_events() if e['type'] == 'history_snipped']
    assert len(events) == 1, "迟滞线内不得再次触发"
    assert [m['content'] for m in out2] == [m['content'] for m in msgs], "两次触发之间逐字稳定"
    # 越过迟滞线（+15 > 12）：再次触发
    for m in [{'role': 'user', 'content': 'h'}] * 15:
        store.append_message(m)
    out3, _ = harness.pre_compact_messages(store.rebuild_openai_history(),
                                           session_store=store, snip_state=state)
    events = [e for e in store.read_events() if e['type'] == 'history_snipped']
    assert len(events) == 2, "越过迟滞线后再次触发"
    assert state['fire_at'] == len(out3) + max(8, len(out3) // 4)


def test_bound_tool_output_middle_out(tmp_path, monkeypatch):
    """入口 middle-out 截断：保头 + 保尾 + 截断标注；全量落盘可恢复；
    阈值内原样返回（不产生落盘）。"""
    monkeypatch.setattr(harness, 'TOOL_RESULTS_DIR', tmp_path / 'tr')
    monkeypatch.setattr(harness, 'TOOL_RESULT_CHAR_LIMIT', 24_000)
    small = 'x' * 1000
    assert harness._bound_tool_output('c_small', small) == small   # 阈值内原样
    # 头部是声明、尾部是错误摘要（最有用的部分在尾）
    big = ('HEADER line\n' + 'm' * 30_000 + '\nERROR: tests failed at the very end')
    out = harness._bound_tool_output('c_big', big)
    assert out.startswith('HEADER line'), "保头"
    assert out.endswith('ERROR: tests failed at the very end'), "保尾（旧 persist 预览只留头）"
    assert 'chars truncated' in out and 'Full output:' in out
    assert len(out) < len(big) // 2, "截断后显著变小"
    # 全量落盘可恢复
    path = tmp_path / 'tr' / 'c_big.txt'
    assert path.exists() and path.read_text(encoding='utf-8') == big


def test_per_call_usage_events(tmp_path, monkeypatch):
    """usage 事件逐调用落盘：一次工具回合（2 次 LLM 调用）→ 2 条事件，
    每条携带该次请求的 prompt；聚合口径与回合累计一致。"""
    from types import SimpleNamespace as NS
    store = SessionEventStore(tmp_path, session_id='ucall')
    mem = tmp_path / 'mem'
    monkeypatch.setattr(harness, 'MEMORY_DIR', mem)
    monkeypatch.setattr(harness, 'MEMORY_INDEX', mem / 'MEMORY.md')
    u1 = NS(prompt_tokens=100, completion_tokens=10, total_tokens=110,
            prompt_tokens_details=NS(cached_tokens=80))
    u2 = NS(prompt_tokens=200, completion_tokens=5, total_tokens=205,
            prompt_tokens_details=NS(cached_tokens=180))

    def fake_create(**kwargs):
        return fake_create.queue.pop(0)
    fake_create.queue = [
        NS(choices=[NS(message=NS(role='assistant', content=None,
                                  tool_calls=[NS(id='c1', type='function',
                                                 function=NS(name='noop', arguments='{}'))]),
                          finish_reason='tool_calls')], usage=u1),
        NS(choices=[NS(message=NS(role='assistant', content='done', tool_calls=None),
                          finish_reason='stop')], usage=u2),
    ]
    monkeypatch.setattr(harness, 'tool_registry', {'noop': lambda **kw: 'ok'})
    monkeypatch.setattr(harness.client.chat.completions, 'create', fake_create)
    harness.agent_loop([{'role': 'user', 'content': '两次调用'}], session_store=store)
    harness.wait_for_housekeeping(30)
    events = [e for e in store.read_events() if e.get('type') == 'usage']
    assert [e['prompt'] for e in events] == [100, 200], "每次调用一条 usage 事件"
    assert [e['cached'] for e in events] == [80, 180]
    totals = store.usage_totals()
    assert totals['prompt'] == 300 and totals['total'] == 315 and totals['calls'] == 2


def test_tail_within_budget(tmp_path, monkeypatch):
    """尾部保留：条数上限 + token 预算双约束（500KB 尾部不再原样保留）。"""
    monkeypatch.setattr(harness, 'CONTEXT_LIMIT', 800_000)
    big = {'role': 'tool', 'tool_call_id': 't', 'content': 'z' * 60_000}
    small = [{'role': 'user', 'content': 'q'}]
    msgs = small + [dict(big, tool_call_id=f't{i}') for i in range(10)]
    # 条数约束：keep=3 → 最多 3 条
    start = harness._tail_within_budget(msgs, keep=3, budget_tokens=None)
    assert len(msgs) - start <= 3
    # 预算约束：每条 ≈17k tokens，预算 20k → 只留最后 1 条（至少保留一条）
    start = harness._tail_within_budget(msgs, keep=10, budget_tokens=20_000)
    assert len(msgs) - start <= 2, "预算收紧尾部（17k+17k > 20k）"
    # 至少保留最后一条（即使单条超预算）
    huge = [{'role': 'user', 'content': 'w' * 90_000}]
    start = harness._tail_within_budget(huge, keep=5, budget_tokens=100)
    assert start == 0


def test_summary_input_keeps_goal_and_budget():
    """summarize 输入：第一条 user（原始目标）始终保留；尾部按预算整条取，
    不再切在 JSON 中间。"""
    goal = {'role': 'user', 'content': '重构缓存模块，禁止改公共接口'}
    huge_mid = [{'role': 'assistant', 'content': 'a' * 50_000} for _ in range(5)]
    tail_note = {'role': 'user', 'content': '最新发现：预算计算有误'}
    msgs = [goal] + huge_mid + [tail_note]
    text = harness._summary_input(msgs, budget=30_000)
    assert text.startswith('[原始目标] 重构缓存模块'), "原始目标始终保留"
    assert '最新发现：预算计算有误' in text, "最新尾部保留"
    assert len(text) < 60_000, "受预算约束"


def test_tool_result_budget_persists_placeholders(tmp_path, monkeypatch):
    """budget 改写走 tool_compacted 事件：跨轮 + 跨重启占位稳定（原实现只改内存，
    重启 rebuild 还原原文导致前缀断裂）。"""
    monkeypatch.setattr(harness, 'TOOL_RESULTS_DIR', tmp_path / 'tr')
    store = SessionEventStore(tmp_path, session_id='budget')
    msgs = [
        {'role': 'user', 'content': 'u'},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'c1', 'type': 'function', 'function': {'name': 'bash', 'arguments': '{}'}},
            {'id': 'c2', 'type': 'function', 'function': {'name': 'bash', 'arguments': '{}'}},
        ]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'a' * (PERSIST + 1000)},
        {'role': 'tool', 'tool_call_id': 'c2', 'content': 'b' * (PERSIST + 2000)},
    ]
    for m in msgs:   # 真实时序：原始消息先落盘，budget 作用于 rebuild 出的历史
        store.append_message(m)
    out = harness.tool_result_budget(store.rebuild_openai_history(),
                                     max_bytes=4000, session_store=store)
    out_tools = [m for m in out if m['role'] == 'tool']
    assert all(str(m['content']).startswith('<persisted-output>') for m in out_tools)
    # rebuild 与内存一致（事件已应用占位）
    rebuilt_tools = [m for m in store.rebuild_openai_history() if m['role'] == 'tool']
    assert [m['content'] for m in rebuilt_tools] == [m['content'] for m in out_tools]
    # 跨重启
    reopened = SessionEventStore.open(store.path)
    rebuilt2 = [m for m in reopened.rebuild_openai_history() if m['role'] == 'tool']
    assert [m['content'] for m in rebuilt2] == [m['content'] for m in out_tools]
    # 原始大输出事件保留在流中（append-only，undo/rewind 可回溯）
    raw = [e for e in store.read_events()
           if e.get('type') == 'message' and (e.get('message') or {}).get('role') == 'tool']
    assert len(raw) == 2 and len(raw[0]['message']['content']) == PERSIST + 1000


# ---------- 运行 ----------

def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"[ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
