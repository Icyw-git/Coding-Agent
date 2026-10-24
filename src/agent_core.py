"""核心可复用模块：Hook 注册表 / 记忆系统 / 上下文压缩。

harness.agent_loop 与 agent_teams.spawn 共用同一份实现，避免双份漂移。

设计约定（为了可测试性）：
- 本模块是「无状态逻辑层」，不持有可变全局状态；
- 可变状态（MEMORY_DIR / CONTEXT_LIMIT / client / build_system 等）由 harness 持有，
  本模块通过 `_h()` 在运行时读取 —— 这样测试可以 `harness.MEMORY_DIR = tmp` /
  patch `harness.client` 直接生效，无需改测试。
"""
import base64
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


def _role_target(role: str):
    """按角色解析模型；配置缺失时返回 None，退回旧 LLM_MODEL_ID 行为。"""
    import llm_config
    try:
        return llm_config.get_settings().resolve_for(role)
    except llm_config.ConfigError:
        return None


def _role_model(role: str) -> str | None:
    target = _role_target(role)
    return target.model if target is not None else None


def _role_completion(role: str = 'main', **kwargs):
    """副通道调用：memory/summary 等角色可独立配置供应商与模型。

    ``harness.client_for`` 在同 provider 时返回模块级 client，测试对
    ``harness.client`` 的 patch 继续生效。
    """
    h = _h()
    target = _role_target(role)
    if target is not None:
        kwargs['model'] = target.model
        api = h.client_for(role, target) if hasattr(h, 'client_for') else h.client
    else:
        kwargs.setdefault('model', os.getenv('LLM_MODEL_ID'))
        api = h.client
    return api.chat.completions.create(**kwargs)


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
        response = _role_completion(
            'memory',
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.7,
            max_tokens=8000,   # 思考模式 reasoning 先于正文，小预算=空 content
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
        response = _role_completion(
            'memory',
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.7,
            max_tokens=8000,   # 思考模式 reasoning 先于正文（实测简单任务也 4000+）
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
        response = _role_completion(
            'memory',
            messages=[{'role': 'user', 'content': prompt + reminder}],
            max_tokens=8000, temperature=0.2,   # 结构化输出：低温提高 JSON 合规率；思考模式需大预算防 JSON 截断
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

# ============================================================
# 多模态附件
# ============================================================
# 工具结果里只放图片引用（harness_image），真正的 data URI 在每次请求前
# 由 expand_image_parts 生成。这样历史、转录和会话事件都不会被 base64 撑大，
# 图片也仍能在会话恢复后重新读取。
# 上下文预算用的固定兜底，不是账单 token。常见值：GPT-4o 1024² ≈ 765、
# Claude 1024² ≈ 1398、Gemini 1024² ≈ 1032、DeepSeek 每图上限 1024。图片实际
# 成本随尺寸/模型/provider 变化，这里只要求「不把窗口算爆」；已知尺寸时
# estimate_image_tokens 会覆盖它。
IMAGE_TOKEN_ESTIMATE = 1000
# DeepSeek 官方只公布「缩放后每图最多 1024 token」，没有公开瓦片公式；
# 预算按上限计，宁可高估。
DEEPSEEK_IMAGE_TOKEN_LIMIT = 1024


def _env_image_token_estimate() -> int:
    """未知尺寸/旧引用用的固定兜底值；环境变量可调。"""
    raw = os.getenv('HARNESS_IMAGE_TOKEN_ESTIMATE')
    if not raw:
        return IMAGE_TOKEN_ESTIMATE
    try:
        return max(1, int(raw))
    except ValueError:
        return IMAGE_TOKEN_ESTIMATE


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """从文件头读宽高（不依赖 Pillow）；识别不了返回 None。"""
    import struct
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
        return struct.unpack('>II', data[16:24])
    if data.startswith((b'GIF87a', b'GIF89a')) and len(data) >= 10:
        return struct.unpack('<HH', data[6:10])
    if data.startswith(b'BM') and len(data) >= 26:
        dib = int.from_bytes(data[14:18], 'little')
        if dib == 12:
            width, height = struct.unpack('<HH', data[18:22])
            return abs(width), abs(height)
        width, height = struct.unpack('<ii', data[18:26])
        return abs(width), abs(height)
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        chunk = data[12:16]
        if chunk == b'VP8X' and len(data) >= 30:
            return (1 + int.from_bytes(data[24:27], 'little'),
                    1 + int.from_bytes(data[27:30], 'little'))
        if chunk == b'VP8L' and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], 'little')
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if (chunk == b'VP8 ' and len(data) >= 30
                and data[23:26] == b'\x9d\x01\x2a'):
            return (int.from_bytes(data[26:28], 'little') & 0x3FFF,
                    int.from_bytes(data[28:30], 'little') & 0x3FFF)
    if data.startswith(b'\xff\xd8'):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index:index + 2], 'big')
            if length < 2 or index + length > len(data):
                break
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(data[index + 3:index + 5], 'big')
                width = int.from_bytes(data[index + 5:index + 7], 'big')
                return width, height
            index += length
    return None


def _openai_image_tokens(width: int, height: int) -> int:
    """GPT-4o 高细节规则：先塞进 2048²，短边缩到 768，再按 512 瓦片计。"""
    import math
    scale = min(1.0, 2048 / max(width, height))
    width, height = max(1, int(width * scale)), max(1, int(height * scale))
    if min(width, height) > 768:
        scale = 768 / min(width, height)
        width, height = max(1, int(width * scale)), max(1, int(height * scale))
    tiles = math.ceil(width / 512) * math.ceil(height / 512)
    return 85 + 170 * tiles


def _claude_image_tokens(width: int, height: int) -> int:
    # 旧版 Claude 文档的经验值：长边超过 1568 会缩放，约 (w*h)/750 且上限 ~1600
    scale = min(1.0, 1568 / max(width, height))
    width, height = max(1, int(width * scale)), max(1, int(height * scale))
    return min(1600, max(1, (width * height + 749) // 750))


def _gemini_image_tokens(width: int, height: int) -> int:
    import math
    return (max(1, math.ceil(width / 768)) * max(1, math.ceil(height / 768))
            * 258)


def _deepseek_image_tokens(width: int, height: int) -> int:
    # 官方只给「缩放后每图最多 1024」，没有公开公式；按上限保守计。
    return DEEPSEEK_IMAGE_TOKEN_LIMIT


def estimate_image_tokens(data: bytes, model: str | None = None) -> int:
    """按当前模型族估算图片 token；尺寸未知时退回固定值。

    这是上下文保护，不是计费：宁可略高，避免压缩阈值被低估。设置
    HARNESS_IMAGE_TOKEN_ESTIMATE 可覆盖固定兜底值。
    """
    fallback = _env_image_token_estimate()
    dimensions = image_dimensions(data)
    if dimensions is None:
        return fallback
    width, height = dimensions
    if width <= 0 or height <= 0:
        return fallback
    model_name = (model or _role_model('main') or os.getenv('LLM_MODEL_ID') or '').lower()
    if 'claude' in model_name or 'anthropic' in model_name:
        estimate = _claude_image_tokens(width, height)
    elif 'gemini' in model_name:
        estimate = _gemini_image_tokens(width, height)
    elif 'deepseek' in model_name:
        estimate = _deepseek_image_tokens(width, height)
    else:
        estimate = _openai_image_tokens(width, height)
    return max(fallback, estimate)


def image_attachment_message(images: list[dict]) -> dict:
    """把 view_image 的附件包成一条紧随工具结果的 user 消息。

    Chat Completions 的 tool 消息对图片支持不一致（部分模型只认 user 里的
    image_url），所以图片走独立的 user 消息，tool 结果只保留文字摘要。
    """
    names = ', '.join(str(image.get('name') or 'image') for image in images)
    content = [{
        'type': 'text',
        'text': f'[image attachment] {names}. '
                'Inspect the attached image before continuing.',
    }]
    content.extend(dict(image) for image in images)
    return {'role': 'user', 'content': content}


def expand_image_parts(messages: list[dict]) -> list[dict]:
    """把 harness_image 引用展开成 OpenAI 的 image_url data URI。

    只复制含图片的消息，普通消息保持原对象；文件被清理时降级成一条文本，
    不让一次读图失败拖垮整个请求。只读 view_image 自己的缓存目录：会话事件
    可以被用户编辑，引用指向 `.env` 这类文件时不能借多模态通道把它送出去。
    """
    try:
        base = getattr(_h(), 'TOOL_RESULTS_DIR', None)
        cache_root = (Path(base) / 'images').resolve() if base else None
    except Exception:
        cache_root = None
    result = []
    for message in messages:
        content = message.get('content')
        if not isinstance(content, list) or not any(
                isinstance(part, dict) and part.get('type') == 'harness_image'
                for part in content):
            result.append(message)
            continue
        parts = []
        for part in content:
            if not isinstance(part, dict) or part.get('type') != 'harness_image':
                parts.append(part)
                continue
            path = Path(str(part.get('path') or ''))
            name = str(part.get('name') or path.name or 'image')
            try:
                resolved = path.resolve()
                if cache_root is not None and not resolved.is_relative_to(cache_root):
                    raise OSError('outside image cache')
                data = resolved.read_bytes()
            except (OSError, ValueError):
                parts.append({'type': 'text',
                              'text': f'[image unavailable: {name}]'})
                continue
            mime = str(part.get('mime') or 'image/png')
            encoded = base64.b64encode(data).decode('ascii')
            parts.append({'type': 'image_url',
                          'image_url': {'url': f'data:{mime};base64,{encoded}'}})
        updated = dict(message)
        updated['content'] = parts
        result.append(updated)
    return result


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


def _image_token_cost(msgs) -> tuple[int, int]:
    """遍历消息里的图片 part，返回 (token 合计, 已计数的 part 数)。"""
    fallback = _env_image_token_estimate()
    total = 0
    seen = 0
    for message in msgs:
        content = (message.get('content') if isinstance(message, dict)
                   else getattr(message, 'content', None))
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get('type')
            if kind == 'harness_image':
                try:
                    total += int(part.get('tokens') or fallback)
                except (TypeError, ValueError):
                    total += fallback
                seen += 1
            elif kind == 'image_url':
                total += fallback
                seen += 1
    return total, seen


def estimate_size(msgs):
    # token 启发式：英文约 3.5 字符/token，中文约 1.3 字符/token（与 CONTEXT_LIMIT 同单位）
    raw = str(msgs)
    # 历史里正常只有引用；万一是旧消息里的 data URI，也不能按字符数把上下文算爆。
    text = re.sub(r'data:[^;\s]+;base64,[A-Za-z0-9+/=]+', '[image]', raw)
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    cjk = len(text) - ascii_chars
    image_tokens, seen = _image_token_cost(msgs)
    # 旧历史里若有字符串形态的图片引用，遍历不到：按 part 出现次数补齐。
    raw_images = raw.count("'harness_image'") + raw.count("'image_url'")
    image_tokens += max(0, raw_images - seen) * _env_image_token_estimate()
    return max(1, int(ascii_chars / 3.5 + cjk / 1.3) + image_tokens)


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
            + [{'role': 'user', 'content': f"[snipped {tail_start - head_end} messages]"}]
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
    response = _role_completion(
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
