"""新建会话的工作区候选：从本机会话摘要推导，用户只选不输入。

Codex 的会话在创建时绑定目录；这里的候选列表让「换目录开新会话」不需要
记忆和输入长路径——当前目录排第一，其次是最近用过的目录（按最近活跃排序，
同一个目录的会话数作为权重提示）。
"""
import os
import string
from pathlib import Path

# 最近目录的上限：再长就靠「浏览其他目录…」兜底，列表本身保持一屏可读。
RECENT_LIMIT = 6


def workspace_key(path: str | Path) -> str:
    """路径去重键：Windows 大小写不敏感，相对路径按当前目录展开。"""
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _exists(path) -> bool:
    try:
        return Path(path).expanduser().is_dir()
    except OSError:
        return False


def _allowed(path, roots) -> bool:
    """登记项是否落在启动授权的根内；未配置根时本地 CLI 不做额外限制。"""
    if not roots:
        return True
    try:
        candidate = Path(path).expanduser().resolve()
    except OSError:
        return False
    for root in roots:
        try:
            if candidate.is_relative_to(Path(root).expanduser().resolve()):
                return True
        except OSError:
            continue
    return False


def workspace_choices(rows, current=None, limit: int | None = RECENT_LIMIT,
                      saved=None, allowed_roots=None) -> list[dict]:
    """当前目录 + 已登记目录 + 最近使用目录。

    rows 是 session_rows() 的摘要（含 workspace / updated），current 是当前
    工作区，saved 是 workspaces.json 里的登记项。三者按规范化路径去重，
    非当前项按最近使用时间排序并截断。返回项额外带 saved/exists/allowed，
    供 UI 标注「已登记」「目录不存在」「未授权」。
    """
    counts: dict[str, int] = {}
    latest: dict[str, float] = {}
    paths: dict[str, str] = {}
    saved_latest: dict[str, float] = {}
    saved_names: dict[str, str] = {}
    saved_ids: dict[str, str] = {}
    for row in rows:
        value = str(row.get('workspace') or '').strip()
        if not value:
            continue
        key = workspace_key(value)
        counts[key] = counts.get(key, 0) + 1
        latest[key] = max(latest.get(key, 0.0), float(row.get('updated') or 0))
        paths.setdefault(key, value)
    for entry in saved or ():
        value = str(entry.get('path') or '').strip()
        if not value:
            continue
        key = workspace_key(value)
        paths.setdefault(key, value)
        try:
            saved_latest[key] = max(saved_latest.get(key, 0.0),
                                    float(entry.get('last_used_at') or 0))
        except (TypeError, ValueError):
            saved_latest[key] = saved_latest.get(key, 0.0)
        name = str(entry.get('name') or '').strip()
        if name:
            saved_names.setdefault(key, name)
        if entry.get('id'):
            saved_ids.setdefault(key, str(entry['id']))

    def activity(key):
        value = max(latest.get(key, 0.0), saved_latest.get(key, 0.0))
        return value or None

    current_path = Path(current).expanduser() if current else None
    current_key = workspace_key(current_path) if current_path is not None else None
    choices = []
    if current_path is not None:
        name = (saved_names.get(current_key)
                or (current_path.name or str(current_path)))
        choices.append({
            'id': saved_ids.get(current_key),
            'path': str(current_path),
            'name': name,
            'sessions': counts.get(current_key, 0),
            'last_active': activity(current_key),
            'current': True,
            'saved': current_key in saved_names or current_key in saved_latest,
            'exists': _exists(current_path),
            'allowed': _allowed(current_path, allowed_roots),
        })

    candidates = sorted(
        (key for key in paths if key != current_key),
        key=lambda key: activity(key) or 0.0, reverse=True)
    if limit is not None:
        candidates = candidates[:limit]
    for key in candidates:
        path = paths.get(key) or key
        choices.append({
            'id': saved_ids.get(key),
            'path': path,
            'name': saved_names.get(key) or Path(path).name or path,
            'sessions': counts.get(key, 0),
            'last_active': activity(key),
            'current': False,
            'saved': key in saved_names or key in saved_latest,
            'exists': _exists(path),
            'allowed': _allowed(path, allowed_roots),
        })
    return choices


def find_workspace(rows, selector) -> dict:
    """按序号/名称/id/路径在候选列表中定位一项，找不到或有歧义时抛错。"""
    value = str(selector).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    if not value:
        raise ValueError('请提供工作区序号、名称或路径')
    if value.isdigit():
        index = int(value) - 1
        if not 0 <= index < len(rows):
            raise ValueError(f'序号超出范围：{value}（共 {len(rows)} 个）')
        return rows[index]
    key = workspace_key(value)
    matches = [row for row in rows if workspace_key(row['path']) == key]
    if not matches:
        folded = value.casefold()
        matches = [row for row in rows
                   if str(row.get('name') or '').casefold() == folded
                   or str(row.get('id') or '').casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f'多个工作区匹配 {value!r}，请改用序号')
    raise ValueError(f'未找到工作区：{value}')


def system_drives() -> list[Path]:
    """Windows 的盘符列表；其它系统返回空（从根目录逐级浏览即可）。"""
    if os.name != 'nt':
        return []
    drives = []
    for letter in string.ascii_uppercase:
        root = Path(f'{letter}:\\')
        try:
            if root.exists():
                drives.append(root)
        except OSError:
            continue
    return drives


def browse_entries(path, drives=None) -> list[dict]:
    """文件夹浏览器的行：子目录、上级、盘符（「进入此文件夹」由 UI 置顶）。

    顺序刻意让子目录紧跟置顶的「进入当前文件夹」：Enter 进入子目录后光标
    回到置顶行，再按一次 Enter 就是进入这个目录并开始会话。
    """
    current = Path(path).expanduser()
    try:
        current = current.resolve()
    except OSError:
        pass
    try:
        children = sorted((item for item in current.iterdir() if item.is_dir()),
                          key=lambda item: item.name.casefold())
    except OSError:
        children = []
    rows = [{'kind': 'dir', 'name': item.name, 'path': str(item)}
            for item in children]
    parent = current.parent
    if parent != current:
        rows.append({'kind': 'parent', 'name': '上级目录', 'path': str(parent)})
    rows.extend({'kind': 'drive', 'name': f'{root}', 'path': str(root)}
                for root in (system_drives() if drives is None else drives))
    return rows


def workspace_label(choice: dict) -> str:
    """列表行文案：目录名 + 会话数；路径由 UI 单独对齐显示。"""
    if choice.get('current'):
        return f"当前目录（{choice['sessions']} 个会话）"
    return f"{choice['name']}（{choice['sessions']} 个会话）"
