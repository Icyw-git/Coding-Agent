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
from datetime import date as _date
from datetime import timedelta as _timedelta
from pathlib import Path

import yaml

import llm_route
import memory_aging
import memory_durability
import memory_feedback
import memory_redact
import memory_schema
import memory_scope
import memory_write
from file_lock import FileLockTimeout, exclusive_file_lock
from llm_route import harness as _h

_MEMORY_LOCK = threading.RLock()
_WRITE_DEPTH = 0   # 同一线程重入时不重复抢 OS 锁（否则 flock 会自锁）
_WRITE_LOCK_NAME = '.memory-write.lock'
_SLUG_RE = re.compile(r'[^a-z0-9._-]+')
_MAX_MEMORY_ITEMS = 50   # 提示词要求 <30；留出余量但仍给删除范围设上界

#: 召回块的优先级声明。必须在第一行：回忆块追加在用户消息之后（保前缀缓存），
#: 而模型对末尾内容权重高——不声明就会被旧偏好盖过当前指令。
#: PAHF 的 injection eval 专门测这一项（over-influence）。
MEMORY_PRECEDENCE = (
    'Priority: the user\'s latest message always overrides these notes when '
    'they conflict. Treat them as background preferences to apply only when the '
    'current request is silent or ambiguous.'
)

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


def _render_entry(entry: dict) -> str:
    """写盘前的唯一出入口：先脱敏，再渲染。

    所有写入（新增 / 合并 / 覆盖）都必须走这里——记忆是长期状态，一条 .env 内容
    被记下来之后每次会话都会被召回，泄露面比会话本身大得多（会话能删，记忆会
    一直跟着）。
    """
    def one_line(value) -> str:
        return ' '.join(str(value or '').split())

    safe_desc, desc_hits = memory_redact.redact(str(entry.get('description') or ''))
    safe_body, body_hits = memory_redact.redact(str(entry.get('body') or ''))
    safe_quote, quote_hits = memory_redact.redact(str(entry.get('source_quote') or ''))
    hits = sorted(set(desc_hits) | set(body_hits) | set(quote_hits))
    if hits:
        _record_redaction(hits)

    name = one_line(entry.get('name'))
    # 关键词也必须脱敏：它们是**从原始描述派生**的，不处理就会把密钥写进
    # frontmatter（实测泄过一次：`keywords: api,key,sk-abcdef...`）。
    raw_keywords = (memory_schema.normalize_keywords(entry.get('keywords'))
                    or memory_schema.derive_keywords(safe_desc or name))
    safe_keywords = [_clean_keyword(word) for word in raw_keywords]
    prepared = {
        **entry,
        'name': name,
        'description': safe_desc,
        'body': safe_body,
        'source_quote': safe_quote[:200],
        'last_update_date': (entry.get('last_update_date')
                             or memory_schema.today()),
        'keywords': [word for word in safe_keywords if word],
    }
    return memory_schema.render_document(prepared)


def _clean_keyword(word: str) -> str:
    """单个关键词过一遍脱敏；命中则整词丢弃。

    丢弃而不是替换成 ``[redacted:x]``：关键词是检索用的碎片，带一段替换标记
    既召不回也污染索引。
    """
    cleaned, hits = memory_redact.redact(word)
    return '' if hits else cleaned


def _render_memory(name: str, mem_type: str, description: str, body: str,
                   *, source: str = '', quote: str = '', scope: str = None,
                   keywords=None, confidence=None, supersedes: str = '') -> str:
    """按字段渲染一条记忆（``_render_entry`` 的字段式入口）。

    合并路径拿到的是一个条目 dict，走 ``_render_entry``；这里只是把字段拼成
    dict 再委托过去，保证**两者共用同一套脱敏与序列化**。
    """
    return _render_entry({
        'name': name,
        'description': description,
        'type': mem_type,
        'body': body,
        'scope': scope or memory_schema.DEFAULT_SCOPE,
        'keywords': keywords,
        'confidence': (memory_schema.DEFAULT_CONFIDENCE
                       if confidence is None else confidence),
        'supersedes': supersedes,
        'source_session': source,
        'source_quote': quote,
    })


def _audit(event: str, payload: dict) -> None:
    """把一条记忆相关事件写进会话流；没有活动会话就静默跳过。

    惰性导入 hooks：memory 是更底层的模块，顶层 import 会形成环。
    审计是观察层，任何失败都不能反过来影响一次写入。
    """
    try:
        import hooks
        hooks._append_audit(event, payload)
    except Exception:
        pass


def _record_redaction(kinds: list[str]) -> None:
    """脱敏是静默篡改用户要记的东西：必须留痕，否则“我明明让它记住了”无解。"""
    print(f'  \033[33m[memory] redacted {len(kinds)} secret(s): {", ".join(kinds)}\033[0m')
    _audit('memory_redacted', {
        'kinds': ','.join(kinds),
        'reason': 'credential-shaped content in memory body',
    })


def write_memory_file(name: str, mem_type: str, description: str, body: str,
                      *, source: str = '', quote: str = '', scope: str = None,
                      confidence: float | None = None,
                      supersedes: str = '', keywords=None) -> tuple[Path, str]:
    """写一条记忆，返回 ``(路径, 动作)``。动作是 add / update / supersede。

    写入前决定怎么落盘（``memory_write.decide``）：同一个偏好的多个措辞版本在
    召回时可能互相矛盾，合并时又要靠模型重新判定，确定性去重比事后补救便宜。

    ``supersedes`` 是**显式**的"我修正了哪一条"（记忆名或文件名）。它优先于
    相似度推测：一条修正和它纠正的错误内容，字面相似度往往不高（措辞本来就会变），
    而抽取提示词里已经有现有记忆清单，让模型直接指认比猜准得多。

    调用方持有写锁。
    """
    with _write_guard():
        result = _write_memory_file_unlocked(
            name, mem_type, description, body, source=source, quote=quote,
            scope=scope, confidence=confidence, supersedes=supersedes,
            keywords=keywords)
    _audit('memory_written', {'name': str(name), 'type': str(mem_type),
                              'chars': len(str(body or ''))})
    return result


def _write_memory_file_unlocked(name: str, mem_type: str, description: str,
                               body: str, *, source: str = '',
                               quote: str = '', scope: str = None,
                               confidence: float | None = None,
                               supersedes: str = '', keywords=None):
    h = _h()
    h.MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    draft = {
        'name': name,
        'description': description,
        'type': memory_schema.canonical_type(mem_type),
        'body': body,
        # 留空交给 memory_durability 判：在这里填上默认值会让它误以为
        # “调用方已显式指定”而跳过 durability 判定（实测踩到过）。
        'scope': scope or '',
        'confidence': (memory_schema.DEFAULT_CONFIDENCE
                       if confidence is None else confidence),
        'last_update_date': memory_schema.today(),
        'source_session': source,
        'source_quote': quote,
    }
    draft['keywords'] = (memory_schema.normalize_keywords(keywords)
                         or memory_schema.derive_keywords(description or name))
    # 两层记忆：没显式指定 scope 时按 durability 判。一次性上下文（「这次不想托运」）
    # 留在 session 层，不冒充长期偏好。显式指定不覆盖——人比规则准。
    draft = memory_durability.apply(draft)

    existing = list_memory_files()
    action, target = memory_write.decide(draft, existing, supersedes=supersedes)
    target_name = f"{_slug(name)}.md"
    if action == memory_write.SUPERSEDE and target['filename'] == target_name:
        # 新旧同名 = 同一个文件：不是替换而是写入。若真按 supersede 处理，
        # 会把刚写好的新内容归档掉，那一条记忆就凭空消失了（实测踩到）。
        action = memory_write.UPDATE
    if action == memory_write.UPDATE:
        entry = memory_write.merge(draft, target)
        filepath = h.MEMORY_DIR / target['filename']
        _atomic_write(filepath, _render_entry(entry))
    else:
        entry = dict(draft)
        if action == memory_write.SUPERSEDE:
            entry['supersedes'] = target['filename']
        filepath = h.MEMORY_DIR / target_name
        _atomic_write(filepath, _render_entry(entry))
        if action == memory_write.SUPERSEDE:
            _retire(target, superseded_by=filepath.name)

    _cache_drop(filepath)
    _rebuild_index_unlocked()
    return filepath, action


def _retire(entry: dict, *, superseded_by: str) -> None:
    """让一条被修正的记忆退出召回：标记 superseded_by 后移入 .archive/。

    归档而不是删除：既有约束是不删用户数据，`/memories archived` 仍能看到。
    留在 MEMORY_DIR 里会让它继续被扫到（list_memory_files 靠 superseded_by 过滤），
    两个机制交叠反而难排查，所以只保留一个。
    """
    h = _h()
    old_path = h.MEMORY_DIR / entry['filename']
    if not old_path.is_file():
        return
    text = old_path.read_text(encoding='utf-8', errors='replace')
    meta, body = _parse_frontmatter(text)
    retired = {**memory_schema.normalize_entry(meta, body, entry['filename']),
               'superseded_by': superseded_by}
    _atomic_write(old_path, _render_entry(retired))
    _cache_drop(old_path)
    memory_aging.archive(h.MEMORY_DIR, {'filename': entry['filename']})


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


def delete_memory_file(name: str) -> bool:
    """删除一条记忆。找不到返回 False（调用方据此给模型列出现有名字）。

    ``name`` 既可以是记忆名也可以是文件名：模型手里通常只有 ``remember`` 返回的
    文件名，而用户在界面上看到的是 name（两者经 ``_slug`` 后一致，但用户可能
    只记得原文）。

    删除后必须重建索引：``MEMORY.md`` 是召回入口，留着已删条目会让模型去读一个
    已经不存在的文件。
    """
    target = _slug(name)
    if not target:
        return False
    # _memory_path 会做目录穿越检查；这里复用它而不是自己拼路径。
    resolved = _memory_path(f'{target}.md')
    if resolved is None or not resolved.is_file():
        return False
    with _write_guard():
        resolved.unlink(missing_ok=True)
        _cache_drop(resolved)
        _rebuild_index_unlocked()
    _audit('memory_forgotten', {'name': str(name)})
    return True


def list_memory_files() -> list:  # 列出所有记忆文件
    """列出全部记忆条目（已归一成 schema v2 字段）。

    已 superseded 的条目**不返回**：它们的修正版已在列表里，两者同时参与召回
    可能取到错的那个（FAMA 惩罚的正是这种行为）。文件仍在磁盘上，可用
    `/memories archived` 看到。
    """
    h = _h()
    results = []
    for f in sorted(h.MEMORY_DIR.glob('*.md')):
        if f.name == 'MEMORY.md':
            continue
        entry = _cached_entry(f)
        if entry is None:
            continue
        meta, body, _raw = entry
        item = memory_schema.normalize_entry(meta, body, f.name)
        if item.get('superseded_by'):
            continue
        results.append(item)
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


#: 中英对照的开发常用词。召回是字面匹配，而记忆常被写成英文、查询常是中文
#: （实测英文库 + 中文查询命中率只有 40%）。这里在**查询侧**做一次扩展：命中
#: 任一方向的词就补上另一种写法，成本为零、完全确定性。
#:
#: 只收"一个词就能定位主题"的常见词。宁可少收——错的同义词会引入假召回，
#: 而漏一个只是少一条命中。
_GLOSSARY: dict[str, tuple[str, ...]] = {
    # 工程
    '部署': ('deploy', 'deployment', 'release'),
    '发布': ('release', 'deploy', 'publish'),
    '测试': ('test', 'tests', 'pytest', 'testing'),
    '构建': ('build',),
    '编译': ('compile', 'build'),
    '提交': ('commit',),
    '分支': ('branch',),
    '合并': ('merge',),
    '仓库': ('repo', 'repository'),
    '目录': ('directory', 'dir', 'layout', 'folder'),
    '文件': ('file',),
    '配置': ('config', 'configuration', 'settings'),
    '密钥': ('key', 'secret', 'credential'),
    '轮换': ('rotate', 'rotation'),
    '密码': ('password', 'passwd'),
    '日志': ('log', 'logs', 'logging'),
    '缓存': ('cache',),
    '数据库': ('database', 'db'),
    '接口': ('api', 'interface', 'endpoint'),
    '权限': ('permission', 'access', 'auth'),
    '错误': ('error', 'bug', 'issue'),
    '代码': ('code',),
    '风格': ('style', 'convention'),
    '缩进': ('indent', 'indentation', 'tabs', 'spaces'),
    '依赖': ('dependency', 'dependencies'),
    '版本': ('version',),
    '升级': ('upgrade', 'update'),
    '安装': ('install',),
    '启动': ('start', 'launch', 'run'),
    '重启': ('restart', 'reboot'),
    '停止': ('stop', 'kill'),
    '性能': ('performance', 'perf', 'speed'),
    '内存': ('memory', 'ram'),
    '磁盘': ('disk', 'storage'),
    '网络': ('network', 'net'),
    '超时': ('timeout',),
    '重试': ('retry', 'retries'),
    '备份': ('backup',),
    '恢复': ('restore', 'recover'),
    '文档': ('doc', 'docs', 'documentation', 'readme'),
    '注释': ('comment', 'comments'),
    '限制': ('limit', 'quota', 'threshold'),
    '账号': ('account',),
    '账户': ('account',),
    '审批': ('approval', 'approve'),
    '允许': ('allow', 'permit'),
    '拒绝': ('deny', 'block', 'reject'),
    '窗口': ('window',),
    '屏幕': ('screen', 'display'),
    '截图': ('screenshot', 'capture'),
    '剪贴板': ('clipboard', 'paste'),
    '任务': ('task', 'job'),
    '计划': ('schedule', 'plan', 'cron'),
    '定时': ('cron', 'schedule'),
    '记忆': ('memory', 'memories'),
    '会话': ('session', 'conversation', 'chat'),
    '工作区': ('workspace', 'workdir'),
    '环境': ('environment', 'env'),
    '沙箱': ('sandbox',),
    '模型': ('model',),
    '提示词': ('prompt',),
    '上下文': ('context',),
    '重写': ('rewrite', 'refactor'),
    '重构': ('refactor', 'rewrite'),
    '删除': ('delete', 'remove', 'rm'),
    '创建': ('create', 'add', 'new'),
    '修改': ('modify', 'edit', 'change', 'update'),
    '查看': ('view', 'show', 'list', 'read'),
    '搜索': ('search', 'grep', 'find'),
    '导出': ('export',),
    '导入': ('import',),
    # 表达风格
    '简短': ('brief', 'concise', 'short'),
    '简洁': ('concise', 'brief', 'terse'),
    '详细': ('detailed', 'verbose', 'elaborate'),
    '中文': ('chinese', 'zh'),
    '英文': ('english', 'en'),
    '回复': ('reply', 'answer', 'respond', 'response'),
    '回答': ('answer', 'reply', 'response'),
    # 生活/偏好（记忆里常见，中文表达差异大）
    '困': ('sleepy', 'tired', 'drowsy'),
    '累': ('tired', 'exhausted'),
    '茶': ('tea',),
    '咖啡': ('coffee',),
    '喝': ('drink', 'beverage'),
    '吃': ('eat', 'food', 'meal'),
    '早上': ('morning',),
    '下午': ('afternoon',),
    '晚上': ('evening', 'night'),
    '周三': ('wednesday',),
    '周末': ('weekend',),
    '喜欢': ('prefer', 'like', 'favourite', 'favorite'),
    '偏好': ('prefer', 'preference'),
    '不要': ('avoid', 'never'),
    '总是': ('always', 'usually'),
    '平时': ('usually', 'normally'),
}

#: 反向表（英文 → 中文），由 _GLOSSARY 生成，避免手写两份。
_GLOSSARY_REVERSE: dict[str, tuple[str, ...]] = {}
for _zh, _en in _GLOSSARY.items():
    for _word in _en:
        _GLOSSARY_REVERSE.setdefault(_word, ())
        _GLOSSARY_REVERSE[_word] += (_zh,)


def _expand_tokens(tokens) -> set[str]:
    """按中英术语表扩展查询词（双向）。"""
    expanded = set(tokens)
    for token in tokens:
        for zh, en in _GLOSSARY.items():
            if zh in token:
                expanded.update(en)
        for zh in _GLOSSARY_REVERSE.get(token, ()):
            expanded.add(zh)
    return expanded


def local_relevant_memories(messages: list, files: list | None = None,
                            max_items: int = 5) -> list:
    """本地记忆筛选：零网络往返。

    打分按**字段强度**分层，而不是数命中词个数：

    - ``keywords``/``name`` 命中 → 3 分。这两个字段是人工/抽取时精确标定的，
      命中一个就足以说明主题相关。中文查询尤其依赖这一条：短句往往只切出一两个
      二元组，按"命中数 ≥2"的旧规则会整条漏掉（实测「跑一下测试」召不回
      「跑测试用 pytest」，明明关键词完全对上）。
    - ``description`` 命中 → 英文词 2 分、中文二元组 1 分。

    阈值 3：一个关键词/名字命中，或描述里两个英文词、或三个中文二元组。

    两个加权（对应 PAHF 的 recency correctness 与 correction 优先级）：
    - ``correction`` 类 ×1.3：用户纠正过的信息比普通偏好更重要
    - 时间衰减：每 30 天 ×0.9，旧记忆排在后面但不会消失

    正文不参与：正文长，会把稀有词权重稀释，召回噪声大。
    """
    if files is None:
        files = list_memory_files()
    if not files:
        return []
    recent = _recent_user_text(messages)
    if not recent.strip():
        return []
    tokens = _expand_tokens(_local_tokens(recent))
    today = _date.today()
    scored = []
    for f in files:
        strong = ' '.join([str(f.get('name') or ''),
                           ' '.join(f.get('keywords') or [])]).lower()
        weak = str(f.get('description') or '').lower()
        score = 0
        for token in tokens:
            if token in strong:
                score += 3
            elif token in weak:
                score += 2 if len(token) > 2 and token.isascii() else 1
        if score < 3:
            continue
        if f.get('type') == 'correction':
            score *= 1.3
        score *= _recency_factor(f.get('last_update_date'), today)
        scored.append((score, f['filename']))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [filename for _, filename in scored[:max_items]]


def _recency_factor(stamp: str, today=None) -> float:
    """时间衰减：每 30 天 ×0.9。日期缺失/不可解析时不衰减（当作新鲜）。"""
    text = memory_schema.normalize_date(stamp)
    if not text:
        return 1.0
    try:
        when = _date.fromisoformat(text)
    except ValueError:
        return 1.0
    age = ((today or _date.today()) - when).days
    if age <= 0:
        return 1.0
    return 0.9 ** (age / 30.0)


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
    # 会话级开关优先：`/memories off|writeonly` 在本会话内不读记忆。
    # 进程级 HARNESS_LOAD_MEMORIES / HARNESS_MEMORY_RECALL 仍作为全局默认。
    if not memory_scope.use_enabled():
        return ''
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
    # 记一笔召回时刻：老化用「最后一次有用」而不是「最后一次写入」，否则
    # 一条被反复用到的旧偏好会因为写得早而被误淘汰。失败不影响对话。
    try:
        memory_aging.record_recall(_h().MEMORY_DIR, selected_files)
    except Exception:
        pass
    parts = ["<relevant_memories>"]
    # 优先级声明必须在第一行。回忆块追加在**用户消息之后**（为保住前缀缓存），
    # 而模型对末尾内容权重高——不声明就会被旧偏好盖过当前指令。
    # PAHF 的 injection eval 专门测这一项（over-influence）。
    parts.append(MEMORY_PRECEDENCE)
    for f in selected_files:
        content = read_memory_file(f)
        if content:
            parts.append(content)
    parts.append('</relevant_memories>')
    return '\n\n'.join(parts)


def extract_memories(messages: list):  # 提取用户偏好、约束、项目事实等记忆
    # `/memories readonly|off` 时本会话不产生新记忆（Codex 的 chat-level
    # contribute 开关）。放在最前面：后面要调一次模型，能省就省。
    if not memory_scope.contribute_enabled():
        return
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

    # post-action 反馈：从「模型做了什么 → 用户怎么反应」里学。
    # PAHF 的结论是这一步才是纠错的核心（pre-action 询问无法解决偏好漂移）。
    # 只产出提示文本，不直接写记忆——避免两处都在判断“什么值得记”。
    try:
        feedback_block = memory_feedback.as_prompt_block(
            memory_feedback.collect(messages))
    except Exception as exc:
        print(f'  \033[33m[memory] feedback signals skipped: '
              f'{type(exc).__name__}: {exc}\033[0m')
        feedback_block = ''

    existing = list_memory_files()
    existing_desc = '\n'.join(f"- {m['name']}: {m['description']}" for m in existing) if existing else "(none)"

    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: "
        "{name, type, description, body, keywords, supersedes}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'preference', 'correction' (the user corrected the "
        "assistant), 'fact', 'context'\n"
        "- description: one-line summary; write conditional preferences as "
        "'when <condition>: <preference>'\n"
        "- body: full detail in markdown\n"
        "- keywords: 3-6 short search terms. Include BOTH the user's language and "
        "English (e.g. '部署,发布,deploy,deployment'), plus any condition words. "
        "Recall is lexical, so a Chinese query only finds an English memory if "
        "the English term is also present as a keyword.\n"
        "- supersedes: OPTIONAL. If this item corrects or replaces one of the "
        "existing memories listed below, put that memory's name here.\n"
        "If nothing new or already covered by existing memories, return [].\n"
        "OUTPUT FORMAT (STRICT): reply with ONLY a plain JSON array. "
        "Do NOT think out loud, do NOT explain, do NOT wrap it in markdown code "
        "fences (```json), do NOT add any text before or after the array.\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        + (f"{feedback_block}\n\n" if feedback_block else '')
        + f"Dialogue:\n{dialogue[:4000]}"
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
            # 归一旧词表 + 未知类型回落：抽取是自由文本，
            # 一个字段写坏不该让整条记忆丢掉（合并路径另有严格校验）。
            mem_type = memory_schema.canonical_type(mem.get('type'))
            desc = ' '.join(str(mem.get('description') or '').split())
            body = str(mem.get('body') or '').strip()
            if desc and body:
                write_memory_file(
                    name, mem_type, desc, body,
                    keywords=mem.get('keywords'),
                    supersedes=str(mem.get('supersedes') or ''))
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
        # 类型归一：旧词表（user/feedback/project/reference）映射到规范词表，
        # 否则 `remember` 写入的记忆会让整批合并永久失败（已修的真 bug）。
        # 但**未知类型仍必须报错**：合并会删旧文件，把一个格式错误的 LLM
        # 输出静默当成 fact，等于用错数据换掉正确数据。
        try:
            mem_type = memory_schema.canonical_type(item.get('type'), strict=True)
        except ValueError:
            raise ValueError(
                f'item {index} has invalid type {item.get("type")!r}') from None
        description = ' '.join(str(item.get('description') or '').split())
        body = str(item.get('body') or '').strip()
        if not name or not description or not body:
            raise ValueError(f'item {index} is missing name/description/body')
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
                _atomic_write(path, _render_entry({
                    'name': item['name'], 'type': item['type'],
                    'description': item['description'], 'body': item['body'],
                }))
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


#: session 层条目在多少天后归档。PAHF 的 session notes 合并即清除；我们给一天
#: 宽限，让同一会话的后续轮次还能用到它，隔天就不再干扰。
SESSION_SCOPE_DAYS = 1


def archive_session_scope(*, days: int = SESSION_SCOPE_DAYS,
                          today=None) -> list[dict]:
    """归档过期的 session 层记忆（一次性上下文不该长期留在库里）。

    与 ``age_memories`` 分开：那条管"太久没用的长期记忆"，这条管"本来就不该
    长期存在的一次性上下文"。混在一起会让两套理由互相污染——排查时看不出
    一条记忆是被"老化"还是被"降级"归档的。

    同样只归档不删除。
    """
    h = _h()
    limit = _date.today() - _timedelta(days=max(0, days))
    stale = []
    for item in list_memory_files():
        if item.get('scope') != 'session':
            continue
        stamp = memory_schema.normalize_date(item.get('last_update_date'))
        if not stamp:
            continue
        try:
            written = _date.fromisoformat(stamp)
        except ValueError:
            continue
        if written <= limit:
            stale.append(item)
    if not stale:
        return []
    moved = []
    with _write_guard():
        for item in stale:
            target = memory_aging.archive(h.MEMORY_DIR, item)
            if target is not None:
                moved.append({**item, 'archived_as': target.name})
        if moved:
            _cache_clear()
            _rebuild_index_unlocked()
    for item in moved:
        _audit('memory_archived', {'name': item.get('name', ''),
                                   'age_days': 0})
    return moved


def age_memories(days: int | None = None) -> list[dict]:
    """把"该退出召回了"的记忆移到 .archive/。返回被归档的条目。

    **不删除**：归档目录里的文件可以随时放回。我们有一条既有约束是不删除
    用户数据，淘汰也必须遵守。

    跳过 preference / correction：人的偏好不会因为三个月没提就失效，教训
    更是要一直记得。
    """
    h = _h()
    entries = list_memory_files()
    # 两种归档理由，各自独立：
    # - stale：放了很久没动（时间维度）
    # - low_signal：从没被召回、置信度低、过了观察期（信号维度）
    # 合并去重，同一条只归档一次。都只归档不删除。
    stale = memory_aging.stale_entries(h.MEMORY_DIR, entries, days=days)
    marked = {item.get('filename') for item in stale}
    stale += [item for item in memory_aging.low_signal_entries(
        h.MEMORY_DIR, entries) if item.get('filename') not in marked]
    if not stale:
        return []
    moved = []
    with _write_guard():
        for entry in stale:
            target = memory_aging.archive(h.MEMORY_DIR, entry)
            if target is not None:
                moved.append({**entry, 'archived_as': target.name})
        if moved:
            _cache_clear()
            _rebuild_index_unlocked()
    if moved:
        names = ', '.join(item['name'] for item in moved[:3])
        more = f' 等{len(moved)} 条' if len(moved) > 3 else ''
        print(f'  \033[90m[memory] archived {names}{more} '
              f'(太久没用到；可用 /memories archived 找回)\033[0m')
        for item in moved:
            _audit('memory_archived', {'name': item.get('name', ''),
                                       'age_days': item.get('age_days', 0)})
    return moved


def consolidate_memories():
    """Merge duplicate/stale memories. Triggered when file count ≥ threshold."""
    h = _h()
    # 先清理再合并：归档后可能已经低于阈值，那就省一次模型调用。
    # 两条清理理由不同——age_memories 管"太久没用的长期记忆"，
    # archive_session_scope 管"本来就不该长期存在的一次性上下文"。
    try:
        archive_session_scope()
        age_memories()
    except Exception as exc:
        print(f'  \033[33m[memory] aging skipped: {type(exc).__name__}: {exc}\033[0m')
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
        "5. A correction ALWAYS wins: if a 'correction' memory contradicts an "
        "earlier 'user' memory, delete the old one instead of keeping both. "
        "A wrong assumption and its fix coexisting means recall may pick the "
        "wrong one.\n"
        "6. Conditional preferences are not contradictions: 'when X: prefer A' "
        "and 'prefer B' can coexist only if B is true outside condition X. "
        "Never drop the condition.\n"
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
