"""记忆条目的字段定义、归一与渲染（schema v2）。

为什么要有这一层
----------------
记忆字段会同时被五处消费：抽取写入、合并校验、索引重建、召回打分、`/memories`
展示。字段规则散在各处就会漂移——本模块收口成一处。

**顺带修掉一个真实缺陷**：`remember` 工具（M1）写入的是语义类型
`preference`/`correction`/`fact`/`context`，而合并校验用的是旧词表
`user`/`feedback`/`project`/`reference`。两边不一致的后果是：用户一旦用过
`remember`，后续每次合并都会整批校验失败并被静默跳过（`[memory] skipped`）。
这里用 `normalize_type()` 做双向归一，旧词表继续能读，新写入一律规范化。

向 schema v2 新增的字段
----------------------
- ``keywords``：检索用的稀疏索引。现在召回只打 `name + description`，正文里
  的关键词（比如条件偏好里的「困了」）召回不到，补这个字段。
- ``last_update_date``：显式时间戳。原来靠文件 mtime，但召回记录写在独立 json 里
  以避免扰动 mtime，一旦要"哪条最新"的确定性排序就不可靠。
- ``confidence``：0..1。来源决定初值（用户明确要求 > 抽取推测），合并时取大。
- ``scope``：``global`` / ``session``。PAHF 的两层记忆：session 内的笔记在合并
  时判定 durability，durable 才提升为 global。
- ``supersedes`` / ``superseded_by``：冲突链。``correction`` 类走覆盖而不是并存
  ——一条错误假设和它的修正在一起，召回可能取到错的那个。

向后兼容
--------
旧文件缺少任何新字段都取默认值，读取不报错。旧类型词表经 ``LEGACY_ALIASES``
映射到规范词表。
"""
from __future__ import annotations

import re
from datetime import date, datetime

#: 规范化类型（写入一律用这四个）。
TYPES = ('preference', 'correction', 'fact', 'context')

#: 旧词表 → 规范词表。旧文件继续可读，不在磁盘上做迁移。
LEGACY_ALIASES = {
    'user': 'preference',
    'feedback': 'correction',
    'project': 'fact',
    'reference': 'context',
}

#: 作用域。session 内的笔记不跨会话，合并时判 durability 才提升。
SCOPES = ('global', 'session')
DEFAULT_SCOPE = 'global'

#: 默认置信度。抽取推测按 0.5，用户明确要求按 0.9（见 memory_write）。
DEFAULT_CONFIDENCE = 0.5

#: 关键词条数上限（防止把正文整段塞进来）。
MAX_KEYWORDS = 12
#: 单个关键词长度上限。
MAX_KEYWORD_CHARS = 24

_KEYWORD_SPLIT = re.compile(r'[,，;；、\s]+')
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _yaml_scalar(value: str) -> str:
    """把字符串输出成 YAML 安全标量。

    为什么必须做：条件性偏好的写法就是 ``when sleepy: prefers tea``——里面带
    ``: ``。直接写进 ``description:`` 会让 YAML 报 "mapping values are not
    allowed here"，**整个 frontmatter 变成空映射**，于是 type/scope/keywords
    全部丢失（实测踩到：type 回落成 fact、last_update_date 变空）。

    只在必要时加双引号，保持普通值可读、diff 干净。
    """
    text = str(value or '')
    needs_quote = (
        ': ' in text or text.endswith(':') or ' #' in text
        or text.startswith(('#', '&', '*', '!', '|', '>', '%', '@', '`', '"', "'"))
        or text != text.strip()
        or text.casefold() in ('true', 'false', 'yes', 'no', 'null', '~')
        or text == ''
    )
    if not needs_quote:
        return text
    escaped = text.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def normalize_type(value) -> str:
    """把任意历史类型名映射到规范词表；**无法识别时回落** ``fact``。

    读取路径用这个：一个字段写坏不该让整批记忆不可读。
    写入/合并校验必须用 ``canonical_type(strict=True)``——那里静默归一等于把
    一个格式错误的 LLM 输出当成有效数据，而合并会删旧文件。
    """
    text = str(value or '').strip().casefold()
    if text in TYPES:
        return text
    return LEGACY_ALIASES.get(text, 'fact')


def canonical_type(value, *, strict: bool = False) -> str:
    """规范类型。``strict=True`` 时未知类型抛 ``ValueError``。"""
    text = str(value or '').strip().casefold()
    if text in TYPES:
        return text
    if text in LEGACY_ALIASES:
        return LEGACY_ALIASES[text]
    if strict:
        raise ValueError(f'invalid memory type {value!r}')
    return 'fact'


def normalize_keywords(value) -> list[str]:
    """关键词归一：接受列表或分隔字符串，去重保序、限长限量。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw = _KEYWORD_SPLIT.split(value)
    elif isinstance(value, (list, tuple, set)):
        raw = []
        for item in value:
            raw.extend(_KEYWORD_SPLIT.split(str(item)))
    else:
        raw = _KEYWORD_SPLIT.split(str(value))
    out: list[str] = []
    seen = set()
    for token in raw:
        word = str(token).strip().strip('"\'')
        if not word:
            continue
        word = word[:MAX_KEYWORD_CHARS]
        key = word.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(word)
        if len(out) >= MAX_KEYWORDS:
            break
    return out


def normalize_confidence(value) -> float:
    """置信度收敛到 0..1；非数字回落默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE
    if number != number:          # NaN
        return DEFAULT_CONFIDENCE
    return max(0.0, min(1.0, number))


def normalize_date(value) -> str:
    """ISO 日期；无法解析返回空串（调用方决定回落 mtime 还是今天）。"""
    text = str(value or '').strip()
    if not text:
        return ''
    if _DATE_RE.match(text):
        return text
    # 容忍 yaml 把 2026-10-28 解析成 date 对象，或带时间的 ISO 串
    if isinstance(value, (date, datetime)):
        return value.strftime('%Y-%m-%d')
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).strftime('%Y-%m-%d')
    except ValueError:
        return ''


def today() -> str:
    return date.today().strftime('%Y-%m-%d')


def normalize_scope(value) -> str:
    text = str(value or '').strip().casefold()
    return text if text in SCOPES else DEFAULT_SCOPE


def derive_keywords(name: str, description: str, body: str = '') -> list[str]:
    """没有显式 keywords 时，从名字/描述里兜底抽几个词。

    只做最朴素的切分（英文词 + 中文二元组），够本地关键词召回用。
    正文不参与：正文长，会把稀有词权重稀释。
    """
    text = f'{name} {description}'
    words = re.findall(r'[A-Za-z][A-Za-z0-9_\-]{2,}', text)
    cjk = re.sub(r'[^\u4e00-\u9fff]', ' ', text)
    bigrams = []
    for run in cjk.split():
        if len(run) >= 2:
            bigrams.extend(run[i:i + 2] for i in range(len(run) - 1))
    return normalize_keywords(words + bigrams)


def normalize_entry(meta: dict, body: str, filename: str) -> dict:
    """把 frontmatter + 正文归一成条目 dict（读取侧唯一入口）。

    所有字段都有默认值：旧文件缺字段照常可用。
    """
    meta = meta if isinstance(meta, dict) else {}
    name = str(meta.get('name') or '').strip() or filename.rsplit('.', 1)[0]
    description = ' '.join(str(meta.get('description') or '').split())
    mem_type = normalize_type(meta.get('type'))
    keywords = normalize_keywords(meta.get('keywords'))
    if not keywords:
        keywords = derive_keywords(name, description, body)
    return {
        'filename': filename,
        'name': name,
        'description': description,
        'type': mem_type,
        'scope': normalize_scope(meta.get('scope')),
        'keywords': keywords,
        'confidence': normalize_confidence(meta.get('confidence')),
        'last_update_date': normalize_date(meta.get('last_update_date')),
        'supersedes': str(meta.get('supersedes') or '').strip(),
        'superseded_by': str(meta.get('superseded_by') or '').strip(),
        'source_session': str(meta.get('source_session') or '').strip(),
        'source_quote': ' '.join(str(meta.get('source_quote') or '').split()),
        'body': body,
    }


def render_frontmatter(entry: dict) -> str:
    """把条目渲染成 frontmatter 文本（不含正文）。

    顺序固定，便于 diff 与人工核对。
    """
    def one_line(value) -> str:
        return ' '.join(str(value or '').split())

    lines = ['---', f"name: {_yaml_scalar(one_line(entry.get('name')))}",
             f"description: {_yaml_scalar(one_line(entry.get('description')))}",
             f"type: {normalize_type(entry.get('type'))}",
             f"scope: {normalize_scope(entry.get('scope'))}"]
    keywords = normalize_keywords(entry.get('keywords'))
    if keywords:
        lines.append('keywords: ' + ','.join(keywords))
    lines.append(f"confidence: {normalize_confidence(entry.get('confidence')):.2f}")
    stamp = normalize_date(entry.get('last_update_date')) or today()
    lines.append(f'last_update_date: {stamp}')
    for field in ('supersedes', 'superseded_by', 'source_session',
                  'source_quote'):
        value = one_line(entry.get(field))
        if value:
            lines.append(f'{field}: {_yaml_scalar(value[:200])}')
    lines.append('---')
    return '\n'.join(lines) + '\n'


def similarity(left: dict, right: dict) -> float:
    """两条记忆的相似度 0..1，用于 update-vs-add 判定。

    组成：关键词 Jaccard（主）+ 名字精确相同（强信号）+ 描述二元组重合（辅）。
    不做向量：本地零往返是硬约束，见 docs/PERSONAL_AGENT_LEARNING_RSI_DESIGN.md。
    """
    left_keys = {k.casefold() for k in normalize_keywords(left.get('keywords'))}
    right_keys = {k.casefold() for k in normalize_keywords(right.get('keywords'))}
    if left_keys or right_keys:
        union = left_keys | right_keys
        jaccard = len(left_keys & right_keys) / len(union) if union else 0.0
    else:
        jaccard = 0.0

    left_name = str(left.get('name') or '').strip().casefold()
    right_name = str(right.get('name') or '').strip().casefold()
    same_name = 1.0 if left_name and left_name == right_name else 0.0

    left_desc = _bigrams(str(left.get('description') or ''))
    right_desc = _bigrams(str(right.get('description') or ''))
    if left_desc or right_desc:
        union = left_desc | right_desc
        desc = len(left_desc & right_desc) / len(union) if union else 0.0
    else:
        desc = 0.0

    return min(1.0, 0.6 * jaccard + 0.3 * same_name + 0.1 * desc)


def _bigrams(text: str) -> set[str]:
    lowered = text.casefold()
    latin = set(re.findall(r'[a-z][a-z0-9_\-]{2,}', lowered))
    cjk = set()
    for run in re.sub(r'[^\u4e00-\u9fff]', ' ', lowered).split():
        if len(run) >= 2:
            cjk.update(run[i:i + 2] for i in range(len(run) - 1))
    return latin | cjk
