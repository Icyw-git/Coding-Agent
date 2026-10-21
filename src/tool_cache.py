"""工具缓存目录（`.cache/tool_results`）的统计与安全回收。

缓存有两类文件：大输出 `<call_id>.txt` 与图片 `images/<sha1>.<ext>`，都被会话
事件引用。回收只删除「超过保留期且没有任何会话再引用」的文件：按事件流原文
匹配文件名，宁可少删也不误删。缺文件时图片会降级成文本（见
`agent_core.expand_image_parts`），但大输出指针会失效，所以默认保留 7 天。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

DEFAULT_RETENTION_DAYS = 7

# 会话事件里保存的是绝对路径；JSON 会把反斜杠写成两个，先折叠再匹配。
_REFERENCE = re.compile(r'tool_results[\\/]+(?:images[\\/]+)?([^\\/"\s]+)')


def _iter_files(root: Path):
    if not root.is_dir():
        return
    for path in root.rglob('*'):
        if path.is_file() and not path.is_symlink():
            yield path


def human_size(size: int) -> str:
    value = float(size)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{int(value)} B'
        value /= 1024
    return f'{size} B'


def scan(root: Path, *, now: float | None = None) -> dict:
    """汇总缓存占用；不跟随符号链接，目录不存在时返回零值。"""
    now = time.time() if now is None else now
    files = images = total = 0
    oldest = newest = None
    for path in _iter_files(Path(root)):
        try:
            stat = path.stat()
        except OSError:
            continue
        files += 1
        total += stat.st_size
        if path.parent.name == 'images':
            images += 1
        oldest = stat.st_mtime if oldest is None else min(oldest, stat.st_mtime)
        newest = stat.st_mtime if newest is None else max(newest, stat.st_mtime)
    return {
        'files': files,
        'images': images,
        'bytes': total,
        'oldest': oldest,
        'newest': newest,
        'age_days': None if oldest is None else max(0, int((now - oldest) / 86400)),
    }


def referenced_names(sessions_dir: Path) -> set[str]:
    """收集事件流里出现过的缓存文件名；整文件文本匹配，避免依赖事件结构。"""
    names: set[str] = set()
    sessions_dir = Path(sessions_dir)
    if not sessions_dir.is_dir():
        return names
    for path in sessions_dir.rglob('*.jsonl'):
        try:
            text = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        names.update(_REFERENCE.findall(text.replace('\\\\', '\\')))
    return names


def plan_cleanup(root: Path, sessions_dir: Path, *,
                 days: int = DEFAULT_RETENTION_DAYS,
                 now: float | None = None) -> dict:
    """列出可删除文件：超过保留期且未被任何会话引用。"""
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    referenced = referenced_names(sessions_dir)
    remove = []
    kept_referenced = kept_recent = 0
    for path in _iter_files(Path(root)):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= cutoff:
            kept_recent += 1
        elif path.name in referenced:
            kept_referenced += 1
        else:
            remove.append((path, stat.st_size))
    return {
        'days': days,
        'remove': remove,
        'bytes': sum(size for _, size in remove),
        'kept_referenced': kept_referenced,
        'kept_recent': kept_recent,
        'referenced': len(referenced),
    }


def apply_cleanup(plan: dict) -> dict:
    """执行回收；单个文件失败只记录，不中断其余删除。"""
    removed = 0
    failed: list[Path] = []
    for path, _ in plan['remove']:
        try:
            path.unlink()
            removed += 1
        except OSError:
            failed.append(path)
    return {'removed': removed, 'failed': failed}


def format_status(root: Path) -> str:
    stats = scan(root)
    if stats['files'] == 0:
        return '  工具缓存为空。'
    return '\n'.join((
        f"  工具缓存：{stats['files']} 个文件（{stats['images']} 张图片），"
        f"共 {human_size(stats['bytes'])}，最早 {stats['age_days']} 天前。",
        '  用 /cache gc [天数] [--apply] 预览或清理过期文件（默认保留 7 天）。',
    ))


def handle_cache_command(root: Path, sessions_dir: Path, argument: str) -> str:
    """/cache 与 /cache gc；默认只预览，加 --apply 才真正删除。"""
    tokens = argument.split()
    if not tokens or tokens[0].lower() != 'gc':
        return format_status(root)
    apply = '--apply' in tokens
    days = DEFAULT_RETENTION_DAYS
    for token in tokens[1:]:
        if token == '--apply':
            continue
        if not token.isdigit() or int(token) < 1:
            return '  用法：/cache gc [天数] [--apply]'
        days = int(token)
    plan = plan_cleanup(root, sessions_dir, days=days)
    lines = [
        f"  超过 {days} 天且未被会话引用：{len(plan['remove'])} 个文件，"
        f"{human_size(plan['bytes'])}；"
        f"保留：近期 {plan['kept_recent']} 个、仍被引用 {plan['kept_referenced']} 个。"
    ]
    if not plan['remove']:
        return '\n'.join(lines + ['  没有可清理的文件。'])
    for path, size in plan['remove'][:10]:
        lines.append(f'  - {path.name} ({human_size(size)})')
    if len(plan['remove']) > 10:
        lines.append(f'  … 另有 {len(plan["remove"]) - 10} 个文件')
    if not apply:
        lines.append('  预览模式，未删除任何文件；加 --apply 执行清理。')
        return '\n'.join(lines)
    result = apply_cleanup(plan)
    lines.append(f"  已清理 {result['removed']} 个文件。")
    if result['failed']:
        lines.append(f"  {len(result['failed'])} 个文件删除失败（可能被占用）。")
    return '\n'.join(lines)
