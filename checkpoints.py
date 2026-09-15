"""Checkpoint / Rewind：把「会话状态 + 工作区文件状态」绑成可回溯节点。

设计见 CHECKPOINT_DESIGN.md。约定：
- 本模块只做文件逻辑，不 import harness（避免循环依赖）；可变配置（目录/忽略名单）由调用方传入。
- 快照失败不得影响 write/edit 的正常落盘：capture_pre_image 内部吞掉异常并打印告警。
- 恢复语义是「回到该节点开始时的状态」：文件侧按节点链逆序合成 pre-image（见 plan_restore）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

DEFAULT_IGNORE = ('.cache', '.git', '.worktrees', '.mailbox', '__pycache__', '.pytest_cache')
DEFAULT_SECRETS = ('.env', '*.pem', '*_rsa')
MAX_FILE_BYTES = 5 * 1024 * 1024


class CheckpointError(RuntimeError):
    """快照缺失或节点不可用时抛出。"""


def _sha256(path: Path) -> str:
    if not path.is_file():
        return ''
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.tmp{uuid.uuid4().hex[:6]}')
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


class CheckpointManager:
    """一个 session 对应一个 manager；节点按 <base_dir>/<cp_id>/ 落盘。"""

    def __init__(self, base_dir, workspace, session_id, *, max_nodes=20, keep_labeled=True,
                 capture_files=True, ignore=None, secrets=None, max_file_bytes=MAX_FILE_BYTES):
        self.base_dir = Path(base_dir)
        self.workspace = Path(workspace).resolve()
        self.session_id = session_id
        self.max_nodes = max_nodes
        self.keep_labeled = keep_labeled
        self.capture_files = capture_files
        self.ignore = tuple(ignore or DEFAULT_IGNORE)
        self.secrets = tuple(secrets or DEFAULT_SECRETS)
        self.max_file_bytes = max_file_bytes
        self._active: str | None = None
        self._state = _read_json(self._state_path) or {'current_head': None}

    # ---------- 路径 ----------

    @property
    def _state_path(self) -> Path:
        return self.base_dir / 'state.json'

    @property
    def _index_path(self) -> Path:
        return self.base_dir / 'index.jsonl'

    def _node_dir(self, cp_id: str) -> Path:
        return self.base_dir / cp_id

    def _abs_of(self, key: str) -> Path:
        return self.workspace / key

    def _rel_key(self, path: Path) -> str | None:
        """workspace 相对键；落在 workspace 之外（或不可解析）时返回 None，不参与快照。"""
        try:
            return path.resolve().relative_to(self.workspace).as_posix()
        except (ValueError, OSError):
            return None

    # ---------- index / state ----------

    def _append_index(self, row: dict) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, separators=(',', ':'), default=str) + '\n'
        with open(self._index_path, 'a', encoding='utf-8', newline='\n') as handle:
            handle.write(line)
            handle.flush()

    def _index(self) -> list[dict]:
        if not self._index_path.exists():
            return []
        folded: dict[str, dict] = {}
        lines = self._index_path.read_text(encoding='utf-8', errors='replace').splitlines()
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # 半截尾行忽略，与 session_store 的容错取向一致
            cp_id = row.get('cp_id')
            if not cp_id:
                continue
            if row.get('gc'):
                folded.pop(cp_id, None)
                continue
            merged = {**folded.get(cp_id, {}), **row}
            folded[cp_id] = merged
        return sorted(folded.values(), key=lambda r: r.get('created_at', 0))

    def list(self, limit: int = 20) -> list[dict]:
        rows = [r for r in self._index() if self._node_dir(r['cp_id']).is_dir()]
        rows.reverse()
        return rows[:limit]

    def get(self, cp_id: str) -> dict:
        node_dir = self._node_dir(cp_id)
        if not node_dir.is_dir():
            raise CheckpointError(f'checkpoint {cp_id} not found')
        meta = _read_json(node_dir / 'meta.json')
        if not meta:
            raise CheckpointError(f'checkpoint {cp_id} has no meta.json')
        return meta

    def head(self) -> str | None:
        return self._state.get('current_head')

    def _save_head(self, cp_id: str | None) -> None:
        self._state['current_head'] = cp_id
        _atomic_write(self._state_path, json.dumps(self._state, ensure_ascii=False, indent=2).encode('utf-8'))

    def resolve(self, ref: str) -> str:
        """解析节点引用。

        - 'head' / 'latest' / ''：最新节点（可能没有文件改动，回滚等于只回滚会话）；
        - 'last' / 'undo'：最近一个**有文件改动**的节点 —— 也就是「撤销最近一次文件改动」，
          没有任何节点记录过文件时退化为最新节点。
        节点是按 loop 迭代生成的，一个用户回合可能有多个节点，只有真正 write/edit 过的那次
        才带 pre-image，所以 'last' 不能简单等同于最新节点。
        """
        rows = self.list(limit=10_000)
        if not rows:
            raise CheckpointError('no checkpoint available')
        if ref in ('head', 'latest', ''):
            return rows[0]['cp_id']
        if ref in ('last', 'undo'):
            return next((row['cp_id'] for row in rows if row.get('files')), rows[0]['cp_id'])
        self.get(ref)  # 不存在时抛 CheckpointError
        return ref

    # ---------- 节点生命周期 ----------

    def begin_turn(self, messages: list, session_store=None, label: str = '', trigger: str = 'turn') -> str:
        """开一个节点并把活跃指针指向它；上一条未 seal 的节点会先被 seal。"""
        if self._active:
            self.seal()
        cp_id = f"cp_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
        node_dir = self._node_dir(cp_id)
        (node_dir / 'files').mkdir(parents=True, exist_ok=True)
        meta = {
            'cp_id': cp_id,
            'label': label,
            'trigger': trigger,
            'session_id': self.session_id,
            'through_seq': getattr(session_store, 'last_seq', None),
            'parent_id': self._state.get('current_head'),
            'status': 'open',
            'created_at': time.time(),
            'files': {},
            'git': self._git_baseline(),
        }
        _atomic_write(node_dir / 'messages.jsonl', self._encode_messages(messages))
        self._write_meta(node_dir, meta)
        self._append_index(self._summary(meta))
        self._save_head(cp_id)
        self._active = cp_id
        if session_store is not None:
            session_store.append_checkpoint(cp_id, meta['through_seq'], label=label, trigger=trigger)
        return cp_id

    def capture_pre_image(self, path) -> None:
        """保存「写入前」的文件内容；同一节点内同一路径只保存一次。异常只告警，不打断写盘。"""
        if not self.capture_files or not self._active:
            return
        try:
            self._capture(Path(path))
        except Exception as exc:  # 快照失败不能影响 write/edit
            print(f"  \033[33m[checkpoint] capture failed for {path}: {exc}\033[0m")

    def _capture(self, path: Path) -> None:
        if path.is_dir():
            return  # write/edit 只针对文件；目录路径不记录，避免回溯时误删目录
        node_dir = self._node_dir(self._active)
        meta = self.get(self._active)
        key = self._rel_key(path)
        if key is None or key in meta['files'] or self._is_ignored(key):
            return
        exists = path.is_file()
        record = {
            'path': key,
            'exists': exists,
            'pre_sha256': _sha256(path) if exists else '',
            'sensitive': self._is_secret(path.name),
        }
        if exists and not record['sensitive']:
            if path.stat().st_size > self.max_file_bytes:
                record['unstorable'] = True
            else:
                dest = node_dir / 'files' / key
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + f'.tmp{uuid.uuid4().hex[:6]}')
                shutil.copyfile(path, tmp)
                os.replace(tmp, dest)
        meta['files'][key] = record
        self._write_meta(node_dir, meta)

    def seal(self) -> dict | None:
        """结束当前节点：记录每个快照文件写入后的 hash，供冲突检测使用。"""
        if not self._active:
            return None
        cp_id, self._active = self._active, None
        node_dir = self._node_dir(cp_id)
        meta = self.get(cp_id)
        for key, record in meta['files'].items():
            record['post_sha256'] = _sha256(self._abs_of(key))
        meta['status'] = 'sealed'
        meta['sealed_at'] = time.time()
        self._write_meta(node_dir, meta)
        self._append_index(self._summary(meta))
        return meta

    def _summary(self, meta: dict) -> dict:
        return {
            'cp_id': meta['cp_id'],
            'label': meta.get('label', ''),
            'trigger': meta.get('trigger', ''),
            'session_id': meta.get('session_id', ''),
            'through_seq': meta.get('through_seq'),
            'parent_id': meta.get('parent_id'),
            'status': meta.get('status', 'open'),
            'created_at': meta.get('created_at', 0),
            'files': sorted(meta.get('files', {}).keys()),
        }

    def _write_meta(self, node_dir: Path, meta: dict) -> None:
        _atomic_write(node_dir / 'meta.json', json.dumps(meta, ensure_ascii=False, indent=2, default=str).encode('utf-8'))

    @staticmethod
    def _encode_messages(messages: list) -> bytes:
        body = ''.join(json.dumps(m, ensure_ascii=False, default=str) + '\n' for m in messages)
        return body.encode('utf-8')

    def messages_snapshot(self, cp_id: str) -> list[dict]:
        path = self._node_dir(cp_id) / 'messages.jsonl'
        if not path.exists():
            raise CheckpointError(f'checkpoint {cp_id} has no messages snapshot')
        messages = []
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                messages.append(json.loads(line))
        return messages

    # ---------- 排除规则 ----------

    def _is_ignored(self, key: str) -> bool:
        parts = Path(key).parts
        return any(part in self.ignore for part in parts)

    def _is_secret(self, name: str) -> bool:
        return any(Path(name).match(pattern) for pattern in self.secrets)

    # ---------- git 基线 ----------

    def _git(self, *args) -> str:
        try:
            result = subprocess.run(['git', *args], cwd=self.workspace,
                                    capture_output=True, text=True, timeout=15)
            return result.stdout.strip()
        except Exception:
            return ''

    def _git_baseline(self) -> dict:
        return {
            'head': self._git('rev-parse', 'HEAD'),
            'status': sorted(self._git('status', '--porcelain').splitlines()),
        }

    # ---------- 回溯 ----------

    def _chain_to_head(self, cp_id: str) -> list[str]:
        """head → target 的节点链；target 不在 head 祖先链上时退化为 [target]。"""
        chain, cursor = [], self.head()
        while cursor:
            chain.append(cursor)
            if cursor == cp_id:
                return chain
            try:
                cursor = self.get(cursor).get('parent_id')
            except CheckpointError:
                cursor = None
        return [cp_id]

    def plan_restore(self, cp_id: str) -> dict:
        """不落盘：算出将执行的文件动作、冲突与不可恢复项。"""
        meta = self.get(cp_id)
        chain = self._chain_to_head(cp_id)
        # chain 是 head → target：迭代过程中后写入的 source 来自更早的节点，
        # 因此最终 source[key] 是「该节点开始时」应恢复的那份 pre-image。
        source: dict[str, tuple] = {}
        post_hashes: dict[str, str] = {}
        for node_id in chain:
            try:
                node_meta = self.get(node_id)
            except CheckpointError:
                continue
            for key, record in node_meta.get('files', {}).items():
                if record.get('post_sha256'):
                    post_hashes.setdefault(key, record['post_sha256'])
                source[key] = (record, node_id)

        actions, conflicts, unverified = [], [], []
        for key, (record, node_id) in source.items():
            current = _sha256(self._abs_of(key))
            entry = {'path': key, 'node': node_id}
            if record.get('sensitive'):
                entry.update(action='skip', reason='sensitive file: content not stored')
            elif record.get('unstorable'):
                entry.update(action='skip', reason='file too large: content not stored')
            elif record['exists'] and record.get('pre_sha256') == current:
                entry.update(action='noop', reason='already at checkpoint content')
            elif record['exists']:
                entry.update(action='restore', reason='restore pre-image')
            elif current:
                entry.update(action='delete', reason='created after checkpoint')
            else:
                entry.update(action='noop', reason='not present')

            expected = post_hashes.get(key, '')
            if entry['action'] in ('restore', 'delete'):
                if not expected:
                    # 节点尚未 seal（该轮未结束）或快照来自 open 节点：无法校验，只提示
                    entry['verified'] = False
                    unverified.append(key)
                elif current != expected:
                    entry['verified'] = True
                    entry['forced_action'] = entry['action']
                    entry.update(action='conflict', reason='file changed after checkpoint (user or bash)')
                    conflicts.append(key)
                else:
                    entry['verified'] = True
            actions.append(entry)

        touched = {entry['path'] for entry in actions}
        return {
            'ok': True,
            'cp_id': cp_id,
            'chain': chain,
            'status': meta.get('status'),
            'label': meta.get('label', ''),
            'actions': actions,
            'conflicts': conflicts,
            'unverified': unverified,
            'drift': self._drift_files(meta, touched),
            'side_effects': self.side_effects(),
        }

    def _drift_files(self, meta: dict, touched: set) -> list[str]:
        """bash / 外部改动导致、且不在 checkpoint 清单里的文件（只提示，不恢复）。"""
        baseline = set((meta.get('git') or {}).get('status') or [])
        if not baseline:
            return []
        drift = []
        for line in sorted(set(self._git('status', '--porcelain').splitlines()) - baseline):
            name = line[3:].strip().strip('"')
            if name and name not in touched and not self._is_ignored(name):
                drift.append(name)
        return drift

    def side_effects(self) -> dict:
        """回滚不会撤销的副作用（见设计文档 §8.6）。"""
        cache = self.workspace / '.cache'
        return {
            'memories': len(list((cache / 'memories').glob('*.md'))) if (cache / 'memories').is_dir() else 0,
            'crons_file': (cache / 'crons.json').is_file(),
            'tasks': len(list((cache / 'tasks').glob('task_*.json'))) if (cache / 'tasks').is_dir() else 0,
            'worktrees': len([p for p in (self.workspace / '.worktrees').glob('*') if p.is_dir()])
            if (self.workspace / '.worktrees').is_dir() else 0,
            'background_tasks': 'in-process: running background commands are not cancelled',
        }

    def restore(self, cp_id: str, *, force: bool = False, scope: str = 'all') -> dict:
        """执行回溯。scope='session' 时只回滚会话（不动文件）。"""
        plan = self.plan_restore(cp_id)
        plan['scope'] = scope
        plan['applied'] = []
        if scope != 'session':
            if plan['conflicts'] and not force:
                plan['ok'] = False
                plan['reason'] = ('conflict: ' + ', '.join(plan['conflicts'])
                                  + ' (use --force to overwrite)')
                return plan
            for entry in plan['actions']:
                action = entry['action']
                if action == 'conflict':  # 只可能出现在 --force 路径上
                    action = entry['forced_action']
                    entry['forced'] = True
                if action == 'restore':
                    src = self._node_dir(entry['node']) / 'files' / entry['path']
                    if not src.exists():
                        entry.update(action='skip', reason='pre-image file missing')
                        continue
                    dest = self._abs_of(entry['path'])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dest)
                    entry['action'] = 'restored'
                    plan['applied'].append(entry['path'])
                elif action == 'delete':
                    self._abs_of(entry['path']).unlink(missing_ok=True)
                    entry['action'] = 'deleted'
                    plan['applied'].append(entry['path'])
        self._save_head(cp_id)
        return plan

    # ---------- 保留策略 ----------

    def gc(self) -> list[str]:
        """删除最旧的、非 head、非 label 的节点；返回被删 cp_id。

        注意：删除会切断更早节点的回溯链，之后回滚到被回收节点之前的节点时，
        文件侧退化为「只用目标节点自己的 pre-image」（见设计文档 §12.8）。
        """
        rows = self.list(limit=10_000)
        head = self.head()
        removed = []
        for row in reversed(rows):  # 最旧优先
            if len(rows) - len(removed) <= self.max_nodes:
                break
            cp_id = row['cp_id']
            if cp_id == head or (self.keep_labeled and row.get('label')):
                continue
            shutil.rmtree(self._node_dir(cp_id), ignore_errors=True)
            self._append_index({'cp_id': cp_id, 'gc': True, 'gc_at': time.time()})
            removed.append(cp_id)
        return removed
