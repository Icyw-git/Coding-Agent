"""Agent 存储 profile：区分源数据、派生缓存和未识别目录。

清理策略遵循「先证明可删，再删除」：会话事件流、记忆和任务状态是恢复源，
不自动清理；工具缓存按「过期 + 未被引用」回收；checkpoint 按数量保留；
其他目录只报告、不猜用途。Codex/Pi 的共同点也是把历史保留与派生数据分开，
删除会话走显式 archive/delete，而不是启动时大扫除。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import checkpoints
import tool_cache

DEFAULT_TMP_DAYS = 7

# key, label, kind, action
_CATEGORIES = (
    ('sessions', '会话事件流', 'source', '保留；用 /archive 或 /delete 管理'),
    ('memories', '长期记忆', 'source', '保留；由记忆工具维护'),
    ('tasks', '任务状态', 'source', '保留；由任务系统维护'),
    ('os_tasks', '后台任务状态', 'source', '保留；用 /watch 查看'),
    ('transcripts', '导出的 transcript', 'export', '保留；需要时手动删除'),
    ('tool_results', '工具大输出/图片', 'derived', '/cache gc [天数] [--apply]'),
    ('checkpoints', '回溯快照', 'derived', '/checkpoint-gc [--all] [--keep N]'),
    ('tmp', '临时缓存', 'temp', '可按天数预览后手动删除'),
    ('api', 'API 访问日志', 'diagnostic', '保留审计；按需轮转'),
    ('sandbox-tmp', '沙箱临时目录', 'temp', '通常可手动清理'),
)
_KNOWN = {name for name, *_ in _CATEGORIES} | {'cache_debug.jsonl'}
_ROOT_STATE = {
    'crons.json': ('定时任务状态', 'source', '保留；由定时任务系统维护'),
    'feishu_bindings.json': ('飞书绑定状态', 'source', '保留；由网关维护'),
    'mcp_servers.json': ('MCP 服务配置', 'source', '保留；由 MCP 配置维护'),
}
_KNOWN |= set(_ROOT_STATE)


def _stats(path: Path, *, now: float | None = None) -> dict:
    """统计目录（或单文件）大小，跳过符号链接。"""
    now = time.time() if now is None else now
    path = Path(path)
    files = 0
    total = 0
    oldest = newest = None
    if path.is_file() and not path.is_symlink():
        try:
            stat = path.stat()
        except OSError:
            return {'files': 0, 'bytes': 0, 'oldest': None, 'newest': None,
                    'age_days': None}
        return {'files': 1, 'bytes': stat.st_size,
                'oldest': stat.st_mtime, 'newest': stat.st_mtime,
                'age_days': max(0, int((now - stat.st_mtime) / 86400))}
    if not path.is_dir() or path.is_symlink():
        return {'files': 0, 'bytes': 0, 'oldest': None, 'newest': None,
                'age_days': None}
    for root, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs
                   if not (Path(root) / name).is_symlink()]
        for name in names:
            item = Path(root) / name
            if item.is_symlink():
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            files += 1
            total += stat.st_size
            oldest = stat.st_mtime if oldest is None else min(oldest, stat.st_mtime)
            newest = stat.st_mtime if newest is None else max(newest, stat.st_mtime)
    return {
        'files': files, 'bytes': total, 'oldest': oldest, 'newest': newest,
        'age_days': None if oldest is None else max(0, int((now - oldest) / 86400)),
    }


def _old_files(path: Path, days: int, *, now: float | None = None) -> dict:
    """统计过期文件（不删除）；用于 tmp 等只做提示的目录。"""
    now = time.time() if now is None else now
    cutoff = now - max(0, int(days)) * 86400
    files = 0
    total = 0
    path = Path(path)
    if not path.is_dir():
        return {'files': 0, 'bytes': 0}
    for root, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs
                   if not (Path(root) / name).is_symlink()]
        for name in names:
            item = Path(root) / name
            if item.is_symlink():
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff:
                files += 1
                total += stat.st_size
    return {'files': files, 'bytes': total}


def scan(root, *, sessions_dir=None, days: int = DEFAULT_TMP_DAYS,
         checkpoint_keep: int = 20, deep: bool = True,
         now: float | None = None) -> dict:
    """生成存储 profile；deep=False 只统计大小，不扫描引用/快照计划。"""
    root = Path(root)
    sessions_dir = Path(sessions_dir) if sessions_dir else root / 'sessions'
    entries = []
    for key, label, kind, action in _CATEGORIES:
        path = root / key
        stats = _stats(path, now=now)
        entry = {
            'key': key, 'label': label, 'kind': kind, 'action': action,
            'path': path, 'days': days, **stats,
        }
        if key == 'tool_results' and deep and path.is_dir():
            plan = tool_cache.plan_cleanup(path, sessions_dir, days=days, now=now)
            entry['reclaimable'] = plan['bytes']
            entry['reclaimable_files'] = len(plan['remove'])
            entry['kept_referenced'] = plan['kept_referenced']
        elif key == 'checkpoints' and deep and path.is_dir():
            entry['keep'] = checkpoint_keep
            summary = checkpoints.gc_all_checkpoints(
                path, max_nodes=checkpoint_keep, dry_run=True)
            entry['nodes'] = summary['nodes']
            entry['reclaimable'] = summary['bytes']
            entry['reclaimable_files'] = summary['removed']
            entry['checkpoint_details'] = summary['details']
        elif key == 'tmp' and path.is_dir():
            old = _old_files(path, days, now=now)
            entry['reclaimable'] = old['bytes']
            entry['reclaimable_files'] = old['files']
        entries.append(entry)

    for name, (label, kind, action) in _ROOT_STATE.items():
        path = root / name
        if path.exists():
            entries.append({
                'key': name, 'label': label, 'kind': kind,
                'action': action, 'path': path, **_stats(path, now=now),
            })

    debug = root / 'cache_debug.jsonl'
    if debug.exists():
        entries.append({
            'key': 'cache_debug.jsonl', 'label': '缓存前缀诊断',
            'kind': 'diagnostic', 'action': '保留诊断；可手动删除',
            'path': debug, **_stats(debug, now=now),
        })

    unmanaged = []
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if child.name in _KNOWN or child.name.startswith('.'):
                continue
            stats = _stats(child, now=now)
            if stats['files'] or stats['bytes']:
                unmanaged.append({
                    'key': child.name, 'path': child,
                    'kind': 'unmanaged', **stats,
                })

    known_bytes = sum(entry['bytes'] for entry in entries)
    reclaimable = sum(entry.get('reclaimable', 0) for entry in entries)
    unmanaged_bytes = sum(entry['bytes'] for entry in unmanaged)
    return {
        'root': root, 'entries': entries, 'unmanaged': unmanaged,
        'known_bytes': known_bytes, 'unmanaged_bytes': unmanaged_bytes,
        'reclaimable_bytes': reclaimable,
        'days': days, 'checkpoint_keep': checkpoint_keep,
        'deep': deep,
    }


def _size(value: int) -> str:
    return tool_cache.human_size(value)


def _entry_line(entry: dict) -> str:
    bits = [f"{entry['label']}: {_size(entry['bytes'])}"]
    if entry.get('files'):
        bits.append(f"{entry['files']} 个文件")
    if entry.get('nodes') is not None:
        bits.append(f"{entry['nodes']} 个节点")
        if entry.get('keep') is not None:
            bits.append(f"每会话保留 {entry['keep']}")
    if entry.get('age_days') is not None:
        bits.append(f"最早 {entry['age_days']} 天前")
    reclaimable = entry.get('reclaimable', 0)
    if reclaimable:
        count = entry.get('reclaimable_files', 0)
        bits.append(f"可回收 {_size(reclaimable)}" + (f" / {count} 项" if count else ''))
    if entry.get('kept_referenced'):
        bits.append(f"引用保护 {entry['kept_referenced']} 个")
    return '  ' + ' · '.join(bits) + f"  → {entry['action']}"


def format_profile(profile: dict, *, limit: int = 12) -> str:
    lines = [
        f"  存储 profile：已知 {_size(profile['known_bytes'])}，"
        f"可回收约 {_size(profile['reclaimable_bytes'])}；"
        f"未识别 {_size(profile['unmanaged_bytes'])}（不处理）。",
    ]
    groups = (
        ('source', '源数据（不自动删除）'),
        ('export', '导出文件（保留）'),
        ('derived', '可回收派生缓存'),
        ('temp', '临时文件'),
        ('diagnostic', '诊断日志'),
    )
    for kind, title in groups:
        rows = [entry for entry in profile['entries']
                if entry['kind'] == kind and (entry.get('files') or entry.get('nodes'))]
        if not rows:
            continue
        lines.append(f"  {title}")
        for entry in rows[:limit]:
            lines.append(_entry_line(entry))
    if profile['unmanaged']:
        lines.append('  未识别目录（只报告，不自动清理）')
        for entry in profile['unmanaged'][:limit]:
            lines.append(
                f"  {entry['key']}: {_size(entry['bytes'])} · "
                f"{entry['files']} 个文件 · 请确认用途后手动处理")
    lines.append('  清理命令：/cache gc、/checkpoint-gc、/archive、/delete。')
    return '\n'.join(lines)


def format_summary(profile: dict) -> str:
    """--doctor 用的短摘要，避免把完整 profile 塞进启动诊断。"""
    return (
        f"[storage] known {_size(profile['known_bytes'])}, "
        f"reclaimable ~{_size(profile['reclaimable_bytes'])}, "
        f"unmanaged {_size(profile['unmanaged_bytes'])}; use /cache profile"
    )
