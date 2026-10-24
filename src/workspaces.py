"""Named workspace registry: discoverability only, never authorization.

The registry lives next to the session logs (``.cache/workspaces.json`` by
default). That directory is writable by the agent, so an entry here must never
widen the allowed roots — otherwise the agent could register any path and
self-authorize it. Authorization stays in startup configuration
(``HARNESS_WORKSPACE`` / ``HARNESS_WORKSPACE_ROOTS``); this module only stores
names so the picker can list places the user has already chosen.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from workspace_choices import workspace_key

VERSION = 1


def default_path(sessions_dir=None) -> Path:
    """Registry file: env override, else next to the session directory."""
    override = os.getenv('HARNESS_WORKSPACE_FILE')
    if override and override.strip():
        return Path(override).expanduser()
    if sessions_dir:
        return Path(sessions_dir).expanduser().parent / 'workspaces.json'
    import paths
    return paths.cache_path('workspaces.json')


def workspace_id(path) -> str:
    return hashlib.sha1(workspace_key(path).encode('utf-8')).hexdigest()[:10]


def _clean(entry) -> dict | None:
    if not isinstance(entry, dict):
        return None
    path = str(entry.get('path') or '').strip()
    if not path:
        return None
    name = str(entry.get('name') or '').strip() or Path(path).name or path
    try:
        added_at = float(entry.get('added_at') or 0)
    except (TypeError, ValueError):
        added_at = 0.0
    try:
        last_used_at = float(entry.get('last_used_at') or added_at)
    except (TypeError, ValueError):
        last_used_at = added_at
    return {
        'id': str(entry.get('id') or workspace_id(path)),
        'name': name,
        'path': path,
        'added_at': added_at,
        'last_used_at': last_used_at,
    }


def _read(file: Path) -> list[dict]:
    try:
        document = json.loads(file.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    entries = document.get('workspaces') if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return []
    cleaned = [item for item in (_clean(entry) for entry in entries) if item]
    # A hand-edited/duplicated file must not show the same directory twice;
    # keep the most recently used copy.
    unique: dict[str, dict] = {}
    for entry in cleaned:
        key = workspace_key(entry['path'])
        previous = unique.get(key)
        if previous is None or entry['last_used_at'] >= previous['last_used_at']:
            unique[key] = entry
    return list(unique.values())


def _write(file: Path, entries: list[dict]) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({'version': VERSION, 'workspaces': entries},
                         ensure_ascii=False, indent=2)
    temp = file.with_name(f'{file.name}.{os.getpid()}.tmp')
    temp.write_text(payload, encoding='utf-8')
    temp.replace(file)


def list_workspaces(file=None, sessions_dir=None) -> list[dict]:
    """Registered workspaces, most recently used first."""
    file = Path(file) if file is not None else default_path(sessions_dir)
    return sorted(_read(file),
                  key=lambda entry: (entry['last_used_at'], entry['name'].casefold()),
                  reverse=True)


def add_workspace(path, name=None, *, file=None, sessions_dir=None) -> dict:
    """Register (or refresh) a directory and return its entry."""
    file = Path(file) if file is not None else default_path(sessions_dir)
    directory = Path(path).expanduser()
    if not directory.is_dir():
        raise ValueError(f'Workspace does not exist: {directory}')
    directory = directory.resolve()
    entries = _read(file)
    key = workspace_key(directory)
    now = time.time()
    for entry in entries:
        if workspace_key(entry['path']) == key:
            entry['path'] = str(directory)
            entry['last_used_at'] = now
            if name and str(name).strip():
                entry['name'] = str(name).strip()
            _write(file, entries)
            return dict(entry)
    entry = {
        'id': workspace_id(directory),
        'name': (str(name).strip() if name and str(name).strip()
                 else directory.name or str(directory)),
        'path': str(directory),
        'added_at': now,
        'last_used_at': now,
    }
    entries.append(entry)
    _write(file, entries)
    return dict(entry)


def remove_workspace(selector, *, file=None, sessions_dir=None) -> dict:
    """Remove by id/name/path; raise KeyError when nothing matches."""
    file = Path(file) if file is not None else default_path(sessions_dir)
    entries = _read(file)
    wanted = str(selector).strip()
    if len(wanted) >= 2 and wanted[0] == wanted[-1] and wanted[0] in ('"', "'"):
        wanted = wanted[1:-1]
    matches = [entry for entry in entries if entry['id'] == wanted]
    if not matches:
        key = workspace_key(wanted)
        matches = [entry for entry in entries if workspace_key(entry['path']) == key]
    if not matches:
        folded = wanted.casefold()
        matches = [entry for entry in entries if entry['name'].casefold() == folded]
    if not matches:
        raise KeyError(wanted)
    if len(matches) > 1:
        raise ValueError(f'多个工作区匹配 {wanted!r}，请改用 id')
    removed = matches[0]
    _write(file, [entry for entry in entries if entry['id'] != removed['id']])
    return removed


def remember_workspace(path, *, file=None, sessions_dir=None) -> dict | None:
    """Best-effort registration when a session starts; never block creation."""
    try:
        return add_workspace(path, file=file, sessions_dir=sessions_dir)
    except (OSError, ValueError):
        return None
