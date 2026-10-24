"""会话与工作区管理适配层：列表、选择、清理与转录读取。

这里只做「读事件流 + 轻量文件操作」，不持有运行时状态。会话目录、摘要缓存
与 transcript 写入仍归 ``harness`` 全局区，所以统一经 ``_h()`` 在调用期读取；
``monkeypatch.setattr(harness, ...)`` 的既有 patch 点（SESSIONS_DIR /
session_rows / write_transcript 等）继续生效。当前工作区一律走
``get_workdir()``：执行上下文（ContextVar）优先，全局 ``WORKDIR`` 只作回退。
"""
import json
import re
import sys
from pathlib import Path

import workspaces
from session_store import SessionReplayError
from workspace_choices import find_workspace, workspace_choices, workspace_key


def _h():
    """延迟取 harness：模块导入期避免循环，调用期读到最新全局状态。"""
    import harness
    return harness


def list_session_paths(limit: int | None = 20) -> list[Path]:
    """Return logs ordered by conversational activity, not filesystem mtime."""
    h = _h()
    return [h.SESSIONS_DIR / f"session_{row['id']}.jsonl"
            for row in h._session_catalog()][:limit]


def _session_catalog():
    """Reuse summaries until the log changes; timestamps only invalidate cache."""
    h = _h()
    cache = h._session_summary_cache
    paths = list(h.SESSIONS_DIR.glob('session_*.jsonl'))
    live = {path.resolve() for path in paths}
    for path in list(cache):
        if path not in live:
            del cache[path]
    rows = []
    for path in paths:
        key = path.resolve()
        try:
            info = path.stat()
            signature = (info.st_mtime_ns, info.st_size, info.st_ino)
            cached = cache.get(key)
            if cached is None or cached[0] != signature:
                summary = h.SessionEventStore.open(path).session_summary()
                cache[key] = (signature, summary)
                while len(cache) > h._SESSION_SUMMARY_CACHE_MAX:
                    cache.pop(next(iter(cache)))
            rows.append(dict(cache[key][1]))
        except (OSError, SessionReplayError) as exc:
            cache.pop(key, None)
            print(f'  [session] 无法读取 {path.stem}: {exc}')
    return sorted(rows, key=lambda row: (row['updated'], row['id']), reverse=True)


def resolve_session_path(session_id: str) -> Path:
    """Resolve a session ID without accepting arbitrary filesystem paths."""
    if not session_id or not session_id.replace('-', '').replace('_', '').isalnum():
        raise ValueError('Invalid session ID')
    path = _h().SESSIONS_DIR / f'session_{session_id}.jsonl'
    if not path.is_file():
        raise FileNotFoundError(f'Session {session_id} not found')
    return path


def set_workspace(path: str) -> Path:
    """Switch the agent workspace without changing the process-wide cwd.

    CLI 级别的全局切换：写入 ``harness.WORKDIR``，供后续没有执行上下文的
    调用读取；执行中的回合仍由 ``use_workspace`` 的 ContextVar 固定。
    """
    h = _h()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = h.get_workdir() / candidate
    candidate = candidate.resolve()
    if not candidate.is_dir():
        raise ValueError(f'Workspace does not exist: {candidate}')
    h.WORKDIR = candidate
    h.SKILLS_DIR = candidate / 'skills'
    h.scan_skills()   # 项目技能随工作区切换；签名未变时是空操作
    return candidate


def start_new_session(workspace=None, session_id=None):
    """Create a session bound to ``workspace`` (default: the current one).

    工作区先校验再落盘：目录不存在时不会留下一个半成品会话。选定目录会成为
    本进程的当前工作区，并写进会话日志（/resume 恢复时据此切回）。
    """
    h = _h()
    if session_id and (h.SESSIONS_DIR / f'session_{session_id}.jsonl').exists():
        raise FileExistsError(f'already exists: {session_id}; use /r {session_id}')
    previous = h.get_workdir()
    target = previous if workspace is None else h.set_workspace(str(workspace))
    try:
        store = h.SessionEventStore(h.SESSIONS_DIR, session_id=session_id)
        store.append('session_created')
        store.record_workspace(target)
    except Exception:
        if workspace is not None:
            h.set_workspace(str(previous))
        raise
    workspaces.remember_workspace(target, file=h.workspace_registry_path())
    return store


def workspace_registry_path() -> Path:
    return workspaces.default_path(_h().SESSIONS_DIR)


def registered_workspaces() -> list[dict]:
    return workspaces.list_workspaces(file=_h().workspace_registry_path())


def workspace_catalog() -> list[dict]:
    """当前目录 + 登记表 + 最近会话目录：与 API/飞书共用同一合并规则。"""
    h = _h()
    return workspace_choices(h.session_rows(limit=None, include_empty=True), h.get_workdir(),
                             saved=h.registered_workspaces())


def _workspace_for_new_session():
    """选择新会话的工作区；非交互返回当前目录，用户取消返回 None。"""
    h = _h()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return h.get_workdir()
    from cli_input import browse_for_workspace, choose_workspace

    choice = choose_workspace(h.workspace_catalog())
    if choice is None:
        return None
    if choice.get('browse'):
        return browse_for_workspace(h.get_workdir())
    return choice['path']


def format_workspace_list() -> str:
    h = _h()
    entries = h.registered_workspaces()
    if not entries:
        return ('  尚未登记工作区。\n'
                '  /workspace add <路径> [--name 名称] 可登记；/new 也能直接选最近目录。')
    from turn_progress import relative_time
    counts: dict[str, int] = {}
    for row in h.session_rows(limit=None, include_empty=True):
        value = str(row.get('workspace') or '').strip()
        if value:
            key = workspace_key(value)
            counts[key] = counts.get(key, 0) + 1
    current_key = workspace_key(h.get_workdir())
    lines = ['  已登记工作区：']
    for index, entry in enumerate(entries, 1):
        key = workspace_key(entry['path'])
        mark = '（当前）' if key == current_key else ''
        when = (f" · {relative_time(entry.get('last_used_at'))}"
                if entry.get('last_used_at') else '')
        lines.append(f"  {index}. {entry['name']}  {entry['path']}{mark}"
                     f" · {counts.get(key, 0)} 个会话{when}")
    lines.append('  /workspace remove <序号|名称|路径> 移除；/new --ws <序号|名称|路径> 直接使用。')
    return '\n'.join(lines)


def _split_workspace_add(argument: str):
    text = argument.strip()
    name = None
    match = re.search(r'\s+--name\s+(.+)$', text)
    if match:
        name = match.group(1).strip().strip('"\'')
        text = text[:match.start()].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        text = text[1:-1]
    return text, name


def add_workspace_from_argument(argument: str) -> dict:
    """`/workspace add` 的共享实现：解析路径与 `--name`，相对路径按当前工作区。"""
    h = _h()
    path, name = _split_workspace_add(argument)
    if not path:
        raise ValueError('用法：/workspace add <路径> [--name 名称]')
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = h.get_workdir() / candidate
    return workspaces.add_workspace(candidate, name=name,
                                    file=h.workspace_registry_path())


def remove_workspace_selector(selector: str) -> dict:
    """按序号/名称/id/路径移除登记项；只影响发现列表，不动任何文件。"""
    h = _h()
    selector = (selector or '').strip()
    if not selector:
        raise ValueError('用法：/workspace remove <序号|名称|路径>')
    entry = find_workspace(h.registered_workspaces(), selector)
    return workspaces.remove_workspace(entry['id'],
                                       file=h.workspace_registry_path())


def handle_workspace_command(command: str, argument: str) -> None:
    h = _h()
    parts = argument.split(maxsplit=1)
    action = parts[0].lower() if parts else 'list'
    if command == '/workspaces' or action in ('', 'list'):
        print(h.format_workspace_list())
        return
    if action == 'add':
        try:
            entry = h.add_workspace_from_argument(parts[1] if len(parts) > 1 else '')
        except (OSError, ValueError) as exc:
            print(f'  [workspace] {exc}')
            return
        print(f"  已登记工作区：{entry['name']}  {entry['path']}")
        return
    if action == 'remove':
        try:
            removed = h.remove_workspace_selector(parts[1] if len(parts) > 1 else '')
        except (KeyError, OSError, ValueError) as exc:
            print(f'  [workspace] {exc}')
            return
        print(f"  已移除登记：{removed['name']}  {removed['path']}")
        return
    print('  用法：/workspaces；/workspace add <路径> [--name 名称]；'
          '/workspace remove <序号|名称|路径>')


def session_rows(limit: int | None = 100, *, include_empty: bool = False,
                 workspace: Path | None = None, query: str = '') -> list[dict]:
    rows = _h()._session_catalog()
    from cli_input import filter_session_rows
    return filter_session_rows(rows, workspace=workspace, include_empty=include_empty,
                               query=query)[:limit]


def format_session_list(limit: int = 20) -> str:
    h = _h()
    from cli_input import session_label

    rows = h.session_rows(max(100, limit), include_empty=False,
                          workspace=h.get_workdir())[:limit]
    if not rows:
        rows = h.session_rows(max(100, limit), include_empty=False)[:limit]
    if not rows:
        return '  (no saved sessions)'
    return '\n\n'.join(session_label(row) for row in rows)


def session_cleanup_plan(command: str, argument: str, current_id: str, *, emit=print):
    """`/archive`、`/delete` 的候选集合：返回 (SessionArchive, rows)。

    归档候选按天数/empty/all 过滤，删除与恢复只看已归档项；当前会话永远
    排除。确认与执行由 UI 负责，这里只做只读扫描，便于 TUI 放到后台线程。
    """
    h = _h()
    from session_cleanup import SessionArchive, cleanup_candidates

    archive = SessionArchive(h.SESSIONS_DIR, h.CHECKPOINT_DIR)
    deleting = command == 'delete'
    restoring = argument == 'restore'
    if restoring or deleting:
        rows = archive.list(emit=emit)
    else:
        rows = cleanup_candidates(
            h.session_rows(limit=None, include_empty=True), current_id,
            days=int(argument) if argument.isdigit() else 30,
            empty_only=argument == 'empty')
        if argument == 'all':
            rows = h.session_rows(limit=None, include_empty=True)
    return archive, [row for row in rows if row['id'] != current_id]


def apply_session_cleanup(archive, rows, action: str, current_id: str):
    """执行已确认的归档/恢复/删除，返回 (completed, [(row, error), ...])。"""
    completed = 0
    failures = []
    for row in rows:
        try:
            if action == 'delete':
                archive.delete(row, current_id)
            elif action == 'restore':
                archive.restore(row['id'])
            else:
                archive.archive(row, current_id)
            completed += 1
        except (OSError, ValueError) as exc:
            failures.append((row, str(exc)))
    return completed, failures


def export_session(messages) -> Path:
    """把当前会话写出一份 transcript，返回路径（复用 agent_core 的唯一写入实现）。"""
    return _h().write_transcript(messages)


def list_transcripts() -> list[Path]:
    """最近优先的 transcript 列表；目录不存在时返回空列表。"""
    try:
        return sorted(_h().TRANSCRIPT_DIR.glob('transcript_*.jsonl'),
                      key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return []


def load_transcript(path) -> list[dict]:
    """读回一份 transcript；坏行跳过，不因一行损坏丢掉整场会话。"""
    records = []
    try:
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return records
