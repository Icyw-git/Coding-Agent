"""Recoverable session archival. Never touches workspace files."""

import shutil
import stat
import time
from pathlib import Path

from session_store import SessionEventStore


def cleanup_candidates(rows, current_id, days=30, empty_only=False, now=None):
    cutoff = (time.time() if now is None else now) - days * 86400
    return [row for row in rows if row['id'] != current_id
            and (row['preview'] == '尚无任务' if empty_only else row['updated'] < cutoff)]


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

    def archive(self, row, current_id):
        if row['id'] == current_id:
            raise ValueError('不能清理当前会话')
        log, checkpoints, target = self._paths(row['id'])
        with SessionEventStore._path_lock(log):
            summary = SessionEventStore.open(log).session_summary()
            if summary != row:
                raise ValueError('会话已更新，请重新选择')
            if target.exists():
                raise ValueError('已有同名归档，不覆盖现有记录')
            target.mkdir(parents=True)
            try:
                if checkpoints.exists():
                    checkpoints.rename(target / 'checkpoints')
                log.rename(target / log.name)
            except OSError:
                if (target / 'checkpoints').exists():
                    (target / 'checkpoints').rename(checkpoints)
                target.rmdir()
                raise

    def list(self):
        if not self.root.exists():
            return []
        rows = []
        for path in self.root.glob('*/session_*.jsonl'):
            try:
                rows.append(SessionEventStore.open(path).session_summary())
            except (OSError, ValueError) as exc:
                print(f'  无法读取归档 {path.parent.name}，已跳过：{exc}')
        return sorted(rows, key=lambda row: row['updated'], reverse=True)

    def restore(self, session_id):
        log, checkpoints, target = self._paths(session_id)
        archived_log = target / log.name
        with SessionEventStore._path_lock(log):
            if archived_log.is_symlink() or (target / 'checkpoints').is_symlink():
                raise ValueError('归档包含链接，未执行改动')
            if not archived_log.is_file():
                raise ValueError('归档不存在')
            if log.exists() or checkpoints.exists():
                raise ValueError('已有同名会话或快照，不覆盖现有记录')
            archived_checkpoints = target / 'checkpoints'
            if archived_checkpoints.exists():
                checkpoints.parent.mkdir(parents=True, exist_ok=True)
                archived_checkpoints.rename(checkpoints)
            try:
                archived_log.rename(log)
            except OSError:
                if checkpoints.exists():
                    checkpoints.rename(archived_checkpoints)
                raise
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
                info = path.lstat()
                if (path.is_symlink() or getattr(info, 'st_file_attributes', 0)
                        & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
                    raise ValueError('归档包含链接，未执行删除')
                if not path.resolve().is_relative_to(target):
                    raise ValueError('归档路径超出目录，未执行删除')
            allowed = {log.name, 'checkpoints'}
            if any(path.name not in allowed for path in target.iterdir()):
                raise ValueError('归档含有未知文件，未执行删除')
            shutil.rmtree(target)
