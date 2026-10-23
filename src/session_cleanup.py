"""Recoverable session archival. Never touches workspace files."""

import shutil
import stat
import time
from pathlib import Path

from session_store import SessionEventStore

_CHILD_DIRS = ('subagents', 'teammates')
_STARTED_EVENTS = ('subagent_started', 'teammate_started')


def _is_link(path):
    """Symlink 或 Windows junction/reparse point；不存在的路径不算链接。"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return path.is_symlink() or bool(
        getattr(info, 'st_file_attributes', 0)
        & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def cleanup_candidates(rows, current_id, days=30, empty_only=False, now=None):
    cutoff = (time.time() if now is None else now) - days * 86400
    return [row for row in rows if row['id'] != current_id
            and (row['preview'] == '尚无任务' if empty_only else row['updated'] < cutoff)]


def orphan_child_logs(sessions_dir, days=30, now=None, *, emit=print):
    """未被任何顶层会话引用、且超过保留期的子代理/teammate 日志。

    任何父日志读不出来就整体放弃——宁可漏删也不误删；保留期按文件 mtime
    判定，正在写入的运行日志不会满足条件。归档目录里的子日志不参与扫描
    （归档/删除已把它们和父会话一起搬走）。
    """
    sessions = Path(sessions_dir)
    if _is_link(sessions) or _is_link(sessions.parent):
        emit(f'  跳过链接会话存储根目录 {sessions}，本轮不执行回收')
        return []
    cutoff = (time.time() if now is None else now) - days * 86400
    referenced = set()
    for log in sorted(sessions.glob('session_*.jsonl')):
        try:
            events = SessionEventStore.open(log).read_events()
        except OSError as exc:
            emit(f'  无法读取 {log.name}，本轮跳过孤儿子日志回收：{exc}')
            return []
        for event in events:
            if event.get('type') not in _STARTED_EVENTS:
                continue
            value = event.get('log')
            if isinstance(value, str) and value:
                referenced.add(value)
    orphans = []
    for name in _CHILD_DIRS:
        directory = sessions / name
        if _is_link(directory):
            emit(f'  跳过链接子日志目录 {directory}，本轮不执行回收')
            continue
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob('session_*.jsonl')):
            if _is_link(path):
                emit(f'  跳过链接子日志 {path}，本轮不执行回收')
                continue
            if f'{name}/{path.name}' in referenced:
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            orphans.append(path)
    return orphans


def purge_orphan_child_logs(sessions_dir, days=30, now=None, *, emit=print):
    """删除孤儿子日志，返回 (已删除路径, 失败数)；单个删除失败不影响其余。"""
    deleted, failures = [], 0
    for path in orphan_child_logs(sessions_dir, days, now, emit=emit):
        if _is_link(path.parent) or _is_link(path):
            failures += 1
            emit(f'  拒绝删除链接孤儿日志 {path.name}')
            continue
        try:
            path.unlink()
            deleted.append(path)
        except OSError as exc:
            failures += 1
            emit(f'  删除孤儿日志失败 {path.name}：{exc}')
    return deleted, failures


class SessionArchive:
    def __init__(self, sessions_dir, checkpoints_dir):
        self.sessions = Path(sessions_dir).resolve()
        self.checkpoints = Path(checkpoints_dir).resolve()
        self.root = self.sessions / '.archive'

    def _paths(self, session_id):
        if not session_id or not session_id.replace('-', '').replace('_', '').isalnum():
            raise ValueError('无效的会话标识')
        paths = (self.sessions / f'session_{session_id}.jsonl',
                 self.checkpoints / session_id, self.root / session_id)
        for path, parent in zip(paths, (self.sessions, self.checkpoints, self.root),
                                strict=True):
            if path.resolve().parent != parent or path.is_symlink():
                raise ValueError('会话路径超出存储目录或使用了链接，未执行改动')
        return paths

    def _child_source(self, value):
        """父会话事件里的 log 引用 → (当前路径, 相对路径)。"""
        rel = Path(str(value))
        if (rel.is_absolute() or len(rel.parts) != 2 or rel.parts[0] not in _CHILD_DIRS
                or not rel.name.startswith('session_') or rel.suffix != '.jsonl'):
            raise ValueError('子代理日志路径无效，未执行改动')
        path = self.sessions / rel
        if path.resolve().parent != self.sessions / rel.parts[0] or _is_link(path):
            raise ValueError('子代理日志超出存储目录或使用了链接，未执行改动')
        return path, rel

    def _referenced_children(self, log):
        """按父会话的 started 事件收集子日志；文件已被手工删除时跳过。"""
        referenced = {}
        for event in SessionEventStore.open(log).read_events():
            if event.get('type') not in _STARTED_EVENTS:
                continue
            value = event.get('log')
            if not isinstance(value, str) or not value:
                continue
            path, rel = self._child_source(value)
            if path.is_file():
                referenced[rel.as_posix()] = (path, rel)
        return list(referenced.values())

    def _archived_children(self, target):
        """归档中的子日志 → (归档路径, 恢复路径)，同时拒绝链接与未知文件。"""
        children = []
        for name in _CHILD_DIRS:
            directory = target / name
            if not directory.exists() and not directory.is_symlink():
                continue
            if _is_link(directory):
                raise ValueError('归档包含链接，未执行改动')
            if not directory.is_dir():
                raise ValueError('归档含有未知文件，未执行改动')
            for path in directory.iterdir():
                rel = Path(name) / path.name
                if (not path.is_file() or _is_link(path)
                        or not path.name.startswith('session_') or path.suffix != '.jsonl'):
                    raise ValueError('归档含有未知文件，未执行改动')
                destination = self.sessions / rel
                if destination.resolve().parent != self.sessions / name:
                    raise ValueError('归档路径超出存储目录，未执行改动')
                children.append((path, destination))
        return children

    @staticmethod
    def _rollback(moved):
        for source, destination in reversed(moved):
            if destination.exists():
                destination.rename(source)

    @staticmethod
    def _remove_empty_children(target):
        for name in _CHILD_DIRS:
            directory = target / name
            if directory.exists():
                try:
                    directory.rmdir()
                except OSError:
                    pass

    def archive(self, row, current_id):
        if row['id'] == current_id:
            raise ValueError('不能清理当前会话')
        log, checkpoints, target = self._paths(row['id'])
        with SessionEventStore._path_lock(log):
            summary = SessionEventStore.open(log).session_summary()
            if summary != row:
                raise ValueError('会话已更新，请重新选择')
            children = self._referenced_children(log)
            if target.exists():
                raise ValueError('已有同名归档，不覆盖现有记录')
            target.mkdir(parents=True)
            moved = []
            try:
                if checkpoints.exists():
                    checkpoints.rename(target / 'checkpoints')
                for source, rel in children:
                    destination = target / rel
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source.rename(destination)
                    moved.append((source, destination))
                log.rename(target / log.name)
            except OSError:
                self._rollback(moved)
                if (target / 'checkpoints').exists():
                    (target / 'checkpoints').rename(checkpoints)
                self._remove_empty_children(target)
                target.rmdir()
                raise

    def list(self, *, emit=print):
        if not self.root.exists():
            return []
        rows = []
        for path in self.root.glob('*/session_*.jsonl'):
            try:
                rows.append(SessionEventStore.open(path).session_summary())
            except (OSError, ValueError) as exc:
                emit(f'  无法读取归档 {path.parent.name}，已跳过：{exc}')
        return sorted(rows, key=lambda row: row['updated'], reverse=True)

    def restore(self, session_id):
        log, checkpoints, target = self._paths(session_id)
        archived_log = target / log.name
        with SessionEventStore._path_lock(log):
            if not target.exists() or not target.is_dir():
                raise ValueError('归档不存在')
            if _is_link(target):
                raise ValueError('归档包含链接，未执行改动')
            allowed = {log.name, 'checkpoints', *_CHILD_DIRS}
            if any(path.name not in allowed for path in target.iterdir()):
                raise ValueError('归档含有未知文件，未执行改动')
            if archived_log.is_symlink() or (target / 'checkpoints').is_symlink():
                raise ValueError('归档包含链接，未执行改动')
            if not archived_log.is_file():
                raise ValueError('归档不存在')
            if log.exists() or checkpoints.exists():
                raise ValueError('已有同名会话或快照，不覆盖现有记录')
            children = self._archived_children(target)
            if any(destination.exists() or destination.is_symlink()
                   for _, destination in children):
                raise ValueError('已有同名子代理日志，不覆盖现有记录')
            archived_checkpoints = target / 'checkpoints'
            moved = []
            try:
                if archived_checkpoints.exists():
                    checkpoints.parent.mkdir(parents=True, exist_ok=True)
                    archived_checkpoints.rename(checkpoints)
                for source, destination in children:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source.rename(destination)
                    moved.append((source, destination))
                archived_log.rename(log)
            except OSError:
                self._rollback(moved)
                if checkpoints.exists():
                    checkpoints.rename(archived_checkpoints)
                raise
            self._remove_empty_children(target)
            target.rmdir()

    def delete(self, row, current_id):
        """Permanently remove one validated archive, never an active log."""
        if row['id'] == current_id:
            raise ValueError('不能删除当前会话')
        log, checkpoints, target = self._paths(row['id'])
        archived_log = target / log.name
        with SessionEventStore._path_lock(log):
            if log.exists() or checkpoints.exists():
                raise ValueError('会话仍在使用中或存在同名记录，请先归档')
            if not archived_log.is_file():
                raise ValueError('归档不存在')
            if SessionEventStore.open(archived_log).session_summary() != row:
                raise ValueError('归档已更新，请重新选择')
            # Reject links/junctions and unexpected data before recursive deletion.
            # Every resolved target must remain within this exact archive folder.
            for path in [target, *target.rglob('*')]:
                if _is_link(path):
                    raise ValueError('归档包含链接，未执行删除')
                if not path.resolve().is_relative_to(target):
                    raise ValueError('归档路径超出目录，未执行删除')
            allowed = {log.name, 'checkpoints', *_CHILD_DIRS}
            if any(path.name not in allowed for path in target.iterdir()):
                raise ValueError('归档含有未知文件，未执行删除')
            self._archived_children(target)
            shutil.rmtree(target)
