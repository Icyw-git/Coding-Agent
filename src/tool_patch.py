"""Codex 风格 apply_patch：解析、规划与原子写盘。

纯逻辑模块：路径解析、写入前 checkpoint 捕获与无头审批门由 tools.py 注入
（`resolve` / `capture` / `gate`），本模块不读取工作区全局状态，便于单独测试。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from tool_text import encode_text, read_text_file

PATCH_BEGIN = '*** Begin Patch'
PATCH_END = '*** End Patch'
_PATCH_FILE_HEADER = re.compile(
    r'^\*\*\* (Add|Update|Delete) File:\s*(.+?)\s*$', re.IGNORECASE)
_PATCH_MOVE_HEADER = re.compile(r'^\*\*\* Move to:\s*(.+?)\s*$')


class PatchError(ValueError):
    pass


def _parse_hunk(raw: dict, path: str, number: int) -> dict:
    old, new = [], []
    for line in raw['lines']:
        if line == '':
            old.append('')
            new.append('')
        elif line[0] == ' ':
            old.append(line[1:])
            new.append(line[1:])
        elif line[0] == '-':
            old.append(line[1:])
        elif line[0] == '+':
            new.append(line[1:])
    if not old and raw['hint'] is None:
        raise PatchError(
            f'{path}: hunk {number} needs context lines or a @@ anchor')
    return {'hint': raw['hint'], 'old': old, 'new': new}


def parse_patch(patch: str) -> list[dict]:
    """解析 Codex 风格的 *** Begin Patch 文本，返回文件操作列表。

    只做语法解析，不读盘、不判定路径；策略层用 patch_paths 取路径，
    handler 用本函数的结果做两阶段（先规划、后写盘）应用。
    """
    if not isinstance(patch, str) or not patch.strip():
        raise PatchError('empty patch')
    lines = patch.splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or lines[index].strip() != PATCH_BEGIN:
        raise PatchError(f"patch must start with '{PATCH_BEGIN}'")
    index += 1
    ops = []
    seen_end = False
    while index < len(lines):
        raw = lines[index]
        if raw.strip() == PATCH_END:
            seen_end = True
            index += 1
            break
        if not raw.strip():
            index += 1
            continue
        match = _PATCH_FILE_HEADER.match(raw)
        if not match:
            raise PatchError(
                f'line {index + 1}: expected file header, got {raw[:60]!r}')
        kind = match.group(1).lower().split()[0]
        path = match.group(2)
        index += 1
        move_to = None
        if kind == 'update':
            while index < len(lines) and not lines[index].strip():
                index += 1
            if index < len(lines) and lines[index].startswith('*** Move to:'):
                move_match = _PATCH_MOVE_HEADER.match(lines[index])
                if not move_match:
                    raise PatchError(f'line {index + 1}: invalid move header')
                move_to = move_match.group(1)
                index += 1
        if kind == 'delete':
            ops.append({'kind': 'delete', 'path': path})
            continue
        if kind == 'add':
            content = []
            while index < len(lines) and not lines[index].startswith('*** '):
                line = lines[index]
                if line.startswith('\\'):
                    index += 1
                    continue
                if line.startswith('+'):
                    content.append(line[1:])
                elif line == '':
                    content.append('')
                else:
                    raise PatchError(
                        f'line {index + 1}: added lines must start with "+"')
                index += 1
            if not content:
                raise PatchError(f'add file {path!r} has no content')
            text = '\n'.join(content)
            ops.append({'kind': 'add', 'path': path,
                        'content': text if text.endswith('\n') else text + '\n'})
            continue
        hunks = []
        current = {'hint': None, 'lines': []}
        while index < len(lines) and not lines[index].startswith('*** '):
            line = lines[index]
            if line.startswith('@@'):
                if current['hint'] is not None or current['lines']:
                    hunks.append(current)
                current = {'hint': line[2:].strip() or None, 'lines': []}
            elif line.startswith('\\'):
                pass
            elif line.startswith((' ', '+', '-')) or line == '':
                current['lines'].append(line)
            else:
                raise PatchError(
                    f'line {index + 1}: hunk lines must start with '
                    '" ", "+" or "-"')
            index += 1
        if current['hint'] is not None or current['lines']:
            hunks.append(current)
        if not hunks and move_to is None:
            raise PatchError(f'update file {path!r} has no hunks')
        ops.append({'kind': 'update', 'path': path, 'move_to': move_to,
                    'hunks': [_parse_hunk(hunk, path, number)
                              for number, hunk in enumerate(hunks, 1)]})
    if not seen_end:
        raise PatchError(f"missing '{PATCH_END}'")
    if any(line.strip() for line in lines[index:]):
        raise PatchError(f'content after {PATCH_END}')
    if not ops:
        raise PatchError('patch affects no files')
    return ops


def patch_paths(patch: str) -> list[str]:
    """补丁涉及的全部路径（含 move 目标），供策略层做整批路径判定。"""
    paths = []
    for op in parse_patch(patch):
        paths.append(op['path'])
        if op.get('move_to'):
            paths.append(op['move_to'])
    return paths


def _apply_hunks(text: str, hunks: list[dict], path: str) -> str:
    trailing_newline = text.endswith('\n')
    lines = text.splitlines()
    cursor = 0
    for number, hunk in enumerate(hunks, 1):
        old, new, hint = hunk['old'], hunk['new'], hunk['hint']
        if hint is not None:
            anchors = [i + 1 for i in range(cursor, len(lines))
                       if lines[i] == hint or hint in lines[i]]
            if not anchors:
                raise PatchError(
                    f'{path}: hunk {number}: anchor not found: {hint!r}')
            if old:
                matches = [anchor for anchor in anchors
                           if lines[anchor:anchor + len(old)] == old]
                if not matches:
                    raise PatchError(
                        f'{path}: hunk {number}: context not found after anchor')
                if len(matches) > 1:
                    raise PatchError(
                        f'{path}: hunk {number}: context is ambiguous; '
                        'add more context lines')
                index = matches[0]
            else:
                if len(anchors) > 1:
                    raise PatchError(
                        f'{path}: hunk {number}: anchor is ambiguous')
                index = anchors[0]
        else:
            matches = [i for i in range(cursor, len(lines) - len(old) + 1)
                       if lines[i:i + len(old)] == old]
            if not matches:
                raise PatchError(f'{path}: hunk {number}: context not found')
            if len(matches) > 1:
                raise PatchError(
                    f'{path}: hunk {number}: context is ambiguous; '
                    'add more context lines')
            index = matches[0]
        lines[index:index + len(old)] = new
        cursor = index + len(new)
    result = '\n'.join(lines)
    return result + '\n' if trailing_newline else result


def _plan_patch(ops: list[dict], cwd: str | None, resolve) -> list[dict]:
    plans = []
    touched = set()

    def reserve(path: Path, what: str) -> None:
        key = os.path.normcase(str(path))
        if key in touched:
            raise PatchError(f'{what} touches the same file twice: {path}')
        touched.add(key)

    for op in ops:
        source = resolve(op['path'], cwd)
        if op['kind'] == 'add':
            reserve(source, 'add')
            if source.exists():
                raise PatchError(f'add file already exists: {source}')
            plans.append({'kind': 'write', 'path': source,
                          'data': encode_text(op['content'], 'utf-8', '\n')})
            continue
        if not source.is_file():
            raise PatchError(f'{op["kind"]} file not found: {source}')
        text, encoding, newline = read_text_file(source)
        if op['kind'] == 'delete':
            reserve(source, 'delete')
            plans.append({'kind': 'delete', 'path': source})
            continue
        reserve(source, 'update')
        updated = _apply_hunks(text, op['hunks'], str(source))
        data = encode_text(updated, encoding, newline)
        move_to = op.get('move_to')
        if not move_to:
            plans.append({'kind': 'write', 'path': source, 'data': data})
            continue
        dest = resolve(move_to, cwd)
        reserve(dest, 'move target')
        if dest.exists():
            raise PatchError(f'move target already exists: {dest}')
        plans.append({'kind': 'move', 'path': source, 'dest': dest, 'data': data})
    return plans


def _apply_plans(plans: list[dict], capture) -> None:
    """写盘并保留回滚副本：任何一个文件失败，已改文件恢复原样。"""
    backups = []
    seen = set()
    for plan in plans:
        paths = ([plan['path'], plan['dest']]
                 if plan['kind'] == 'move' else [plan['path']])
        for path in paths:
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            capture(path)
            backups.append((path, path.read_bytes() if path.is_file() else None))
    try:
        for plan in plans:
            if plan['kind'] == 'delete':
                plan['path'].unlink()
            elif plan['kind'] == 'write':
                plan['path'].parent.mkdir(parents=True, exist_ok=True)
                plan['path'].write_bytes(plan['data'])
            else:
                plan['dest'].parent.mkdir(parents=True, exist_ok=True)
                plan['dest'].write_bytes(plan['data'])
                plan['path'].unlink()
    except Exception:
        for path, old in reversed(backups):
            try:
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(old)
            except OSError:
                pass
        raise


def apply_patch(patch: str, cwd: str | None, *,
                resolve, capture, gate) -> str:
    """解析 → 审批 → 规划 → 写盘；`gate('apply_patch', args)` 返回拒绝文本或空。"""
    try:
        ops = parse_patch(patch)
    except PatchError as exc:
        return f'Error: invalid patch: {exc}'
    gate_args = {'patch': patch}
    if cwd:
        gate_args['cwd'] = cwd
    denied = gate('apply_patch', gate_args)
    if denied:
        return denied
    try:
        plans = _plan_patch(ops, cwd, resolve)
        _apply_plans(plans, capture)
    except PatchError as exc:
        return f'Error: patch not applied: {exc}'
    except Exception as exc:
        return f'Error: patch not applied: {exc}'
    updates = sum(1 for plan in plans if plan['kind'] == 'write')
    adds = sum(1 for op in ops if op['kind'] == 'add')
    deletes = sum(1 for op in ops if op['kind'] == 'delete')
    moves = sum(1 for plan in plans if plan['kind'] == 'move')
    return (f'Applied patch: {updates} updated, {adds} added, '
            f'{deletes} deleted, {moves} moved')
