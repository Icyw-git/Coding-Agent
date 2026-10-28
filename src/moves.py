"""安全的移动/重命名：`move_paths` 的能力层。

为什么需要专用工具
------------------
`delete_paths` 走回收站、可撤销、逐次审批；而 `mv` / `move` / `ren` 全在 bash
名单里，**没有专用工具，也不可撤销**。更常见的操作（移动）比更危险的操作
（删除）保护更弱——这个倒挂是真实的：一次 `mv` 手误会直接丢文件。

三层分工（与删除保持一致）
--------------------------
- 本模块：**怎么移动**（校验、冲突检测、执行、撤销日志）
- `exec_policy`：**该不该移动**（审批、路径分区）
- `tool_move`：**怎么说清楚**（预览文案、审计）

两条硬约束
----------
1. **先全部校验，再动手**。任何一项冲突（源不存在、目标已存在、跨盘符、目标在
   源目录内）都在执行前报出来。批量移动执行到一半失败，用户面对的是一个
   半完成状态，比干脆失败糟糕得多。
2. **一律可撤销**。每次成功移动写一条记录（from/to），支持按批次撤销。
   不提供"永久移动"这种模式——需要永久请用 bash，但那样至少用户知道自己在
   拼命令。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

#: 撤销日志文件名（放在 cache 下）。
UNDO_NAME = 'moves.jsonl'

#: 单批最多移动多少项。与 delete_paths 的 50 项上限对齐。
MAX_ITEMS = 50


class MoveError(Exception):
    """移动失败。文案直接给模型，所以要说清下一步。"""


def _journal_path(cache_dir) -> Path:
    return Path(cache_dir) / UNDO_NAME


def _resolve_source(raw) -> Path:
    candidate = Path(raw).expanduser()
    try:
        return candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise MoveError(f'cannot resolve source: {exc}') from exc


def _resolve_target(raw, *, base: Path) -> Path:
    """目标路径：相对路径按**源文件所在目录**解析。

    这样 ``move_paths([{from: 'a.txt', to: 'old/a.txt'}])`` 的语义是"在同一个
    文件夹里归到 old/ 子目录"，符合直觉；按 cwd 解析会让用户以为移动成功却在
    另一个位置。
    """
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise MoveError(f'cannot resolve target: {exc}') from exc


def validate_moves(moves, *, max_items: int = MAX_ITEMS) -> list[dict]:
    """校验整批移动，返回规范化后的 ``[{from, to}]``（都是绝对 Path）。

    **一项不合格就整批拒绝**：批量移动执行到一半失败会留下半完成状态。
    """
    if isinstance(moves, dict):
        moves = [moves]
    if not isinstance(moves, list) or not moves:
        raise MoveError('moves must be a non-empty list of {from, to}')
    if len(moves) > max_items:
        raise MoveError(f'too many items ({len(moves)} > {max_items})')

    prepared = []
    seen_sources: set[Path] = set()
    seen_targets: set[Path] = set()
    for index, item in enumerate(moves, start=1):
        if not isinstance(item, dict):
            raise MoveError(f'item {index} is not an object with from/to')
        raw_from = str(item.get('from') or '').strip()
        raw_to = str(item.get('to') or '').strip()
        if not raw_from or not raw_to:
            raise MoveError(f'item {index} needs both from and to')

        source = _resolve_source(raw_from)
        if not source.exists() and not source.is_symlink():
            raise MoveError(f'item {index}: source does not exist: {source}')
        if source.parent == source or (source.anchor and source == Path(source.anchor)):
            raise MoveError(f'item {index}: refusing to move a filesystem root')

        target = _resolve_target(raw_to, base=source.parent)
        if target == source:
            raise MoveError(f'item {index}: target is the same as source: {source}')
        if target.exists() or target.is_symlink():
            raise MoveError(f'item {index}: target already exists: {target}')
        # 移到自己的子目录里：操作系统会报错，但报得含糊，这里提前说清。
        if _is_within(target, source):
            raise MoveError(
                f'item {index}: cannot move {source.name} into itself: {target}')
        if source.anchor.casefold() != target.anchor.casefold():
            raise MoveError(
                f'item {index}: cross-volume move is not supported '
                f'({source.anchor} -> {target.anchor})')

        if source in seen_sources:
            raise MoveError(f'item {index}: duplicate source: {source}')
        if target in seen_targets:
            raise MoveError(f'item {index}: duplicate target: {target}')
        seen_sources.add(source)
        seen_targets.add(target)
        prepared.append({'from': source, 'to': target})
    return prepared


def _is_within(child: Path, parent: Path) -> bool:
    """child 是否在 parent 目录之内（含相等）。大小写不敏感（Windows 语义）。"""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def common_base(moves: list[dict]) -> Path | None:
    """所有 from/to 的**最长公共父目录**。找不到（跨盘符等）返回 None。

    用来把预览里的路径缩短：六个文件全是
    ``C:\\Users\\...\\Downloads\\report-0.pdf`` 时，屏幕被前缀占满，
    真正的文件名被挤到行尾，人根本核对不了自己批的是什么。
    """
    paths: list[Path] = []
    for item in moves:
        for key in ('from', 'to'):
            value = item.get(key)
            if value is not None:
                paths.append(Path(value))
    if not paths:
        return None
    try:
        parts = [list(path.parts) for path in paths]
    except (OSError, ValueError):
        return None
    if not parts:
        return None
    prefix = parts[0]
    for other in parts[1:]:
        limit = min(len(prefix), len(other))
        index = 0
        while index < limit and prefix[index] == other[index]:
            index += 1
        prefix = prefix[:index]
        if not prefix:
            return None
    base = Path(*prefix) if prefix else None
    # 只在"确实是一个目录"时用相对路径：base 恰好等于某个文件路径时不能用。
    if base is None or not base.is_dir():
        return None
    return base


def describe(moves: list[dict], *, limit: int = 20) -> str:
    """人类可读的移动清单（预览与结果共用）。

    路径能相对化就相对化——预览的目的是让人**核对**，而满屏的绝对路径前缀
    恰好把它变成不可核对。
    """
    base = common_base(moves)

    def _show(value) -> str:
        path = Path(value)
        if base is None:
            return str(path)
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            return str(path)

    lines = []
    if base is not None:
        lines.append(f'  in {base}')
    for item in moves[:limit]:
        lines.append(f"  {_show(item['from'])} -> {_show(item['to'])}")
    if len(moves) > limit:
        lines.append(f'  … {len(moves) - limit} more')
    return '\n'.join(lines)


def move(moves, *, cache_dir, runner=None) -> dict:
    """执行移动并写撤销日志。返回 ``{moved, journal_id}``。

    ``runner`` 仅供测试注入（真实的 ``shutil.move`` 会动磁盘）。
    """
    prepared = validate_moves(moves)
    journal_id = f'{int(time.time())}-{uuid.uuid4().hex[:8]}'
    moved = []

    # 先建目标目录，再逐个移动。目录创建失败也在动手之前暴露。
    for item in prepared:
        item['to'].parent.mkdir(parents=True, exist_ok=True)
    for item in prepared:
        (runner or shutil.move)(str(item['from']), str(item['to']))
        moved.append({'from': str(item['from']), 'to': str(item['to'])})

    _append_journal(cache_dir, journal_id, moved)
    return {'moved': moved, 'journal_id': journal_id}


def _append_journal(cache_dir, journal_id: str, moved: list[dict]) -> None:
    """撤销日志：append-only，一条记录一批移动。"""
    path = _journal_path(cache_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({
                'id': journal_id, 'ts': time.time(), 'moves': moved,
            }, ensure_ascii=False) + '\n')
    except OSError:
        # 写不了日志也要让移动本身成功——但要说出来，否则用户以为能撤销。
        raise MoveError(
            f'moved {len(moved)} item(s) but could not write the undo journal '
            f'({path}); undo will not be available') from None


def recent_batches(cache_dir, limit: int = 10) -> list[dict]:
    """最近的移动批次（新的在前）。"""
    path = _journal_path(cache_dir)
    if not path.is_file():
        return []
    rows = []
    try:
        with path.open('r', encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return list(reversed(rows))[:limit]


def undo(cache_dir, *, batch_id: str = '', runner=None) -> dict:
    """撤销一批移动（默认最近一批）。返回 ``{restored, skipped}``。

    逐项检查：目标还在才移回，源位置已被占用就跳过并报告——**不覆盖**任何
    现有文件。撤销本身也不该造成数据丢失。
    """
    batches = recent_batches(cache_dir, limit=50)
    if not batches:
        raise MoveError(
            'no move batches recorded yet — nothing to undo. '
            'Batches are recorded by move_paths.')
    if batch_id:
        batch = next((row for row in batches if row.get('id') == batch_id), None)
        if batch is None:
            recent = ', '.join(str(row.get('id')) for row in batches[:3])
            raise MoveError(
                f'no move batch with id {batch_id}. '
                f'Recent batches: {recent or "(none)"}')
    else:
        batch = batches[0]

    restored, skipped = [], []
    for item in batch.get('moves') or []:
        source = Path(item['from'])
        target = Path(item['to'])
        if not target.exists() and not target.is_symlink():
            skipped.append({'path': str(target), 'reason': 'moved file is gone'})
            continue
        if source.exists() or source.is_symlink():
            skipped.append({'path': str(source),
                            'reason': 'original location is occupied'})
            continue
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            (runner or shutil.move)(str(target), str(source))
            restored.append({'from': str(target), 'to': str(source)})
        except OSError as exc:
            skipped.append({'path': str(target), 'reason': str(exc)[:120]})
    return {'restored': restored, 'skipped': skipped, 'batch': batch.get('id')}


def available() -> tuple[bool, str]:
    """移动能力是否可用。与 trash 不同，纯 Python 即可，总是可用。"""
    return True, f'{os.name} filesystem'
