"""技能系统：内建技能与当前工作区技能合并扫描 / 列表 / 加载。

项目技能与内建技能同名时覆盖内建技能；扫描按 manifest 的 mtime/size
做签名，工作区切换或技能文件被编辑后自动重扫。
"""
import os
from pathlib import Path

from agent_core import _parse_frontmatter


def _h():
    import harness
    return harness


SKILL_REGISTRY: dict[str, dict] = {}
_last_signature = None


def _skill_dirs() -> list[Path]:
    directories = []
    seen = set()
    for value in (getattr(_h(), 'BUILTIN_SKILLS_DIR', None),
                  getattr(_h(), 'SKILLS_DIR', None)):
        if value is None:
            continue
        directory = Path(value)
        try:
            key = os.path.normcase(str(directory.resolve()))
        except OSError:
            key = os.path.normcase(str(directory))
        if key in seen:
            continue
        seen.add(key)
        directories.append(directory)
    return directories


def _signature(directories: list[Path]):
    rows = []
    for directory in directories:
        try:
            rows.append(('dir', os.path.normcase(str(directory.resolve()))))
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            manifest = entry / 'skill.md'
            try:
                if not entry.is_dir() or not manifest.is_file():
                    continue
                stat = manifest.stat()
            except OSError:
                continue
            rows.append((str(manifest), stat.st_mtime_ns, stat.st_size))
    return tuple(rows)


def scan_skills(force: bool = False) -> None:
    global _last_signature
    directories = _skill_dirs()
    signature = _signature(directories)
    if not force and signature == _last_signature:
        return
    registry: dict[str, dict] = {}
    for directory in directories:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            manifest = entry / 'skill.md'
            try:
                if not entry.is_dir() or not manifest.is_file():
                    continue
                raw = manifest.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            meta, body = _parse_frontmatter(raw)
            name = meta.get('name', entry.name)
            description = meta.get(
                'description', raw.split('\n')[0].lstrip('#').strip())
            registry[name] = {
                'name': name,
                'description': description,
                'body': body,
            }
    SKILL_REGISTRY.clear()
    SKILL_REGISTRY.update(registry)
    _last_signature = signature


def list_skills() -> str:
    scan_skills()
    if not SKILL_REGISTRY:
        return "No skills registered"
    return '\n'.join(
        f'- **{skill["name"]}**: {skill["description"]}'
        for skill in SKILL_REGISTRY.values())


def load_skill(name: str) -> str:
    scan_skills()
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Error: skill {name} not found"
    return skill['body']
