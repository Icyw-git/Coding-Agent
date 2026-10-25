"""记忆系统：索引读写、本地/LLM 召回、提取与合并。

可变状态（MEMORY_DIR / MEMORY_INDEX / CONSOLIDATE_THRESHOLD）由 harness 持有，
这里通过 ``llm_route.harness()`` 在运行时读取，测试 monkeypatch harness 即可生效。

写入约束：所有改文件的操作都走 ``_write_guard``（进程内 RLock + 跨进程 OS 锁），
并且先写临时文件再 ``os.replace``。合并（consolidate）额外采用「先落新文件、再删旧
文件」的顺序：中途失败只会多出文件，不会把旧记忆删空。
"""
import json
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager

import yaml

import llm_route
from file_lock import FileLockTimeout, exclusive_file_lock
from llm_route import harness as _h

_MEMORY_LOCK = threading.RLock()
_WRITE_DEPTH = 0   # 同一线程重入时不重复抢 OS 锁（否则 flock 会自锁）
_WRITE_LOCK_NAME = '.memory-write.lock'
_SLUG_RE = re.compile(r'[^a-z0-9._-]+')
_MAX_MEMORY_ITEMS = 50   # 提示词要求 <30；留出余量但仍给删除范围设上界

# 记忆文件解析缓存：key 是路径，value 是 ((size, mtime_ns), meta, body, raw)。
# 每轮 load_memories 都要扫全部记忆；21 个文件时 YAML 解析约 7 ms，直接缓存
# 解析结果能把固定开销降到读 stat。写入路径主动失效，外部修改靠 size/mtime 兜底。
_FRONTMATTER_CACHE: dict[str, tuple[tuple[int, int], dict, str, str]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 256


@contextmanager
def _write_guard():
    """串行化记忆写操作（进程内 + 跨进程）；可重入。"""
    global _WRITE_DEPTH
    with _MEMORY_LOCK:
        if _WRITE_DEPTH:
            _WRITE_DEPTH += 1
            try:
                yield
            finally:
                _WRITE_DEPTH -= 1
            return
        memory_dir = _h().MEMORY_DIR
        memory_dir.mkdir(parents=True, exist_ok=True)
        try:
            with exclusive_file_lock(memory_dir / _WRITE_LOCK_NAME, timeout=10.0):
                _WRITE_DEPTH = 1
                try:
                    yield
                finally:
                    _WRITE_DEPTH = 0
        except FileLockTimeout as exc:
            raise RuntimeError(f'memory store is busy: {exc}') from exc


def _slug(name: str) -> str:
    """把记忆名收敛成安全文件名；不允许穿越目录或逃出 MEMORY_DIR。"""
    slug = _SLUG_RE.sub('-', str(name or '').strip().lower()).strip('.-')
    slug = slug[:80]
    return slug or f'memory-{int(time.time())}'


def _cache_drop(path) -> None:
    with _CACHE_LOCK:
        _FRONTMATTER_CACHE.pop(str(path), None)


def _cache_clear() -> None:
    with _CACHE_LOCK:
        _FRONTMATTER_CACHE.clear()


def _cached_entry(path):
    """返回 (meta, body, raw)；stat 未变时直接复用上次解析结果。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    signature = (stat.st_size, stat.st_mtime_ns)
    key = str(path)
    with _CACHE_LOCK:
        hit = _FRONTMATTER_CACHE.get(key)
    if hit is not None and hit[0] == signature:
        return hit[1], hit[2], hit[3]
    try:
        raw = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    meta, body = _parse_frontmatter(raw)
    with _CACHE_LOCK:
        if len(_FRONTMATTER_CACHE) >= _CACHE_MAX:
            _FRONTMATTER_CACHE.clear()
        _FRONTMATTER_CACHE[key] = (signature, meta, body, raw)
    return meta, body, raw


def _memory_path(filename: str):
    """把模型给的文件名限制在 MEMORY_DIR 内；越界返回 None。"""
    h = _h()
    root = h.MEMORY_DIR.resolve()
    path = (h.MEMORY_DIR / str(filename)).resolve()
    return path if path.parent == root else None


def _atomic_write(path, text: str) -> None:
    """先写同目录临时文件再替换：崩溃时只会留下完整旧文件或完整新文件。"""
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    try:
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _render_memory(name: str, mem_type: str, description: str, body: str) -> str:
    """frontmatter 必须单行，否则 YAML 解析会把后续正文吞掉。"""
    def one_line(value) -> str:
        return ' '.join(str(value or '').split())

    return (f"---\nname: {one_line(name)}\ndescription: {one_line(description)}\n"
            f"type: {one_line(mem_type)}\n---\n\n{body}\n")


def write_memory_file(name: str, mem_type: str, description: str, body: str):  # 保存记忆文件
    with _write_guard():
        return _write_memory_file_unlocked(name, mem_type, description, body)


def _write_memory_file_unlocked(name: str, mem_type: str, description: str, body: str):
    h = _h()
    h.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    filepath = h.MEMORY_DIR / f'{_slug(name)}.md'
    _atomic_write(filepath, _render_memory(name, mem_type, description, body))
    _cache_drop(filepath)
    _rebuild_index_unlocked()  # 重建记忆索引
    return filepath


def _rebuild_index():
    with _write_guard():
        _rebuild_index_unlocked()


def _rebuild_index_unlocked():
    h = _h()
    lines = []
    for f in sorted(h.MEMORY_DIR.glob('*.md')):
        if f.name == 'MEMORY.md':
            continue
        entry = _cached_entry(f)
        if entry is None:
            continue
        meta, body, _raw = entry
        name = meta.get('name', f.name)
        desc = meta.get('description', (body.splitlines() or [''])[0][:80])
        lines.append(f'- [{name}({f.name}) - {desc}]')  # 索引结构：[名称](文件名) - 描述
    h.MEMORY_INDEX.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(h.MEMORY_INDEX, '\n'.join(lines) + '\n' if lines else '')


def read_memory_index() -> str:  # 读取记忆索引
    h = _h()
    if not h.MEMORY_INDEX.exists():
        return ''
    text = h.MEMORY_INDEX.read_text(encoding='utf-8', errors='replace').strip()
    return text if text else ''


def read_memory_file(filename: str):  # 读取记忆文件
    path = _memory_path(filename)
    if path is None:
        return None
    entry = _cached_entry(path)
    return entry[2] if entry is not None else None


def list_memory_files() -> list:  # 列出所有记忆文件
    h = _h()
    results = []
    for f in sorted(h.MEMORY_DIR.glob('*.md')):
        if f.name == 'MEMORY.md':
            continue
        entry = _cached_entry(f)
        if entry is None:
            continue
        meta, body, _raw = entry
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
                # 只取文本块：图片引用/占位不是用户提问，不参与记忆召回
                texts = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get('type') == 'text':
                            texts.append(str(part.get('text') or ''))
                    elif getattr(part, 'type', None) == 'text':
                        texts.append(str(getattr(part, 'text', '') or ''))
                text = ' '.join(t for t in texts if t)
                if text.startswith('[image attachment'):
                    continue
                content = text
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
        response = llm_route.completion(
            'memory',
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.7,
            max_tokens=8000,   # 思考模式 reasoning 先于正文，小预算=空 content
        )
        text = (response.choices[0].message.content or '').strip()
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
        response = llm_route.completion(
            'memory',
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.7,
            max_tokens=8000,   # 思考模式 reasoning 先于正文（实测简单任务也 4000+）
        )
        msg = response.choices[0].message
        text = (msg.content or '').strip()
        if not text:
            # 推理模型可能把 max_tokens 预算全花在 reasoning_content 上导致 content 为空，
            # 退而从 reasoning_content 里找 JSON 数组
            text = (getattr(msg, 'reasoning_content', '') or '').strip()
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
            if not isinstance(mem, dict):
                continue
            name = str(mem.get('name') or f'memory_{int(time.time())}')
            mem_type = str(mem.get('type') or 'user')
            if mem_type not in _h().MEMORY_TYPES:
                mem_type = 'user'   # schema 外的类型不落盘，避免污染后续合并
            desc = ' '.join(str(mem.get('description') or '').split())
            body = str(mem.get('body') or '').strip()
            if desc and body:
                write_memory_file(name, mem_type, desc, body)  # 写入新记忆文件
                count += 1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
        else:
            print(f"[Memory: extract returned {len(items)} items but none had desc+body]")
    except Exception as e:
        print(f"[Memory: extract error: {e}]")


def _validate_consolidated(items) -> list[dict]:
    """校验合并输出；任意一条不合格就整批放弃。

    合并会删旧文件，所以不能像提取那样「跳过坏项继续写」：只写回一部分
    也等于丢数据。这里只做纯数据校验，不碰磁盘。
    """
    h = _h()
    if not isinstance(items, list) or not items:
        raise ValueError('result is not a non-empty list')
    if len(items) > _MAX_MEMORY_ITEMS:
        raise ValueError(f'{len(items)} memories exceeds limit {_MAX_MEMORY_ITEMS}')
    prepared = []
    seen = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f'item {index} is not an object')
        name = str(item.get('name') or '').strip()
        mem_type = str(item.get('type') or '').strip()
        if not mem_type:
            mem_type = 'user'   # 缺省按提取路径的约定；显式错误类型仍整批拒绝
        description = ' '.join(str(item.get('description') or '').split())
        body = str(item.get('body') or '').strip()
        if not name or not description or not body:
            raise ValueError(f'item {index} is missing name/description/body')
        if mem_type not in h.MEMORY_TYPES:
            raise ValueError(f'item {index} has invalid type {mem_type!r}')
        slug = _slug(name)
        if slug in seen:
            raise ValueError(f'duplicate memory name {name!r}')
        seen.add(slug)
        prepared.append({'slug': slug, 'name': name, 'type': mem_type,
                         'description': description, 'body': body})
    return prepared


def _replace_memory_files(prepared: list[dict]) -> None:
    """用校验过的集合替换旧记忆：先落新文件，全部成功后才删旧文件。

    崩溃/写入失败发生在删除之前时，磁盘上只会多出文件（重复），不会缺文件；
    遗留的重复项下次合并仍会收敛。索引在最后重建，最后一步也是原子替换。
    """
    h = _h()
    with _write_guard():
        memory_dir = h.MEMORY_DIR
        memory_dir.mkdir(parents=True, exist_ok=True)
        staging = memory_dir / f'.staging-{uuid.uuid4().hex}'
        staging.mkdir()
        try:
            staged = {}
            for item in prepared:
                path = staging / f"{item['slug']}.md"
                _atomic_write(path, _render_memory(
                    item['name'], item['type'], item['description'], item['body']))
                meta, _body = _parse_frontmatter(path.read_text(encoding='utf-8'))
                if (str(meta.get('name')) != item['name']
                        or str(meta.get('type')) != item['type']):
                    raise ValueError(f"staged file failed verification: {path.name}")
                staged[path.name] = path
            for filename, path in staged.items():
                path.replace(memory_dir / filename)
            for old in memory_dir.glob('*.md'):
                if old.name == 'MEMORY.md' or old.name in staged:
                    continue
                old.unlink(missing_ok=True)
            _cache_clear()   # 文件集合已变：旧缓存可能对应已删除/旧版文件
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        _rebuild_index_unlocked()


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
        response = llm_route.completion(
            'memory',
            messages=[{'role': 'user', 'content': prompt + reminder}],
            max_tokens=8000, temperature=0.2,   # 结构化输出：低温提高 JSON 合规率；思考模式需大预算防 JSON 截断
        )
        msg = response.choices[0].message
        text = (msg.content or '').strip()
        if not text:
            # 推理模型可能把 max_tokens 预算全花在 reasoning_content 上导致 content 为空，
            # 退而从 reasoning_content 里找 JSON 数组（与 extract_memories 一致）
            text = (getattr(msg, 'reasoning_content', '') or '').strip()
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
        prepared = _validate_consolidated(items)
        _replace_memory_files(prepared)
        print(f"\n\033[33m[Memory: consolidated {len(files)} → {len(prepared)} memories]\033[0m")
    except ValueError as exc:
        # 整批校验失败：此时还没动过任何旧文件
        print(f"[Memory: consolidate aborted - {exc}; existing memories kept]")
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


def inject_memories(messages, memories_content, system=None, environment=None):
    """记忆注入（返回副本，不污染 messages）：记忆块作为独立的 system 消息
    追加在请求末尾。主前缀（system + 全部历史）保持逐字稳定，供应商前缀
    缓存（KV 复用）可跨轮命中——若改写第一条 user 消息（旧行为），每轮
    召回结果变化都会作废整个前缀，多轮对话越长越亏。

    environment（当前本地时间等）放在记忆块之前：两者都在稳定前缀之后，
    但记忆块保持在最后，读请求尾部的人/测试仍先看到召回结果。"""
    request_messages = messages.copy()
    if system:
        request_messages = [{'role': 'system', 'content': system}] + request_messages
    if environment:
        request_messages.append({'role': 'system', 'content': environment})
    if memories_content:
        request_messages.append({'role': 'system', 'content': memories_content})
    return request_messages
