"""用 mock 的 OpenAI 响应测试 agent_loop 的上下文管理流程。

覆盖：
- 正常多工具流 + 最终文本回复（工具结果以 role=tool 回填）
- 循环开头三道预处理（budget → snip → micro）的执行顺序
- 超过 CONTEXT_LIMIT 时自动 compact_history
- API 报 prompt_too_long 时 reactive compact + 重试 / 重试上限后抛错
- 模型调用 compact 工具：先执行 compact 之前的工具，再压缩重开

运行: python test_context_manage.py
"""
import copy
import json
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


# ---------- logger：统一输出带时间戳的调试日志，方便排查 ----------

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s',
                    datefmt='%H:%M:%S')
logger = logging.getLogger('test_context_manage')


def _fmt_msgs(msgs, limit=1500):
    """消息列表转截断 JSON，避免刷屏。"""
    text = json.dumps(msgs, ensure_ascii=False)
    return text[:limit] + ('...' if len(text) > limit else '')


def _roles_summary(msgs):
    """消息角色序列，如 user -> system -> assistant -> tool。"""
    return ' -> '.join(m.get('role', '?') for m in msgs)


# ---------- fake OpenAI 响应 ----------

class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self):
        d = {'role': 'assistant', 'content': self.content}
        if self.tool_calls:
            d['tool_calls'] = [
                {'id': tc.id, 'type': 'function',
                 'function': {'name': tc.function.name, 'arguments': tc.function.arguments}}
                for tc in self.tool_calls
            ]
        return d


class FakeChoice:
    def __init__(self, message, finish_reason):
        self.message = message
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, choice):
        self.choices = [choice]


class FakeCompletions:
    """按队列消费 items（response 或 Exception），并记录每次收到的 messages。"""

    def __init__(self, items):
        self.items = list(items)
        self.calls = []

    def create(self, **kwargs):
        # 深拷贝快照：每轮实际发送的消息，避免后续 append/extend 影响历史记录
        idx = len(self.calls) + 1
        payload = copy.deepcopy(kwargs.get('messages', []))
        self.calls.append(payload)
        logger.debug("API call #%d  model=%s  roles=[%s]",
                     idx, kwargs.get('model'), _roles_summary(payload))
        logger.debug("API call #%d payload:\n%s", idx, _fmt_msgs(payload))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            logger.warning("API call #%d raised %s: %s", idx, type(item).__name__, item)
            raise item
        finish = item.choices[0].finish_reason
        if finish == 'tool_calls':
            names = [tc.function.name for tc in item.choices[0].message.tool_calls]
            logger.debug("API call #%d -> finish_reason=%s tool_calls=%s", idx, finish, names)
        else:
            logger.debug("API call #%d -> finish_reason=%s content=%r",
                         idx, finish, (item.choices[0].message.content or '')[:200])
        return item


def make_text_response(text):
    return FakeResponse(FakeChoice(FakeMessage(content=text), finish_reason='stop'))


def make_tool_response(tool_calls):
    """tool_calls: [(id, name, arguments_json), ...]"""
    calls = [FakeToolCall(cid, name, args) for cid, name, args in tool_calls]
    return FakeResponse(FakeChoice(FakeMessage(content=None, tool_calls=calls),
                                   finish_reason='tool_calls'))


# ---------- 工具 / 方法打桩 ----------

def patch_tool(name, func):
    """替换 tool_registry 里的处理器，返回原函数用于恢复。"""
    original = harness.tool_registry[name]
    harness.tool_registry[name] = func
    return original


# ---------- 用例 ----------

def test_normal_tool_flow():
    """bash 工具执行 → 结果以 role=tool 回填 → 模型给出最终文本。"""
    calls = []
    orig_bash = patch_tool('bash', lambda command: calls.append(command) or f"out:{command}")
    orig_create = harness.client.chat.completions.create
    try:
        fake = FakeCompletions([
            make_tool_response([('c1', 'bash', '{"command": "echo hi"}')]),
            make_text_response('task done'),
        ])
        harness.client.chat.completions.create = fake.create
        msgs = [{'role': 'user', 'content': 'run it'}]
        result = harness.agent_loop(msgs)
        assert result == 'task done', result
        assert calls == ['echo hi'], f"工具未执行: {calls}"
        # 第二次 API 调用应包含 tool 结果消息
        msgs2 = fake.calls[1]
        assert any(m.get('role') == 'tool' and m.get('tool_call_id') == 'c1' for m in msgs2)
        assert 'out:echo hi' in json.dumps(msgs2, ensure_ascii=False)
    finally:
        harness.tool_registry['bash'] = orig_bash
        harness.client.chat.completions.create = orig_create


def test_preprocessors_order():
    """每轮循环开头必须按 budget → snip → micro 顺序执行预处理。"""
    names = ('tool_result_budget', 'snip_compact', 'micro_compact')
    originals = {n: getattr(harness, n) for n in names}
    order = []
    try:
        for n in names:
            orig = originals[n]
            setattr(harness, n, (lambda o, name: lambda m: (order.append(name),
                                                            logger.debug("[preprocess] %s called on %d msgs", name, len(m)),
                                                            o(m))[2])(orig, n))
        fake = FakeCompletions([make_text_response('ok')])
        orig_create = harness.client.chat.completions.create
        harness.client.chat.completions.create = fake.create
        try:
            harness.agent_loop([{'role': 'user', 'content': 'x'}])
        finally:
            harness.client.chat.completions.create = orig_create
        assert order == list(names), f"预处理顺序错误: {order}"
    finally:
        for n in names:
            setattr(harness, n, originals[n])


def test_auto_compact_over_limit():
    """estimate_size 超过 CONTEXT_LIMIT 时先 compact_history 再调 API。"""
    orig_limit = harness.CONTEXT_LIMIT
    orig_compact = harness.compact_history
    harness.CONTEXT_LIMIT = 50
    compact_calls = []
    harness.compact_history = lambda msgs: (compact_calls.append(len(msgs))
                                            or [{'role': 'user', 'content': '[Compacted] summary'}])
    orig_create = harness.client.chat.completions.create
    try:
        fake = FakeCompletions([make_text_response('ok')])
        harness.client.chat.completions.create = fake.create
        try:
            result = harness.agent_loop([{'role': 'user', 'content': 'hello ' * 200}])
        finally:
            harness.client.chat.completions.create = orig_create
        assert result == 'ok'
        assert len(compact_calls) == 1, f"应触发 1 次 compact_history: {len(compact_calls)}"
        # 第一次 API 调用收到的应是压缩后的上下文
        assert '[Compacted] summary' in json.dumps(fake.calls[0], ensure_ascii=False)
    finally:
        harness.CONTEXT_LIMIT = orig_limit
        harness.compact_history = orig_compact


def test_reactive_compact_retry_then_success():
    """API 报 prompt_too_long → reactive_compact 后重试成功。"""
    orig_reactive = harness.reactive_compact
    reactive_calls = []
    harness.reactive_compact = lambda msgs: (reactive_calls.append(len(msgs))
                                             or [{'role': 'user', 'content': '[Reactive compact] summary'}])
    orig_create = harness.client.chat.completions.create
    try:
        fake = FakeCompletions([
            RuntimeError('prompt_too_long: reduce input'),
            make_text_response('recovered'),
        ])
        harness.client.chat.completions.create = fake.create
        try:
            result = harness.agent_loop([{'role': 'user', 'content': 'x'}])
        finally:
            harness.client.chat.completions.create = orig_create
        assert result == 'recovered'
        assert len(reactive_calls) == 1
    finally:
        harness.reactive_compact = orig_reactive


def test_reactive_compact_exhausts_retries():
    """连续报错达到重试上限后应抛出原始异常（不再无限重试）。"""
    orig_reactive = harness.reactive_compact
    harness.reactive_compact = lambda msgs: [{'role': 'user', 'content': '[Reactive compact]'}]
    orig_create = harness.client.chat.completions.create
    try:
        # MAX_REACTIVE_RETRIES=1：第 1 次错误重试，第 2 次错误直接抛
        fake = FakeCompletions([
            RuntimeError('prompt_too_long: reduce input'),
            RuntimeError('prompt_too_long: reduce input'),
        ])
        harness.client.chat.completions.create = fake.create
        try:
            try:
                harness.agent_loop([{'role': 'user', 'content': 'x'}])
                raised = False
            except RuntimeError:
                raised = True
            assert raised, "重试耗尽后应抛出原始异常"
        finally:
            harness.client.chat.completions.create = orig_create
    finally:
        harness.reactive_compact = orig_reactive


def test_compact_tool_executes_prior_tools():
    """模型同时返回 [bash, compact]：bash 先执行，再压缩，结果以文本保留。"""
    executed = []
    orig_bash = patch_tool('bash', lambda command: (executed.append(command),
                                                    logger.info("[tool] bash(%r) executing", command),
                                                    f"out:{command}")[2])
    orig_compact = harness.compact_history
    harness.compact_history = lambda msgs: (logger.info("[compact-tool] compact_history called on %d msgs", len(msgs)),
                                            [{'role': 'user', 'content': '[Compacted] summary'}])[1]
    orig_create = harness.client.chat.completions.create
    try:
        fake = FakeCompletions([
            make_tool_response([
                ('c1', 'bash', '{"command": "echo a"}'),
                ('c2', 'compact', '{}'),
            ]),
            make_text_response('final answer'),
        ])
        harness.client.chat.completions.create = fake.create
        try:
            result = harness.agent_loop([{'role': 'user', 'content': 'do it'}])
        finally:
            harness.client.chat.completions.create = orig_create
        logger.info("[compact-tool] session done, result=%r", result)
        assert result == 'final answer'
        assert executed == ['echo a'], f"compact 之前的工具应先执行: {executed}"
        # 压缩后重开的上下文应包含摘要 + 之前工具的结果文本
        msgs2 = json.dumps(fake.calls[1], ensure_ascii=False)
        assert '[Compacted] summary' in msgs2
        assert 'out:echo a' in msgs2, "compact 前的工具结果应保留在新上下文里"
        # 新上下文里不应有引用旧 tool_call_id 的 role=tool 消息（避免 orphan）
        assert not any(m.get('role') == 'tool' for m in fake.calls[1]), "不应残留孤儿 tool 消息"
    finally:
        harness.tool_registry['bash'] = orig_bash
        harness.compact_history = orig_compact


def test_compact_tool_first_alone():
    """compact 是唯一/首个 tool_call 时正常压缩重开。"""
    orig_compact = harness.compact_history
    harness.compact_history = lambda msgs: [{'role': 'user', 'content': '[Compacted] summary'}]
    orig_create = harness.client.chat.completions.create
    try:
        fake = FakeCompletions([
            make_tool_response([('c1', 'compact', '{}')]),
            make_text_response('done after compact'),
        ])
        harness.client.chat.completions.create = fake.create
        try:
            result = harness.agent_loop([{'role': 'user', 'content': 'x'}])
        finally:
            harness.client.chat.completions.create = orig_create
        assert result == 'done after compact'
        assert '[Compacted] summary' in json.dumps(fake.calls[1], ensure_ascii=False)
    finally:
        harness.compact_history = orig_compact


def assert_valid_openai_msgs(msgs, label):
    """OpenAI 格式合法性：每条 role=tool 消息必须能对上之前 assistant 的 tool_calls。"""
    pending = {}
    for m in msgs:
        role = m.get('role')
        if role == 'assistant' and m.get('tool_calls'):
            for tc in m['tool_calls']:
                pending[tc['id']] = False
        elif role == 'tool':
            cid = m.get('tool_call_id')
            if cid not in pending:
                _log_format_error(label, msgs, f"orphan tool message {cid}")
            pending[cid] = True
        else:
            if pending and not all(pending.values()):
                _log_format_error(label, msgs, f"dangling tool_calls {pending}")
            pending = {}
    if pending and not all(pending.values()):
        _log_format_error(label, msgs, f"dangling tool_calls at end {pending}")
    logger.debug("[format-check] %s: valid (%d messages)", label, len(msgs))


def _log_format_error(label, msgs, reason):
    """格式校验失败：先输出完整快照便于排查，再抛 AssertionError。"""
    logger.error("[format-check] %s: %s", label, reason)
    logger.error("[format-check] %s snapshot:\n%s",
                 label, json.dumps(msgs, ensure_ascii=False, indent=2))
    raise AssertionError(f"{label}: {reason}")


def test_full_session_scenario():
    """端到端会话：多轮工具 → 自动压缩 → compact 工具 → reactive 压缩 → 最终回答。

    通过控制 estimate_size 的返回值精确触发一次自动压缩（iteration 2），
    其余轮次保持 50 低于 CONTEXT_LIMIT，让场景确定可复现。
    """
    executed = []

    def fake_bash(command):
        logger.info("[tool] bash(%r) executing", command)
        executed.append(f"bash:{command}")
        return f"out:{command}"

    def fake_read(path):
        logger.info("[tool] read(%r) executing", path)
        executed.append(f"read:{path}")
        return f"READ:{'L' * 100}:{path}"

    orig_bash = patch_tool('bash', fake_bash)
    orig_read = patch_tool('read', fake_read)
    orig_estimate = harness.estimate_size
    orig_compact = harness.compact_history
    orig_reactive = harness.reactive_compact
    orig_limit = harness.CONTEXT_LIMIT
    orig_create = harness.client.chat.completions.create

    sizes = iter([50, 10000, 50, 50, 50])          # iteration 2 超过阈值触发自动压缩
    compact_calls = []
    reactive_calls = []
    harness.CONTEXT_LIMIT = 100
    harness.estimate_size = lambda msgs: next(sizes)
    harness.compact_history = lambda msgs: (compact_calls.append(len(msgs))
                                            or logger.info("[scenario] compact_history called (auto/compact tool) on %d msgs", len(msgs))
                                            or [{'role': 'user', 'content': '[Compacted] summary'}])
    harness.reactive_compact = lambda msgs: (reactive_calls.append(len(msgs))
                                             or logger.info("[scenario] reactive_compact called on %d msgs", len(msgs))
                                             or [{'role': 'user', 'content': '[Reactive compact] summary'}])
    try:
        fake = FakeCompletions([
            make_tool_response([('c1', 'bash', '{"command": "echo 1"}')]),
            make_tool_response([('c2', 'bash', '{"command": "echo 2"}'), ('c3', 'compact', '{}')]),
            RuntimeError('prompt_too_long: reduce input'),
            make_tool_response([('c4', 'read', '{"path": "big.txt"}')]),
            make_text_response('all done'),
        ])
        harness.client.chat.completions.create = fake.create
        logger.info("[scenario] session starts (5 scripted responses)")
        result = harness.agent_loop([{'role': 'user', 'content': 'go'}])
        logger.info("[scenario] session ended, result=%r", result)

        assert result == 'all done'
        # 每轮发给 API 的快照都必须是合法 OpenAI 格式（无孤儿 tool 消息）
        for i, msgs in enumerate(fake.calls):
            assert_valid_openai_msgs(msgs, f"call {i}")
        # 工具按脚本顺序执行：bash → bash → read
        assert executed == ['bash:echo 1', 'bash:echo 2', 'read:big.txt'], executed
        # 自动压缩触发 1 次（iteration 2），compact 工具触发 1 次（iteration 2 响应）
        assert len(compact_calls) == 2, f"compact_history 次数: {len(compact_calls)}"
        # reactive 压缩恰好 1 次
        assert len(reactive_calls) == 1, f"reactive_compact 次数: {len(reactive_calls)}"
        # iteration 2 自动压缩后，call 2 的上下文已是摘要
        assert '[Compacted] summary' in json.dumps(fake.calls[1], ensure_ascii=False)
        # compact 工具轮（call 3）：之前的 bash 结果以文本保留，不残留 role=tool 消息
        msgs3 = fake.calls[2]
        assert 'bash:echo 2' not in json.dumps(msgs3)  # bash 是执行记录，不直接出现在上下文
        assert 'out:echo 2' in json.dumps(msgs3, ensure_ascii=False), "compact 前工具结果应保留"
        assert not any(m.get('role') == 'tool' for m in msgs3), "不应残留孤儿 tool 消息"
        # reactive 压缩后（call 4）上下文是新的摘要
        assert '[Reactive compact] summary' in json.dumps(fake.calls[3], ensure_ascii=False)
    finally:
        harness.tool_registry['bash'] = orig_bash
        harness.tool_registry['read'] = orig_read
        harness.estimate_size = orig_estimate
        harness.compact_history = orig_compact
        harness.reactive_compact = orig_reactive
        harness.CONTEXT_LIMIT = orig_limit
        harness.client.chat.completions.create = orig_create


# ---------- 运行 ----------

def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    failed = 0
    for t in tests:
        logger.info("=== running: %s ===", t.__name__)
        try:
            t()
            logger.info("=== PASS: %s ===", t.__name__)
            print(f"[PASS] {t.__name__}")
        except AssertionError as e:
            failed += 1
            logger.error("=== FAIL: %s: %s ===", t.__name__, e)
            print(f"[FAIL] {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            logger.error("=== ERROR: %s: %s: %s ===", t.__name__, type(e).__name__, e)
            print(f"[ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
