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


def workspace_choices(rows, current, limit: int = RECENT_LIMIT) -> list[dict]:
    """当前目录 + 最近使用的工作区。

    rows 是 session_rows() 的摘要（含 workspace / updated）；current 是当前
    工作区。返回项：{path, name, sessions, last_active, current}。没有
    workspace 的旧会话不参与候选——它们无法代表一个可切换的目录。
    """
    counts: dict[str, int] = {}
    latest: dict[str, float] = {}
    paths: dict[str, str] = {}
    for row in rows:
        value = str(row.get('workspace') or '').strip()
        if not value:
            continue
        key = workspace_key(value)
        counts[key] = counts.get(key, 0) + 1
        latest[key] = max(latest.get(key, 0.0), float(row.get('updated') or 0))
        paths.setdefault(key, value)

    current_path = Path(current).expanduser()
    current_key = workspace_key(current_path)
    choices = [{
        'path': str(current_path),
        'name': current_path.name or str(current_path),
        'sessions': counts.get(current_key, 0),
        'last_active': latest.get(current_key),
        'current': True,
    }]
    recent = sorted(
        (key for key in counts if key != current_key),
        key=lambda key: latest.get(key, 0.0), reverse=True)[:limit]
    for key in recent:
        path = paths.get(key) or key
        choices.append({
            'path': path,
            'name': Path(path).name or path,
            'sessions': counts[key],
            'last_active': latest.get(key),
            'current': False,
        })
    return choices


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
