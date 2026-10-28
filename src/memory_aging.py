"""记忆的老化与归档（N2）。

为什么需要
----------
``memory.py`` 只有 ``_MAX_MEMORY_ITEMS = 50`` 的**数量上界**（超了报错），没有
时间维度。用一年后，50 条里全是早期闲聊，最近有用的偏好挤不进去——而且报错的
是用户，模型只会看到写入失败。

Codex 的做法是双时间淘汰：rollout 30 天未用淘汰、单条记忆 30 天未被召回清除。

三个不照抄的地方
----------------
1. **不静默删除**：淘汰前移到 ``memories/.archive/``，``/memories`` 能看到并恢复。
   我们有一条既有约束是「不删除用户数据」，淘汰也必须遵守。
2. **偏见过期要慢**：``preference`` 类不过期（人的偏好不会因为三个月没提就失效），
   但年龄要可见——让用户自己判断，而不是替他想。
3. **不写回记忆文件**：记录「最后一次被召回」需要频繁写入，直接改 md 会不停
   扰动 mtime（而 mtime 正是老化依据）和前缀缓存。这里用独立的
   ``.recall-log.json`` 存召回时刻，读失败一律降级为「当作刚用过」。

判定依据
--------
- 「多久没用」取 ``created/mtime`` 与召回记录的**较大者**：刚写过也算用过。
- 只淘汰 ``fact`` / ``context``：这两类有时效性（"部署走 X"改版后就错了）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import settings

#: 默认保留天数。Codex 用 30 天；我们放宽到 90 天——单人本地记忆条数本来就少，
#: 太激进会让人觉得"它把我说的忘了"。
DEFAULT_DAYS = 90

#: 永不因时间淘汰的类型。``preference`` 是长期事实，``correction`` 是教训。
KEEP_FOREVER = frozenset({'preference', 'correction'})

#: 召回记录文件名（放在 memories 目录里，但不以 .md 结尾，不会被索引扫到）。
LOG_NAME = '.recall-log.json'

ENV_DAYS = 'HARNESS_MEMORY_MAX_AGE_DAYS'


def threshold_days() -> int:
    raw = os.getenv(ENV_DAYS, '').strip()
    if not raw:
        return DEFAULT_DAYS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_DAYS
    return value if value > 0 else DEFAULT_DAYS


def enabled() -> bool:
    return settings.flag('HARNESS_MEMORY_AGING', True)


def _log_path(memory_dir: Path) -> Path:
    return Path(memory_dir) / LOG_NAME


def _read_log(memory_dir: Path) -> dict:
    path = _log_path(memory_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        # 记录损坏不是错误状态：老化不该因此删掉任何东西。
        return {}
    return data if isinstance(data, dict) else {}


def _write_log(memory_dir: Path, data: dict) -> None:
    path = _log_path(memory_dir)
    tmp = path.with_name(path.name + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0),
                       encoding='utf-8')
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)


def record_recall(memory_dir, names) -> None:
    """记一次召回时刻。失败只忽略——诊断信息不该影响一轮对话。"""
    if not enabled():
        return
    names = [str(name) for name in (names or []) if name]
    if not names:
        return
    memory_dir = Path(memory_dir)
    data = _read_log(memory_dir)
    now = int(time.time())
    for name in names:
        data[name] = now
    _write_log(memory_dir, data)


def last_used(memory_dir, filename: str, path: Path, *, log: dict | None = None) -> float:
    """这条记忆最后一次「有用」的时刻（写入时间与召回时间取大）。

    ``log`` 允许调用方传入已读好的召回记录。扫描全部记忆时**必须**这么传：
    不传的话每条都要读盘 + 解析一次 json（实测 30 条时 age_memories 要 74ms，
    而它每轮合并后都会跑一遍）。
    """
    stamps = []
    try:
        stamps.append(path.stat().st_mtime)
    except OSError:
        pass
    records = _read_log(Path(memory_dir)) if log is None else log
    stamp = records.get(filename)
    if isinstance(stamp, (int, float)):
        stamps.append(float(stamp))
    return max(stamps) if stamps else time.time()


def age_days(memory_dir, filename: str, path: Path, *,
             log: dict | None = None) -> int:
    return int((time.time() - last_used(memory_dir, filename, path, log=log))
               // 86400)


def stale_entries(memory_dir, entries, *, days: int | None = None) -> list[dict]:
    """挑出超过保留期、且类型允许淘汰的条目。不碰磁盘。"""
    if not enabled():
        return []
    limit = days if days is not None else threshold_days()
    memory_dir = Path(memory_dir)
    recency = _read_log(memory_dir)      # 读一次，不要每条重读
    out = []
    for item in entries:
        filename = item.get('filename') or ''
        if not filename.endswith('.md'):
            continue
        mem_type = str(item.get('type') or '').strip().lower()
        if mem_type in KEEP_FOREVER:
            continue
        path = memory_dir / filename
        if not path.is_file():
            continue
        age = age_days(memory_dir, filename, path, log=recency)
        if age >= limit:
            out.append({**item, 'age_days': age})
    return out


#: 低信号剪枝默认参数。三种条件同时成立才归档：
#: 从没被召回、置信度低、写入已过观察期。
LOW_SIGNAL_DAYS = 30
LOW_SIGNAL_CONFIDENCE = 0.3


def low_signal_entries(memory_dir, entries, *, days: int | None = None,
                       confidence: float | None = None) -> list[dict]:
    """挑出"一直没被用到、也不太可信"的条目。不碰磁盘。

    与 ``stale_entries`` 的区别：那条只看时间（90 天没动），这条看**信号**——
    一条从没被召回过的低置信条目，即使只放了一周也在占召回预算。

    三个条件必须同时成立，缺一不可：

    - **从未被召回**：召回记录里没有它。只要有召回记录，就说明它对某次对话有用。
    - **置信度低**：默认 < 0.3。用户明确要求写的记忆（0.9）不会因为没召回就被清。
    - **过了观察期**：默认 30 天。新写入的条目还没来得及被用到。

    ``preference``/``correction`` 仍然豁免：偏好和教训不该因为"暂时没召回"消失。
    """
    if not enabled():
        return []
    limit_days = LOW_SIGNAL_DAYS if days is None else days
    floor = LOW_SIGNAL_CONFIDENCE if confidence is None else confidence
    memory_dir = Path(memory_dir)
    recalls = _read_log(memory_dir)
    out = []
    for item in entries:
        filename = item.get('filename') or ''
        if not filename.endswith('.md'):
            continue
        if str(item.get('type') or '').strip().lower() in KEEP_FOREVER:
            continue
        if filename in recalls:
            continue                      # 被用到过，不是低信号
        value = item.get('confidence')
        if not isinstance(value, (int, float)) or value >= floor:
            continue
        path = memory_dir / filename
        if not path.is_file():
            continue
        age = age_days(memory_dir, filename, path, log=recalls)
        if age < limit_days:
            continue
        out.append({**item, 'age_days': age, 'confidence': value})
    return out


def archive(memory_dir, entry) -> Path | None:
    """把一条记忆移到 .archive/，不删除。返回归档后的路径。"""
    memory_dir = Path(memory_dir)
    filename = entry.get('filename') or ''
    if not filename.endswith('.md'):
        return None
    source = memory_dir / filename
    if not source.is_file():
        return None
    target_dir = memory_dir / '.archive'
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    if target.exists():
        # 同名冲突时加时间戳，绝不覆盖已归档内容。
        target = target_dir / f'{source.stem}-{int(time.time())}.md'
    try:
        source.replace(target)
    except OSError:
        return None
    return target


def archived_entries(memory_dir) -> list[dict]:
    """列出已归档条目（``/memories archived`` 用）。"""
    directory = Path(memory_dir) / '.archive'
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob('*.md')):
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append({'filename': path.name,
                    'age_days': int((time.time() - stat.st_mtime) // 86400)})
    return out


def restore_archived(memory_dir, filename: str) -> bool:
    """把一条归档的记忆放回 memories/。"""
    memory_dir = Path(memory_dir)
    name = Path(str(filename or '')).name
    if not name.endswith('.md') or '/' in name or '\\' in name:
        return False
    source = memory_dir / '.archive' / name
    if not source.is_file():
        return False
    target = memory_dir / name
    if target.exists():
        return False
    try:
        source.replace(target)
    except OSError:
        return False
    return True
