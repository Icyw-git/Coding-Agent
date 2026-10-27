"""域名授权账本：把「这个域名以后可以直接访问」从一次审批变成可撤销的名单。

与 ``grants.py``（目录写授权）同构，刻意分成两个模块：

- 目录授权是**放宽一个限制**，风险是写错文件；
- 域名授权是**放宽外发边界**，风险是把数据送出去。

所以两者的存储分开、撤销面分开、``deny`` 的优先级也不同：域名 deny 是硬拒，
连 ``danger-full-access`` 都绕不过（与秘密文件同性质）；目录授权只放宽越界写。

账本放在 ``<home>/.cache/net_grants.json``，结构与读取策略沿用 grants：

- 写入走跨进程文件锁 + 临时文件原子替换；
- 读取不加锁（原子替换保证不会读到半个文件）；
- 损坏、未知版本、字段不合法时 **fail-closed**：策略层看不到任何规则，
  等于回到「未知域名逐个审批」；
- 修改操作遇到损坏账本直接报错，不静默覆盖用户数据。

授权只能由人给出：没有模型工具能写这份文件。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime
from pathlib import Path

import net_policy
import paths
from file_lock import exclusive_file_lock

VERSION = 1
DURABLE_PATH = paths.cache_path('net_grants.json')
LOCK_PATH = paths.cache_path('locks/net_grants.lock')
MAX_NOTE_CHARS = 200
MAX_RECORDS = 200


class NetGrantError(ValueError):
    """域名账本内容不合法。"""


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        import locale
        fallback = locale.getpreferredencoding(False) or 'utf-8'
        return raw.decode(fallback)


def _corrupt(message: str, strict: bool) -> list[dict]:
    if strict:
        raise NetGrantError(message)
    print(f'  \033[31m[net_grants] {message}; ignoring all domain rules\033[0m')
    return []


def _clean_record(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    try:
        pattern = net_policy.normalize_pattern(item.get('host'))
    except net_policy.NetPolicyError:
        return None
    scope = str(item.get('scope') or '').strip().casefold()
    if scope not in net_policy.SCOPES:
        return None
    expires = str(item.get('expires') or '').strip()
    if expires:
        try:
            date.fromisoformat(expires)
        except ValueError:
            return None    # 坏日期不能当成“永久有效”
    return {
        'host': pattern,
        'scope': scope,
        'expires': expires,
        'created_by': str(item.get('created_by') or '')[:MAX_NOTE_CHARS],
        'note': str(item.get('note') or '')[:MAX_NOTE_CHARS],
        'session_id': str(item.get('session_id') or '')[:MAX_NOTE_CHARS],
        'created_at': str(item.get('created_at') or ''),
    }


# 每次工具调用都会读一次域名账本。缓存键是 (路径, mtime_ns, size)：改了文件
# 立刻失效，不会出现「改了规则不生效」。与 rules.load 同一手法。
# 只缓存非 strict 读（strict 是修改前的校验，必须看最新盘上内容）。
_CACHE: dict[tuple[str, int, int], list[dict]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 8


def _cache_key() -> tuple[str, int, int] | None:
    try:
        stat = Path(DURABLE_PATH).stat()
    except OSError:
        return None
    return (str(DURABLE_PATH), stat.st_mtime_ns, stat.st_size)


def _read(strict: bool = False) -> list[dict]:
    if not DURABLE_PATH.exists():
        return []
    cached = None
    if not strict:
        key = _cache_key()
        if key is not None:
            with _CACHE_LOCK:
                cached = _CACHE.get(key)
            if cached is not None:
                return [dict(record) for record in cached]
    records = _read_uncached(strict)
    if not strict:
        key = _cache_key()
        if key is not None:
            with _CACHE_LOCK:
                if len(_CACHE) >= _CACHE_MAX:
                    _CACHE.clear()
                _CACHE[key] = [dict(record) for record in records]
    return records


def _read_uncached(strict: bool = False) -> list[dict]:
    try:
        data = json.loads(_read_text(DURABLE_PATH))
    except Exception as exc:
        return _corrupt(f'cannot read {DURABLE_PATH}: {exc}', strict)
    if not isinstance(data, dict) or data.get('version') != VERSION:
        return _corrupt(f'unsupported net grants version in {DURABLE_PATH}', strict)
    raw = data.get('domains')
    if not isinstance(raw, list):
        return _corrupt(f'invalid domains list in {DURABLE_PATH}', strict)
    records = []
    for item in raw:
        record = _clean_record(item)
        if record is None:
            print(f'  \033[33m[net_grants] skipping malformed record: {item!r}\033[0m')
            continue
        records.append(record)
    return records


def _save(records: list[dict]) -> None:
    DURABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {'version': VERSION, 'domains': records}
    tmp = DURABLE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    tmp.replace(DURABLE_PATH)


def _validate_expiry(raw) -> str:
    if raw in (None, ''):
        return ''
    if isinstance(raw, date):
        value = raw
    else:
        try:
            value = date.fromisoformat(str(raw).strip())
        except ValueError as exc:
            raise NetGrantError('expires must be YYYY-MM-DD') from exc
    if value < date.today():
        raise NetGrantError('expires must not be in the past')
    return value.isoformat()


def add(host, *, scope: str = net_policy.ALLOW, expires=None, note: str = '',
        created_by: str = '', session_id: str = '') -> dict:
    """新增或更新一条域名规则；返回落盘后的记录。"""
    pattern = net_policy.normalize_pattern(host)
    scope = str(scope or '').strip().casefold()
    if scope not in net_policy.SCOPES:
        raise NetGrantError(f'scope must be one of {", ".join(net_policy.SCOPES)}')
    expiry = _validate_expiry(expires)
    record = {
        'host': pattern,
        'scope': scope,
        'expires': expiry,
        'created_by': str(created_by or '')[:MAX_NOTE_CHARS],
        'note': str(note or '')[:MAX_NOTE_CHARS],
        'session_id': str(session_id or '')[:MAX_NOTE_CHARS],
        'created_at': datetime.now().isoformat(timespec='seconds'),
    }
    with exclusive_file_lock(LOCK_PATH):
        records = _read(strict=True)
        for index, existing in enumerate(records):
            if existing['host'] == pattern:
                record['created_at'] = (existing.get('created_at')
                                        or record['created_at'])
                records[index] = record
                break
        else:
            if len(records) >= MAX_RECORDS:
                raise NetGrantError(f'refusing more than {MAX_RECORDS} domain rules')
            records.append(record)
        _save(records)
    return dict(record)


def remove(host) -> dict | None:
    """按模式删除；不存在返回 None。"""
    pattern = net_policy.normalize_pattern(host)
    with exclusive_file_lock(LOCK_PATH):
        records = _read(strict=True)
        kept = [record for record in records if record['host'] != pattern]
        if len(kept) == len(records):
            return None
        _save(kept)
    return next(record for record in records if record['host'] == pattern)


def revoke_for_session(session_id: str) -> list[dict]:
    """撤销某个会话创建的域名规则。"""
    session_id = str(session_id or '').strip()
    if not session_id:
        return []
    with exclusive_file_lock(LOCK_PATH):
        records = _read(strict=True)
        removed = [record for record in records
                   if record.get('session_id') == session_id]
        if not removed:
            return []
        _save([record for record in records
               if record.get('session_id') != session_id])
    return removed


def _is_active(record: dict, today: date) -> bool:
    expires = record.get('expires') or ''
    return not expires or date.fromisoformat(expires) >= today


def list_rules(*, today: date | None = None) -> list[dict]:
    """全部记录（含过期项），附 ``active`` 便于卡片显示。"""
    current = today or date.today()
    rows = []
    for record in _read():
        row = dict(record)
        row['active'] = _is_active(record, current)
        rows.append(row)
    return sorted(rows, key=lambda row: (row['scope'], row['host']))


def active_rules(*, today: date | None = None) -> tuple[tuple[str, str], ...]:
    """策略层用的有效规则。读取失败时返回空元组（fail-closed）。"""
    current = today or date.today()
    active = []
    for record in _read():
        if not _is_active(record, current):
            continue
        active.append((record['host'], record['scope']))
    return tuple(active)


def counts(*, today: date | None = None) -> tuple[int, int]:
    """(allow, deny) 计数，用于 /status 与控制器。"""
    allowed = denied = 0
    for _pattern, scope in active_rules(today=today):
        if scope == net_policy.ALLOW:
            allowed += 1
        elif scope == net_policy.DENY:
            denied += 1
    return allowed, denied


def check_writable() -> None:
    """测试与排障用：账本可读且版本受支持。"""
    _read(strict=True)


def ledger_path() -> Path:
    return Path(os.fspath(DURABLE_PATH))
