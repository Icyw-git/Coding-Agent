"""核心可复用模块：Hook 注册表 / 记忆系统 / 上下文压缩。

harness.agent_loop 与 agent_teams.spawn 共用同一份实现，避免双份漂移。

设计约定（为了可测试性）：
- 本模块是「无状态逻辑层」，不持有可变全局状态；
- 可变状态（MEMORY_DIR / CONTEXT_LIMIT / client / build_system 等）由 harness 持有，
  本模块通过 `_h()` 在运行时读取 —— 这样测试可以 `harness.MEMORY_DIR = tmp` /
  patch `harness.client` 直接生效，无需改测试。
"""
import json
import os
import re
import time
from pathlib import Path

import yaml


def _h():
    """延迟取 harness 模块：避免模块级循环导入，且始终读到 harness 的当前全局状态。"""
    import harness
    return harness


# ============================================================
# Hook 注册表
# ============================================================

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result  # 阻断工具调用
    return None


def _extract_text(text: str) -> str:
    return text.strip()


# ============================================================
# 记忆系统
# ============================================================

def write_memory_file(name: str, mem_type: str, description: str, body: str):  # 保存记忆文件
    h = _h()
    h.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    slug = name.lower().replace(' ', '-').replace('/', '-')
    filename = f'{slug}.md'
    filepath = h.MEMORY_DIR / filename
    filepath.write_text(
        f'---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n',
        encoding='utf-8',
    )
    _rebuild_index()  # 重建记忆索引
    return filepath


def _rebuild_index():
    h = _h()
    lines = []
    for f in sorted(h.MEMORY_DIR.glob('*.md')):
        if f.name == 'MEMORY.md':
            continue
        raw = f.read_text(encoding='utf-8', errors='replace')
        meta, body = _parse_frontmatter(raw)
        name = meta.get('name', f.name)
        desc = meta.get('description', raw.split('\n')[0][:80])
        lines.append(f'- [{name}({f.name}) - {desc}]')  # 索引结构：[名称](文件名) - 描述
    h.MEMORY_INDEX.write_text('\n'.join(lines) + '\n' if lines else '', encoding='utf-8')


def read_memory_index() -> str:  # 读取记忆索引
    h = _h()
    if not h.MEMORY_INDEX.exists():
        return ''
    text = h.MEMORY_INDEX.read_text(encoding='utf-8', errors='replace').strip()
    return text if text else ''


def read_memory_file(filename: str):  # 读取记忆文件
    h = _h()
    path = h.MEMORY_DIR / filename
    if not path.exists():
        return None
    return path.read_text(encoding='utf-8', errors='replace')


def list_memory_files() -> list:  # 列出所有记忆文件
    h = _h()
    results = []
    for f in sorted(h.MEMORY_DIR.glob('*.md')):
        if f.name == 'MEMORY.md':
            continue
        raw = f.read_text(encoding='utf-8', errors='replace')
        meta, body = _parse_frontmatter(raw)
        results.append({
            'filename': f.name,
            'name': meta.get('name', f.stem),
            'description': meta.get('description', ''),
            'type': meta.get('type', 'user'),
            'body': body,
        })
    return results


def _recent_user_text(messages: list, max_chars: int = 2000) -> str:
    """最近 ≤3 条用户消息拼接（记忆召回的依据：偏好/约束都出现在用户输入里）。"""
    recent_texts = []
    for msg in reversed(messages):
        if msg.get('role') == 'user':
            content = msg.get('content', '')
            if isinstance(content, list):
                content = " ".join(
                    str(b.get('text', '') if isinstance(b, dict) else getattr(b, 'text', ''))
                    for b in content
                    if (b.get('type') if isinstance(b, dict) else getattr(b, 'type', None)) == 'text'
                )
            if isinstance(content, str):
                recent_texts.append(content)
            if len(recent_texts) >= 3:
                break
    # Keep the latest question when older messages exceed the recall budget.
    return ' '.join(reversed(recent_texts))[-max_chars:] if max_chars > 0 else ''


def _local_tokens(text: str) -> list[str]:
    """中文按 CJK 二元组、英文/数字按长度≥3 的词切分（小写）。

    中文没有空格分词，二元组是无需词典的召回手段；纯单字命中噪声太大，
    由打分阈值过滤。
    """
    tokens = [raw.lower() for raw in re.findall(r'[A-Za-z0-9_]+', text)
              if len(raw) >= 3]
    for run in re.findall(r'[\u4e00-\u9fff]+', text):
        for i in range(len(run) - 1):
            tokens.append(run[i:i + 2])
    return tokens


def local_relevant_memories(messages: list, files: list | None = None,
                            max_items: int = 5) -> list:
    """本地记忆筛选：零网络往返，覆盖中英文。

    打分 = 2×ASCII 词命中 + 1×中文二元组命中（去重），阈值 ≥2：
    单个英文关键词即可命中（信息量大），中文需要至少两个二元组
    （例如「简短」+「回答」），避免常见字偶然撞车。
    """
    if files is None:
        files = list_memory_files()
    if not files:
        return []
    recent = _recent_user_text(messages)
    if not recent.strip():
        return []
    tokens = set(_local_tokens(recent))
    scored = []
    for f in files:
        haystack = (f['name'] + ' ' + f['description']).lower()
        score = sum(2 if len(token) > 2 and token.isascii() else 1
                    for token in tokens if token in haystack)
        if score >= 2:
            scored.append((score, f['filename']))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [filename for _, filename in scored[:max_items]]


def select_relevant_memories(messages: list, max_items: int = 5) -> list:
    files = list_memory_files()
    if not files:
        return []
    recent = _recent_user_text(messages)
    if not recent.strip():
        return []

    catalog_lines = []
    for i, f in enumerate(files):
        catalog_lines.append(f"{i}:{f['name']} - {f['description']}")
    catalog = '\n'.join(catalog_lines)

    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )

    try:
        response = _h().client.chat.completions.create(
            model=os.getenv('LLM_MODEL_ID'),
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.7,
            max_tokens=200,
        )
        text = _extract_text(response.choices[0].message.content).strip()
        match = re.search(r'\[.*?\]', text, re.DOTALL)  # 解析JSON数组，如[0, 3]
        if match:
            indices = json.loads(match.group())
            selected = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(files):
                    selected.append(files[idx]['filename'])
                    if len(selected) >= max_items:  # 最多选择max_items个记忆文件
                        break
            return selected
    except Exception:
        pass

    # LLM 不可用/返回异常时的 fallback：中文感知的本地筛选
    # （不再用按空格拆词——中文没有空格，旧 fallback 对中文完全失效）
    return local_relevant_memories(messages, files, max_items=max_items)


def load_memories(messages: list) -> str:  # 加载相关记忆文件内容
    # 召回策略 HARNESS_MEMORY_RECALL：
    #   local 默认——本地中文关键词筛选，零网络往返（省 ~2s/轮，见 perf 分段 mem.load）；
    #          召回质量与 llm 模式不同（关键词匹配 vs 语义筛选）
    #   llm    旧行为——每条消息一次 LLM 筛选往返，需要语义召回时显式开启
    #   off    关闭召回
    # HARNESS_LOAD_MEMORIES=0 仍生效（等价 off，向后兼容）。
    if os.getenv('HARNESS_LOAD_MEMORIES', '1') == '0':
        return ''
    mode = os.getenv('HARNESS_MEMORY_RECALL', 'local').strip().lower()
    if mode == 'off':
        return ''
    if mode == 'local':
        selected_files = local_relevant_memories(messages)
    else:
        selected_files = select_relevant_memories(messages)
    if not selected_files:
        return ''
    parts = ["<relevant_memories>"]
    for f in selected_files:
        content = read_memory_file(f)
        if content:
            parts.append(content)
    parts.append('</relevant_memories>')
    return '\n\n'.join(parts)


def extract_memories(messages: list):  # 提取用户偏好、约束、项目事实等记忆
    # 记忆窗口：会话开头前 10 条（用户偏好通常最先说）+ 结尾最后 10 条
    if len(messages) <= 20:
        window = messages
    else:
        window = messages[:10] + messages[-10:]

    dialogue_parts = []
    for msg in window:
        role = msg.get('role', '?')
        content = msg.get('content', '')
        if isinstance(content, list):
            content = " ".join(
                str(getattr(b, "text", "")) for b in content
                if getattr(b, "type", None) == "text"
            )
        if isinstance(content, str) and content.strip():
            dialogue_parts.append(f"{role}: {content}")
    dialogue = "\n".join(dialogue_parts)

    if not dialogue.strip():
        print("[Memory: extract skipped - no text content in recent messages]")
        return

    existing = list_memory_files()
    existing_desc = '\n'.join(f"- {m['name']}: {m['description']}" for m in existing) if existing else "(none)"

    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user' (user preference), 'feedback' (guidance), "
        "'project' (project fact), 'reference' (external pointer)\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n"
        "OUTPUT FORMAT (STRICT): reply with ONLY a plain JSON array. "
        "Do NOT think out loud, do NOT explain, do NOT wrap it in markdown code "
        "fences (```json), do NOT add any text before or after the array.\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )

    try:
        response = _h().client.chat.completions.create(
            model=os.getenv('LLM_MODEL_ID'),
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.7,
            max_tokens=4000,
        )
        msg = response.choices[0].message
        text = _extract_text(msg.content or '').strip()
        if not text:
            # 推理模型可能把 max_tokens 预算全花在 reasoning_content 上导致 content 为空，
            # 退而从 reasoning_content 里找 JSON 数组
            text = _extract_text(getattr(msg, 'reasoning_content', '') or '').strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        # 先试贪婪匹配，失败再逐个尝试最短数组段，取第一个能解析成 list 的
        items = None
        if match:
            try:
                items = json.loads(match.group())
            except Exception:
                items = None
        if not isinstance(items, list):
            for m in re.finditer(r'\[.*?\]', text, re.DOTALL):
                try:
                    cand = json.loads(m.group())
                    if isinstance(cand, list):
                        items = cand
                        break
                except Exception:
                    continue
        if not isinstance(items, list):
            print(f"[Memory: extract failed - no valid JSON array in LLM reply: {text[:200]!r}]")
            return
        if not items:
            print("[Memory: extract returned empty (LLM judged nothing worth remembering)]")
            return
        count = 0
        for mem in items:
            name = mem.get('name', f'memory_{int(time.time())}')
            mem_type = mem.get('type', 'user')
            desc = mem.get('description', '')
            body = mem.get('body', '')
            if desc and body:
                write_memory_file(name, mem_type, desc, body)  # 写入新记忆文件
                count += 1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
        else:
            print(f"[Memory: extract returned {len(items)} items but none had desc+body]")
    except Exception as e:
        print(f"[Memory: extract error: {e}]")


def consolidate_memories():
    """Merge duplicate/stale memories. Triggered when file count ≥ threshold."""
    h = _h()
    files = list_memory_files()
    if len(files) < h.CONSOLIDATE_THRESHOLD:
        return

    catalog = "\n\n".join(
        f"## {f['filename']}\nname: {f['name']}\ndescription: {f['description']}\n{f['body']}"
        for f in files
    )

    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        "3. Keep the total under 30 memories\n"
        "4. Preserve important user preferences above all\n"
        "Output ONLY the JSON array - no explanations, no markdown fences, no commentary. "
        "The first character of your reply must be '[' and the last must be ']'. "
        "Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )

    def _call(reminder=''):
        response = h.client.chat.completions.create(
            model=os.getenv('LLM_MODEL_ID'),
            messages=[{'role': 'user', 'content': prompt + reminder}],
            max_tokens=4000, temperature=0.2,   # 结构化输出：低温提高 JSON 合规率
        )
        msg = response.choices[0].message
        text = _extract_text(msg.content or '').strip()
        if not text:
            # 推理模型可能把 max_tokens 预算全花在 reasoning_content 上导致 content 为空，
            # 退而从 reasoning_content 里找 JSON 数组（与 extract_memories 一致）
            text = _extract_text(getattr(msg, 'reasoning_content', '') or '').strip()
        return text

    match = None
    last_text = ''
    for attempt in range(2):   # 首次失败（跑题/截断）：点名要求纯 JSON 重试一次
        text = _call('\n\nREMINDER: Reply with ONLY the JSON array, nothing else.'
                     if attempt else '')
        last_text = text
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            break
        print(f"[Memory: consolidate reply had no JSON array (attempt {attempt + 1}/2), retrying]")
    if not match:
        print(f"[Memory: consolidate failed - no JSON array in LLM reply: {last_text[:200]!r}]")
        return
    try:
        items = json.loads(match.group())
        if not items:
            # 必须先于删除逻辑返回：否则会把所有记忆文件删光再写 0 条
            print("[Memory: consolidate returned empty - aborting (would delete all memories)]")
            return

        # Remove old memory files (keep MEMORY.md)
        for f in h.MEMORY_DIR.glob("*.md"):
            if f.name != "MEMORY.md":
                f.unlink()

        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)  # 边写边更新索引

        print(f"\n\033[33m[Memory: consolidated {len(files)} → {len(items)} memories]\033[0m")
    except Exception as e:
        print(f"[Memory: consolidate error: {e}]")


def _parse_frontmatter(text: str):
    if not text.startswith('---'):
        return {}, text
    parts = text.split('---', 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}  # frontmatter 非映射（如缺空格被当纯标量）时按空处理
    return meta, parts[2].strip()


# ============================================================
# 上下文压缩（OpenAI 格式：assistant 的 tool_calls 是顶层字段，
# 工具结果是独立的 role='tool' 消息：tool_call_id + content 字符串）
# ============================================================

def estimate_size(msgs):
    # token 启发式：英文约 3.5 字符/token，中文约 1.3 字符/token（与 CONTEXT_LIMIT 同单位）
    text = str(msgs)
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    cjk = len(text) - ascii_chars
    return max(1, int(ascii_chars / 3.5 + cjk / 1.3))


def _message_has_tool_use(msg):  # 判断是否工具调用
    return msg.get('role') == 'assistant' and bool(msg.get('tool_calls'))


def _is_tool_result_message(msg):  # 判断是否工具结果
    return msg.get('role') == 'tool'


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
    # 头部：保留前 3 条；不能切断"assistant 带多个工具"的分组（否则 dangling）
    head_end = 3
    if head_end > 0 and head_end < len(messages):
        # 情况 A：头部以 assistant-with-tool_calls 结尾 → 并入其工具结果
        if _message_has_tool_use(messages[head_end - 1]):
            while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
                head_end += 1
        # 情况 B：头部切断在分组中间（前后都是工具结果）→ 并入剩余工具结果
        while head_end < len(messages) and _is_tool_result_message(messages[head_end - 1]) and _is_tool_result_message(messages[head_end]):
            head_end += 1
    # 尾部预算：head + 1 条 marker + tail <= max_messages
    keep_tail = max_messages - head_end - 1
    if keep_tail <= 1:
        return messages   # 窗口小到放不下头部+marker：放弃截断（安全阀不能丢掉全部现场）
    tail_start = len(messages) - keep_tail
    # 裁剪点不能落在工具结果上（其 assistant 已被裁掉=孤儿），推进到非工具消息
    while tail_start < len(messages) and _is_tool_result_message(messages[tail_start]):
        tail_start += 1
    if head_end >= tail_start:
        return messages
    if info is not None:
        info.update(head_end=head_end, tail_start=tail_start,
                    snipped=tail_start - head_end)
    return messages[:head_end] + [{'role': 'user', 'content': f"[snipped {tail_start - head_end} messages]"}] + messages[tail_start:]


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
    total = sum(len(str(m.get('content', ''))) for m in tool_msgs)
    if total <= max_bytes:
        return messages
    ranked = sorted(tool_msgs, key=lambda m: len(str(m.get('content', ''))), reverse=True)
    for m in ranked:
        if total <= max_bytes:
            break
        content = str(m.get('content', ''))
        if len(content) <= h.PERSIST_THERSHOLD:
            continue
        tid = m.get('tool_call_id', 'unknown')
        m['content'] = persist_large_output(tid, content)  # 持久化大输出，返回引用路径和预览内容
        if session_store is not None:
            session_store.append_tool_compacted(tid, m['content'])
        total = sum(len(str(x.get('content', ''))) for x in tool_msgs)
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
                       if m.get('role') == 'user' and str(m.get('content', '')).strip()), None)
    if first_user is not None:
        lines.append(f"[原始目标] {str(first_user['content'])[:2000]}")
    tail, total = [], 0
    for m in reversed(messages):
        content = str(m.get('content') or '')
        if m.get('tool_calls'):
            calls = '; '.join(f"{c.get('function', {}).get('name', '?')}({str(c.get('function', {}).get('arguments', ''))[:120]})"
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
    response = _h().client.chat.completions.create(
        model=os.getenv("LLM_MODEL_ID"),
        messages=[{'role': 'user', 'content': prompt}],
        temperature=0.7,
        max_tokens=4000,
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
    transcript = write_transcript(messages)
    tail_start = _tail_within_budget(messages, keep, budget_tokens)
    summary = summarize_history(messages[:tail_start])
    result = [{'role': 'user', 'content': f'[Reactive compact]\n\n{summary}'}]
    result.extend(messages[tail_start:])  # 保留最近工作现场
    return result


def inject_memories(messages, memories_content, system=None):
    """记忆注入（返回副本，不污染 messages）：记忆块作为独立的 system 消息
    追加在请求末尾。主前缀（system + 全部历史）保持逐字稳定，供应商前缀
    缓存（KV 复用）可跨轮命中——若改写第一条 user 消息（旧行为），每轮
    召回结果变化都会作废整个前缀，多轮对话越长越亏。"""
    request_messages = messages.copy()
    if system:
        request_messages = [{'role': 'system', 'content': system}] + request_messages
    if memories_content:
        request_messages.append({'role': 'system', 'content': memories_content})
    return request_messages
