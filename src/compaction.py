"""上下文压缩：消息裁剪、工具结果预算、LLM 摘要与 transcript 快照。

压缩流水线（micro → snip → budget → LLM 摘要）与 agent_loop 共用；可变配置
（CONTEXT_LIMIT / KEEP_RECENT / 持久化目录）由 harness 持有，运行时经
``llm_route.harness()`` 读取，测试 patch harness 即可生效。
"""
import json
import time

import llm_route
from llm_route import harness as _h
from tokens import estimate_size


def content_text(content) -> str:
    """消息 content 的可读文本（字符串原样返回，多模态只取文本和图片占位）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get('type') == 'text':
                texts.append(str(part.get('text') or ''))
            elif part.get('type') == 'harness_image':
                texts.append(f"[image: {part.get('name') or 'image'}]")
            elif part.get('type') == 'image_url':
                texts.append('[image]')
        return ' '.join(text for text in texts if text)
    return str(content or '')


def _message_has_tool_use(msg):  # 判断是否工具调用
    return msg.get('role') == 'assistant' and bool(msg.get('tool_calls'))


def _is_tool_result_message(msg):  # 判断是否工具结果
    return msg.get('role') == 'tool'


# 截断标记的文本必须**跨轮逐字稳定**：它落在前缀中间，一旦文本变化（旧版写
# `[snipped N messages]`，N 是每次截掉的消息数），marker 之后整段尾部全部失配。
# 实测：45 次 snip 后的调用加权命中率只有 45%（其余 95%），吃掉 58% 的未命中
# token。代价是模型看不到「截掉了多少条」——判定「有没有丢上下文」靠的是标记
# 存在与否，不是那个数字，而这个数字每轮都在变，本来也读不出来。
SNIP_MARKER = '[earlier messages were trimmed]'


def snip_compact(messages, max_messages=None, info=None):
    """保留头 3 条 + marker + 尾部，中间按 tool_calls 分组裁剪。

    消息数安全阀随窗口缩放（默认 max(50, CONTEXT_LIMIT//4000)，1M 上下文 ≈ 208 条）：
    它只做「消息数失控」的兜底，日常大小压缩由 micro_compact 按水位触发——
    触发越频繁，[snipped N] marker 每轮变文本 + 窗口滑动，前缀缓存断得越狠。
    info 出参：触发时填 head_end / tail_start / snipped，调用方持久化为
    history_snipped 索引事件（rebuild 用同一切片表达式逐字还原）。"""
    h = _h()
    if max_messages is None:
        max_messages = max(50, h.CONTEXT_LIMIT // 4000)
    if len(messages) <= max_messages:
        return messages
    # 头部/尾部边界统一归一化：不能切断 assistant 带多个 tool_calls 的分组。
    from session_store import normalize_snip_bounds
    head_end, _ = normalize_snip_bounds(messages, 3, len(messages))
    keep_tail = max_messages - head_end - 1
    if keep_tail <= 1:
        return messages   # 窗口小到放不下头部+marker：放弃截断（安全阀不能丢掉全部现场）
    tail_start = len(messages) - keep_tail
    head_end, tail_start = normalize_snip_bounds(
        messages, head_end, tail_start)
    if head_end >= tail_start:
        return messages
    if info is not None:
        info.update(head_end=head_end, tail_start=tail_start,
                    snipped=tail_start - head_end)
    return (messages[:head_end]
            + [{'role': 'user', 'content': SNIP_MARKER}]
            + messages[tail_start:])


def collect_tool_results(messages):  # 收集所有工具结果
    return [msg for msg in messages if msg.get('role') == 'tool']


def micro_compact(messages, size=None, session_store=None, info=None):
    """旧工具结果占位：只保留最近 KEEP_RECENT 条，**决策持久化为事件**。

    前缀缓存保护（两条，缺一不可）：
    1. 上下文不过半（estimate < 0.5×CONTEXT_LIMIT）不改写 —— 否则命中率 84%→6% 断崖；
    2. 改写决策写入 tool_compacted 事件（rebuild 时确定性应用）—— 若只在内存改写，
       每轮 rebuild 会还原原文再重新改写，滑动边界每轮断一次前缀（实测命中 ~50%）。
       迟滞：新越界的工具结果攒够 4 条才成批改写，批间前缀逐字稳定。

    占位符可恢复：带工具身份（名称+参数）与完整输出落盘路径。
    info 出参：本函数原地改写（返回原对象），调用方靠 info['rewrote'] 感知改写
    发生并重算 size。"""
    h = _h()
    if (size if size is not None else h.estimate_size(messages)) < h.CONTEXT_LIMIT * 0.5:
        return messages
    if session_store is None:
        return messages   # 无 store 无法持久化决策：跳过（auto 压缩仍兜底）
    tool_results = collect_tool_results(messages)
    eligible = len(tool_results) - h.KEEP_RECENT
    if eligible <= 0:
        return messages
    compacted = session_store.compacted_placeholders()
    prefix_ids = [m.get('tool_call_id') for m in tool_results[:eligible]]
    new_ids = [cid for cid in prefix_ids if cid and cid not in compacted]
    if len(new_ids) < 4:
        return messages   # 新越界不足一批：本轮不改写，保住前缀缓存
    content_by_id = {m.get('tool_call_id'): str(m.get('content', ''))
                     for m in tool_results}
    identity = {}
    for m in messages:
        if m.get('role') != 'assistant':
            continue
        for call in m.get('tool_calls') or []:
            fn = call.get('function') or {}
            identity[call.get('id')] = (f"{fn.get('name', '?')}"
                                        f"({str(fn.get('arguments', ''))[:80]})")
    for call_id in new_ids:
        path = _persist_to_file(call_id, content_by_id.get(call_id, ''))
        saved = f' Full output saved: {path}.' if path else ''
        ident = identity.get(call_id, call_id)
        placeholder = (f"[Earlier tool result compacted: {ident}."
                       f"{saved} Re-run the tool if needed.]")
        session_store.append_tool_compacted(call_id, placeholder)
        compacted[call_id] = placeholder
    # 当前轮立即应用（历史里的占位由 rebuild 提供，这里补新决策的）
    for m in messages:
        if m.get('role') == 'tool' and m.get('tool_call_id') in compacted:
            m['content'] = compacted[m['tool_call_id']]
    if info is not None:
        info['rewrote'] = True
    return messages


def _persist_to_file(tool_use_id, output) -> str | None:
    """把输出写入 TOOL_RESULTS_DIR，返回文件路径；写失败返回 None。"""
    h = _h()
    try:
        h.TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = h.TOOL_RESULTS_DIR / f'{tool_use_id}.txt'
        if not path.exists():
            path.write_text(output, encoding='utf-8')
        return str(path)
    except OSError:
        return None


def persist_large_output(tool_use_id, output):  # 持久化大输出
    h = _h()
    if len(output) <= h.PERSIST_THERSHOLD:
        return output
    path = _persist_to_file(tool_use_id, output)
    if path is None:
        return output
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"


def _bound_tool_output(tool_call_id, output):
    """入口 middle-out 截断（对齐 Codex truncate.rs）：保头 + 保尾 + 截断标注。

    编译错误、测试失败摘要、`tail -20` 等关键信息集中在尾部，旧 persist 预览
    只留头 2000 字符恰好把最有用的部分丢掉。截断发生在 append 之前——事件流里
    存的就是最终形态，历史中从此不存在超大工具结果，前缀天然稳定；全量落盘
    可恢复（强于 Codex 的纯截断）。"""
    h = _h()
    text = output if isinstance(output, str) else str(output)
    if len(text) <= h.TOOL_RESULT_CHAR_LIMIT:
        return output
    path = _persist_to_file(tool_call_id, text)
    saved = f'\nFull output: {path}' if path else '\n(persist unavailable)'
    head, tail = text[:8000], text[-4000:]
    return (f"{head}\n… [{len(text) - 12000} chars truncated]{saved}\n…\n{tail}")


def _content_chars(content) -> int:
    """工具结果大小：多模态消息只算文本部分，图片按固定成本另计。"""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(str(part.get('text') or ''))
                   for part in content if isinstance(part, dict))
    return len(str(content or ''))


def tool_result_budget(messages, max_bytes=None, session_store=None, info=None):
    """工具结果总量预算：超大单条输出（>PERSIST_THERSHOLD 字符）落盘换预览。

    预算与 estimate_size 同源换算（CONTEXT_LIMIT 是 token，工具结果以英文为主
    ≈3 字符/token）。入口截断（_bound_tool_output）上线后历史单条 ≤
    TOOL_RESULT_CHAR_LIMIT，本层降级为多中条聚合超限的净兜底，极少触发。
    改写决策持久化为 tool_compacted 事件（与 micro 同机制）：只改内存的话重启
    rebuild 会还原原文，前缀缓存跨重启断裂。原地改写，info['rewrote'] 出参。"""
    h = _h()
    if max_bytes is None:
        max_bytes = int(h.CONTEXT_LIMIT * 0.8 * 3)
    tool_msgs = [m for m in messages if m.get('role') == 'tool']
    total = sum(_content_chars(m.get('content', '')) for m in tool_msgs)
    if total <= max_bytes:
        return messages
    ranked = sorted(tool_msgs, key=lambda m: _content_chars(m.get('content', '')),
                    reverse=True)
    for m in ranked:
        if total <= max_bytes:
            break
        content = m.get('content', '')
        if not isinstance(content, str):
            continue   # 图片/多模态内容不套用文本截断
        if len(content) <= h.PERSIST_THERSHOLD:
            continue
        tid = m.get('tool_call_id', 'unknown')
        m['content'] = persist_large_output(tid, content)  # 持久化大输出，返回引用路径和预览内容
        if session_store is not None:
            session_store.append_tool_compacted(tid, m['content'])
        total = sum(_content_chars(x.get('content', '')) for x in tool_msgs)
    if info is not None:
        info['rewrote'] = True
    return messages


def write_transcript(messages):  # 写入对话记录，返回路径
    h = _h()
    h.TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    # time_ns：同一秒内两次落盘曾共用同一路径互相覆盖（transcript 是压缩丢弃
    # 内容的唯一全量恢复源，丢一份就永久丢）
    path = h.TRANSCRIPT_DIR / f'transcript_{time.time_ns()}.jsonl'
    with open(path, 'w', encoding='utf-8') as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + '\n')
    return path


def _summary_input(messages, budget=80_000):
    """给 summarize_history 构造输入：纯文本行，永不切在 JSON 中间。

    旧实现 json.dumps(...)[:-80000] 丢掉前 41%（原始目标、用户约束都在最前），
    尾部还不是合法 JSON。这里始终保留第一条 user 消息（原始目标），尾部按
    字符预算逐条取（消息不劈半）。"""
    lines = []
    first_user = next((m for m in messages
                       if m.get('role') == 'user'
                       and content_text(m.get('content')).strip()), None)
    if first_user is not None:
        lines.append(f"[原始目标] {content_text(first_user['content'])[:2000]}")
    tail, total = [], 0
    for m in reversed(messages):
        content = content_text(m.get('content'))
        if m.get('tool_calls'):
            calls = '; '.join(
                f"{c.get('function', {}).get('name', '?')}"
                f"({str(c.get('function', {}).get('arguments', ''))[:120]})"
                for c in m['tool_calls'])
            content = f"{content or ''}[tool_calls: {calls}]" if content else f"[tool_calls: {calls}]"
        line = f"{m.get('role', '?')}: {content[:4000]}"
        if tail and total + len(line) > budget:
            break
        tail.append(line)
        total += len(line)
    lines.extend(reversed(tail))
    return '\n'.join(lines)


def summarize_history(messages):  # 总结对话记录，返回总结内容
    if not messages:
        return "(empty summary)"
    conversation = _summary_input(messages)
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete, and MUST complete "
              "all five sections - if tight on space, drop details rather than truncating.\n\n" + conversation)
    response = llm_route.completion(
        'summary',
        messages=[{'role': 'user', 'content': prompt}],
        temperature=0.7,
        max_tokens=8000,   # 思考模式 reasoning 先于正文
    )
    return (response.choices[0].message.content or "(empty summary)").strip()


def _tail_within_budget(messages, keep, budget_tokens):
    """按「条数上限 + token 预算」双约束取尾部；起点不落在孤儿工具结果上。

    旧实现只按条数（messages[-6:]），6 条各 100k 字符的工具结果会原样保留、
    压缩后立刻又超限。"""
    h = _h()
    tail_start = max(0, len(messages) - keep)
    total = 0
    budget = min(budget_tokens if budget_tokens is not None else 50_000,
                 h.CONTEXT_LIMIT // 8)
    # 从尾部向前累计，预算耗尽则收缩 tail_start（条数与预算取更紧的那个）
    idx = len(messages)
    while idx > tail_start:
        size = estimate_size([messages[idx - 1]])
        if total + size > budget and idx < len(messages):
            break
        total += size
        idx -= 1
    tail_start = max(tail_start, idx)
    # 起点不能是孤儿工具结果（其 assistant 已被裁掉）
    while tail_start < len(messages) and _is_tool_result_message(messages[tail_start]):
        tail_start += 1
    return tail_start


def compact_history(messages, keep=6, budget_tokens=None):  # 使用 llm 进行对话记录压缩：摘要 + 保留最近几轮原文
    # 对齐 Codex 手感：压缩后保留最近工作现场（对话不断层），更早内容进摘要。
    # tail 会写入 compaction 事件（见 _record_compaction），回放时完整还原。
    transcript_path = write_transcript(messages)
    print(f'[transcript saved:{transcript_path}]')
    summary = summarize_history(messages)
    tail_start = _tail_within_budget(messages, keep, budget_tokens)
    result = [{'role': 'user', 'content': f'[Compacted]\n\n{summary}'}]
    result.extend(messages[tail_start:])
    return result


def reactive_compact(messages, keep=5, budget_tokens=None):
    """保存完整对话记录，摘要 + 按预算保留最近现场（reactive 重试会逐次缩 keep/预算）。"""
    transcript_path = write_transcript(messages)
    print(f'[transcript saved:{transcript_path}]')
    tail_start = _tail_within_budget(messages, keep, budget_tokens)
    summary = summarize_history(messages[:tail_start])
    result = [{'role': 'user', 'content': f'[Reactive compact]\n\n{summary}'}]
    result.extend(messages[tail_start:])  # 保留最近工作现场
    return result
