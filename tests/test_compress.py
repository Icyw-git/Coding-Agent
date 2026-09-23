"""用 mock 数据测试 harness.py 的上下文压缩逻辑（OpenAI 消息格式）。

运行: python test_compress.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness

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
                assert False, f"{label}: orphan tool message {m.get('tool_call_id')}"
            pending[m["tool_call_id"]] = True
        else:
            assert not pending or all(pending.values()), \
                f"{label}: dangling tool_calls {pending}"
            pending = {}
    assert not pending or all(pending.values()), f"{label}: dangling tool_calls at end"


# ---------- 用例 ----------

def test_snip_compact():
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


def test_snip_compact_tool_groups():
    """真实会话常见"一个 assistant 带多个工具"分组：裁剪后不得出现孤儿 tool 消息。"""
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


def test_snip_compact_head_no_dangling():
    """头部边界（head_end=3）落在多工具分组中间：裁剪后不得出现 dangling assistant。"""
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


def test_micro_compact():
    msgs = build_conversation(6, content_len=200)   # 每个工具结果 200+ 字符
    out = harness.micro_compact(msgs)
    tool_msgs = [m for m in out if m["role"] == "tool"]
    assert len(tool_msgs) == 6
    for m in tool_msgs[:-KEEP_RECENT]:
        assert str(m["content"]).startswith("[Earlier tool result compacted"), m
    for m in tool_msgs[-KEEP_RECENT:]:
        assert str(m["content"]).startswith("output-"), "最近的结果不应被压缩"
    # 结构仍有效
    assert all("tool_call_id" in m for m in tool_msgs)


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
