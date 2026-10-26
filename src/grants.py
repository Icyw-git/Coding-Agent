"""目录级持久授权：把「这个目录以后可以写」从一次审批变成可撤销的账本。

账本放在 ``<home>/.cache/grants.json``，只记录 ``scope=write`` 的目录。策略层
（``exec_policy``）在判定「工作区外写入」时先查这里：命中的路径直接放行，不再
弹审批。秘密文件与系统目录的硬拒在授权之前，授权不能覆盖它们；``read-only``
档仍按只读档审批，持久授权只放宽「越界」这一条，不改变沙箱档位。

授权只能由人给出：没有模型工具能写这份文件；飞书侧是 ``/grant add`` 命令或
API，模型只能通过 ``revoke_permissions`` 撤销**本会话创建**的授权。

文件是小型读多写少的 JSON：写入走跨进程文件锁 + 临时文件原子替换；读取不
加锁（原子替换保证不会读到半个文件）。损坏或版本不认识时 fail-closed：策略层
看不到任何授权，改动操作则报错而不是静默清空。
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path

import paths
from exec_policy import system_roots
from file_lock import exclusive_file_lock

VERSION = 1
DURABLE_PATH = paths.cache_path('grants.json')
LOCK_PATH = paths.cache_path('locks/grants.lock')
SCOPE_WRITE = 'write'
MAX_PATH_CHARS = 1024
MAX_NOTE_CHARS = 200


class GrantError(ValueError):
    """授权参数或账本内容不合法。"""


def split_path_argument(text: str) -> tuple[str, str]:
    """拆出路径与剩余参数；路径含空格时必须用引号包起来。

    不用 shlex：POSIX 规则会把 Windows 反斜杠当转义符，``D:\\notes`` 会变成
    ``D:notes``，看起来授权成功、实际指向另一个目录。
    """
    text = (text or '').strip()
    if not text:
        return '', ''
    if text[0] in ('"', "'"):
        quote = text[0]
        end = text.find(quote, 1)
        if end < 0:
            raise ValueError('路径引号未闭合')
        return text[1:end], text[end + 1:].strip()
    parts = text.split(maxsplit=1)
    return parts[0], (parts[1] if len(parts) > 1 else '')


def parse_options(text: str) -> tuple[str, str]:
    """解析 ``until YYYY-MM-DD`` 与 ``note 任意文字``（note 吃掉末尾）。"""
    expires = ''
    note = ''
    rest = (text or '').strip()
    match = re.search(r'(?:^|\s)until\s+(\d{4}-\d{2}-\d{2})(?=\s|$)', rest)
    if match:
        expires = match.group(1)
        rest = (rest[:match.start()] + ' ' + rest[match.end():]).strip()
    match = re.search(r'(?:^|\s)note\s+(.*)$', rest, re.S)
    if match:
        note = match.group(1).strip()
        rest = rest[:match.start()].strip()
    if rest:
        raise ValueError(f'无法识别的参数: {rest}')
    return expires, note


def _read(strict: bool = False) -> list[dict]:
    if not DURABLE_PATH.exists():
        return []
    try:
        data = json.loads(_read_text(DURABLE_PATH))
    except Exception as exc:
        return _corrupt(f'cannot read {DURABLE_PATH}: {exc}', strict)
    if not isinstance(data, dict) or data.get('version') != VERSION:
        return _corrupt(f'unsupported grants file version in {DURABLE_PATH}', strict)
    raw = data.get('paths')
    if not isinstance(raw, list):
        return _corrupt(f'invalid paths list in {DURABLE_PATH}', strict)
    records = []
    for item in raw:
        record = _clean_record(item)
        if record is None:
            print(f'  \033[33m[grants] skipping malformed record: {item!r}\033[0m')
            continue
        records.append(record)
    return records


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
        raise GrantError(message)
    print(f'  \033[31m[grants] {message}; ignoring all path grants\033[0m')
    return []


def _clean_record(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    raw_path = str(item.get('path') or '').strip()
    if not raw_path or '\x00' in raw_path or len(raw_path) > MAX_PATH_CHARS:
        return None
    try:
        resolved = Path(raw_path).expanduser()
        if not resolved.is_absolute():
            return None
        resolved = resolved.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if str(item.get('scope') or SCOPE_WRITE) != SCOPE_WRITE:
        return None
    expires = str(item.get('expires') or '').strip()
    if expires:
        try:
            date.fromisoformat(expires)
        except ValueError:
            return None   # 坏日期不能当成“永久有效”
    return {
        'path': str(resolved),
        'scope': SCOPE_WRITE,
        'expires': expires,
        'created_by': str(item.get('created_by') or '')[:MAX_NOTE_CHARS],
        'note': str(item.get('note') or '')[:MAX_NOTE_CHARS],
        'session_id': str(item.get('session_id') or '')[:MAX_NOTE_CHARS],
        'created_at': str(item.get('created_at') or ''),
    }


def _norm(path) -> str:
    return os.path.normcase(str(Path(path)))


def _save(records: list[dict]) -> None:
    DURABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {'version': VERSION, 'paths': records}
    tmp = DURABLE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    tmp.replace(DURABLE_PATH)


def _validate_path(raw) -> Path:
    text = str(raw or '').strip()
    if not text:
        raise GrantError('path must not be empty')
    if '\x00' in text:
        raise GrantError('path contains a NUL byte')
    if len(text) > MAX_PATH_CHARS:
        raise GrantError(f'path is longer than {MAX_PATH_CHARS} characters')
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise GrantError('path must be absolute')
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise GrantError(f'cannot resolve path: {exc}') from exc
    if resolved.parent == resolved:
        raise GrantError('refusing to grant a filesystem root')
    if not resolved.exists():
        raise GrantError('path does not exist')
    if not resolved.is_dir():
        raise GrantError('path is not a directory')
    for root in system_roots():
        if resolved == root or resolved.is_relative_to(root):
            raise GrantError('refusing to grant a protected system directory')
    return resolved


def _validate_expiry(raw) -> str:
    if raw in (None, ''):
        return ''
    if isinstance(raw, date):
        value = raw
    else:
        try:
            value = date.fromisoformat(str(raw).strip())
        except ValueError as exc:
            raise GrantError('expires must be YYYY-MM-DD') from exc
    if value < date.today():
        raise GrantError('expires must not be in the past')
    return value.isoformat()


def add_path(path, *, expires=None, note: str = '', created_by: str = '',
             session_id: str = '') -> dict:
    """新增或更新一个目录写授权；返回落盘后的记录。"""
    resolved = _validate_path(path)
    expiry = _validate_expiry(expires)
    record = {
        'path': str(resolved),
        'scope': SCOPE_WRITE,
        'expires': expiry,
        'created_by': str(created_by or '')[:MAX_NOTE_CHARS],
        'note': str(note or '')[:MAX_NOTE_CHARS],
        'session_id': str(session_id or '')[:MAX_NOTE_CHARS],
        'created_at': datetime.now().isoformat(timespec='seconds'),
    }
    with exclusive_file_lock(LOCK_PATH):
        records = _read(strict=True)
        key = _norm(resolved)
        for index, existing in enumerate(records):
            if _norm(existing['path']) == key:
                record['created_at'] = existing.get('created_at') or record['created_at']
                records[index] = record
                break
        else:
            records.append(record)
        _save(records)
    return dict(record)


def _lookup_path(raw) -> Path:
    """查询/删除用的路径归一：只要求绝对、可解析，不要求已存在。"""
    text = str(raw or '').strip()
    if not text or '\x00' in text:
        raise GrantError('path must not be empty')
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise GrantError('path must be absolute')
    try:
        return candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise GrantError(f'cannot resolve path: {exc}') from exc


def find_path(path) -> dict | None:
    target = _norm(_lookup_path(path))
    for record in _read():
        if _norm(record['path']) == target:
            return dict(record)
    return None


def remove_path(path) -> dict | None:
    """按路径删除授权；不存在返回 None。"""
    target = _norm(_lookup_path(path))
    with exclusive_file_lock(LOCK_PATH):
        records = _read(strict=True)
        kept = [record for record in records
                if _norm(record['path']) != target]
        if len(kept) == len(records):
            return None
        _save(kept)
    return next(record for record in records
                if _norm(record['path']) == target)


def revoke_for_session(session_id: str) -> list[dict]:
    """撤销某个会话创建的授权；模型只能通过这条路收回自己的授权。"""
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


def list_grants(*, today: date | None = None) -> list[dict]:
    """全部记录（含过期项），附 ``active`` 便于卡片显示。"""
    current = today or date.today()
    rows = []
    for record in _read():
        row = dict(record)
        row['active'] = _is_active(record, current)
        rows.append(row)
    return sorted(rows, key=lambda row: row['path'].casefold())


def active_write_paths(*, today: date | None = None) -> tuple[Path, ...]:
    """策略层用的有效授权路径。读取失败时返回空元组（fail-closed）。"""
    current = today or date.today()
    active = []
    for record in _read():
        if not _is_active(record, current):
            continue
        try:
            path = Path(record['path'])
        except (TypeError, ValueError):
            continue
        if path.is_absolute():
            active.append(path)
    return tuple(active)
