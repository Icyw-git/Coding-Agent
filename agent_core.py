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


def select_relevant_memories(messages: list, max_items: int = 5) -> list:
    files = list_memory_files()
    if not files:
        return []
    recent_texts = []
    for msg in reversed(messages):
        if msg.get('role') == 'user':  # 只考虑用户消息，因为只有用户消息才会包含记忆
            content = msg.get('content', '')
            if isinstance(content, list):  # 处理包含多个文本的消息
                content = " ".join(
                    str(getattr(b, "text", "")) for b in content
                    if getattr(b, "type", None) == "text"
                )
            if isinstance(content, str):
                recent_texts.append(content)
            if len(recent_texts) >= 3:  # 最多考虑最近3条消息
                break
    recent = ' '.join(reversed(recent_texts))[:2000]  # 最近3条消息，最多2000个字符
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

    keywords = [w.lower() for w in recent.split() if len(w) >= 3]  # fallback 关键词
    selected = []
    for f in files:
        text = (f['name'] + ' ' + f['description']).lower()
        if any(kw in text for kw in keywords):
            selected.append(f['filename'])
            if len(selected) >= max_items:
                break
    return selected


def load_memories(messages: list) -> str:  # 加载相关记忆文件内容
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
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )

    try:
        response = h.client.chat.completions.create(
            model=os.getenv('LLM_MODEL_ID'),
            messages=[{'role': 'user', 'content': prompt}], max_tokens=4000
        )
        msg = response.choices[0].message
        text = _extract_text(msg.content or '').strip()
        if not text:
            # 推理模型可能把 max_tokens 预算全花在 reasoning_content 上导致 content 为空，
            # 退而从 reasoning_content 里找 JSON 数组（与 extract_memories 一致）
            text = _extract_text(getattr(msg, 'reasoning_content', '') or '').strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            print(f"[Memory: consolidate failed - no JSON array in LLM reply: {text[:200]!r}]")
            return
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
    return max(1, int(ascii_chars / 3.5 + cjk * 1.3))


def _message_has_tool_use(msg):  # 判断是否工具调用
    return msg.get('role') == 'assistant' and bool(msg.get('tool_calls'))


def _is_tool_result_message(msg):  # 判断是否工具结果
    return msg.get('role') == 'tool'


def snip_compact(messages, max_messages=50):  # 保留头 3 条，尾部 47 条，中间按 tool_calls 分组
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
        return messages[:head_end]
    tail_start = len(messages) - keep_tail
    # 裁剪点不能落在工具结果上（其 assistant 已被裁掉=孤儿），推进到非工具消息
    while tail_start < len(messages) and _is_tool_result_message(messages[tail_start]):
        tail_start += 1
    if head_end >= tail_start:
        return messages
    result = messages[:head_end] + [{'role': 'user', 'content': f"[snipped {tail_start - head_end} messages]"}] + messages[tail_start:]
    # 若配对调整导致超出预算，从尾部再裁掉超出的条数，并避免以"孤儿"工具结果开头
    over = len(result) - max_messages
    while over > 0 and tail_start < len(messages):
        tail_start += 1
        over -= 1
        while tail_start < len(messages) and _is_tool_result_message(messages[tail_start]):
            tail_start += 1
            over -= 1
    return messages[:head_end] + [{'role': 'user', 'content': f"[snipped {tail_start - head_end} messages]"}] + messages[tail_start:]


def collect_tool_results(messages):  # 收集所有工具结果
    return [msg for msg in messages if msg.get('role') == 'tool']


def micro_compact(messages):  # 旧工具结果占位，只保留最近 KEEP_RECENT 条工具结果
    h = _h()
    tool_results = collect_tool_results(messages)
    if len(tool_results) < h.KEEP_RECENT:
        return messages
    for msg in tool_results[:-h.KEEP_RECENT]:
        if len(str(msg.get('content', ''))) > 120:
            msg['content'] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


def persist_large_output(tool_use_id, output):  # 持久化大输出
    h = _h()
    if len(output) <= h.PERSIST_THERSHOLD:
        return output
    h.TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = h.TOOL_RESULTS_DIR / f'{tool_use_id}.txt'
    if not path.exists():
        path.write_text(output, encoding='utf-8')
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"


def tool_result_budget(messages, max_bytes=None):  # 将工具结果压缩到 max_bytes 内存
    h = _h()
    if max_bytes is None:
        # 默认预算取 CONTEXT_LIMIT 的 80%，保证 budget 先于自动压缩(LLM)触发
        max_bytes = int(h.CONTEXT_LIMIT * 0.8)
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
        total = sum(len(str(x.get('content', ''))) for x in tool_msgs)
    return messages


def write_transcript(messages):  # 写入对话记录，返回路径
    h = _h()
    h.TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = h.TRANSCRIPT_DIR / f'transcript_{int(time.time())}.jsonl'
    with open(path, 'w', encoding='utf-8') as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + '\n')
    return path


def summarize_history(messages):  # 总结对话记录，返回总结内容
    if not messages:
        return "(empty summary)"
    conversation = json.dumps(messages, default=str)[-80000:]
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


def compact_history(messages):  # 使用 llm 进行对话记录压缩，返回压缩后的消息
    transcript_path = write_transcript(messages)
    print(f'[transcript saved:{transcript_path}]')
    summary = summarize_history(messages)
    # The request builder owns the system message. Keeping it out of canonical history
    # avoids duplicating system prompts after a compaction or a replay.
    return [{'role': 'user', 'content': f'[Compacted]\n\n{summary}'}]


def reactive_compact(messages):  # 保存完整对话记录，保留最近五条消息，返回压缩后的消息
    transcript = write_transcript(messages)
    tail_start = max(0, len(messages) - 5)
    # 尾部起点不能是孤儿工具结果（其 assistant 已被裁掉）
    while tail_start < len(messages) and _is_tool_result_message(messages[tail_start]):
        tail_start += 1
    summary = summarize_history(messages[:tail_start])
    result = [{'role': 'user', 'content': f'[Reactive compact]\n\n{summary}'}]
    result.extend(messages[tail_start:])  # 保留最近工作现场
    return result


def inject_memories(messages, memories_content, system=None):
    """记忆注入（返回副本，不污染 messages）：只往第一条 user 消息追加记忆；
    system 非空时前置 system 消息。"""
    request_messages = messages.copy()
    if memories_content:
        for i, m in enumerate(request_messages):
            if m.get('role') == 'user' and isinstance(m.get('content'), str):
                request_messages[i] = {**m, 'content': memories_content + '\n\n' + m['content']}
                break
    if system:
        request_messages = [{'role': 'system', 'content': system}] + request_messages
    return request_messages
