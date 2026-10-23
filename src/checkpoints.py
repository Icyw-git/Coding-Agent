"""Checkpoint / Rewind：把「会话状态 + 工作区文件状态」绑成可回溯节点。

设计见 CHECKPOINT_DESIGN.md。约定：
- 本模块只做文件逻辑，不 import harness（避免循环依赖）；可变配置（目录/忽略名单）由调用方传入。
- 快照失败不得影响 write/edit 的正常落盘：capture_pre_image 内部吞掉异常并打印告警。
- 恢复语义是「回到该节点开始时的状态」：文件侧按节点链逆序合成 pre-image（见 plan_restore）。
- bash / 外部进程的文件改动也有覆盖：begin_turn 预存当时已脏文件的 pre-image
  （source='baseline'），seal 时从 git 基线补记回合内的新建/修改/删除（source='git'）。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from exec_policy import is_secret_path
from perf import segment as perf_segment

DEFAULT_IGNORE = ('.cache', '.git', '.worktrees', '.mailbox', '__pycache__', '.pytest_cache')
# 密钥材料：证书/私钥文件与 dot-env 的全部变体（.env.local、.env.production 常放真凭证），
# 以及 OpenSSH 私钥的默认文件名。公开可写的示例模板在 exec_policy 里放行。
DEFAULT_SECRETS = ('.env', '.env.*', '*.pem', '*.key', '*.p12', '*.pfx',
                   '*_rsa', 'id_ecdsa', 'id_ed25519', 'id_dsa')
MAX_FILE_BYTES = 5 * 1024 * 1024
MESSAGES_FILE = 'messages.jsonl'
MESSAGES_GZIP_FILE = 'messages.jsonl.gz'


def _split_rename_paths(value: str) -> tuple[str, str] | None:
    """Split Git's ``old -> new`` display form outside quoted path names."""
    quoted = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == '\\' and quoted:
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if not quoted and value.startswith(' -> ', index):
            return value[:index].strip().strip('"'), value[index + 4:].strip().strip('"')
    return None


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


def _tree_bytes(path: Path) -> int:
    """节点目录占用的字节数；删除预览和回收统计共用。"""
    try:
        return sum(item.stat().st_size for item in path.rglob('*')
                   if item.is_file())
    except OSError:
        return 0


def _session_dirs(base_dir) -> list[Path]:
    base = Path(base_dir)
    if not base.is_dir():
        return []
    return sorted(entry for entry in base.iterdir()
                  if entry.is_dir() and (entry / 'index.jsonl').is_file())


def checkpoint_counts(base_dir) -> dict[str, int]:
    """每个会话仍有效的节点数；用于区分“当前会话无需清理”和全局未处理量。"""
    counts = {}
    for entry in _session_dirs(base_dir):
        manager = CheckpointManager(base_dir, entry, entry.name)
        counts[entry.name] = len(manager.list(limit=10_000))
    return counts


def _read_plain_snapshot(path: Path) -> list:
    """明文 JSONL 快照 → 消息列表；空行跳过，坏行抛 JSONDecodeError。"""
    messages = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.strip():
            messages.append(json.loads(line))
    return messages


def _read_gzip_snapshot(path: Path) -> list:
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        return [json.loads(line) for line
                in handle.read().splitlines() if line.strip()]


def migrate_legacy_checkpoints(base_dir, *, emit=print) -> dict:
    """把升级前的明文 messages.jsonl 快照压缩成 .gz（一次性、幂等）。

    每个节点先写 .gz，再解压回读与明文逐条比对，一致才删除明文；任何一步
    失败都保留明文原样。新节点本就直接写 .gz，重跑只会得到 skipped。
    """
    stats = {'migrated': 0, 'skipped': 0, 'failed': 0}
    base = Path(base_dir)
    if not base.is_dir():
        return stats
    for plain in sorted(base.glob('*/*/messages.jsonl')):
        compressed = plain.parent / MESSAGES_GZIP_FILE
        if compressed.is_file():
            # .gz 已存在：多半是上次迁移写到一半。内容一致就补删明文，
            # 不一致（或明文损坏）则不动，读路径本就优先用 .gz。
            try:
                if _read_plain_snapshot(plain) == _read_gzip_snapshot(compressed):
                    plain.unlink()
                    stats['migrated'] += 1
                else:
                    stats['skipped'] += 1
            except (OSError, json.JSONDecodeError, ValueError):
                stats['skipped'] += 1
            continue
        try:
            messages = _read_plain_snapshot(plain)
        except (OSError, json.JSONDecodeError) as exc:
            stats['failed'] += 1
            emit(f'  [checkpoint] 损坏快照保持原样 {plain.parent.name}: {exc}')
            continue
        try:
            _atomic_write(compressed, CheckpointManager._encode_messages(messages))
            if _read_gzip_snapshot(compressed) != messages:
                raise ValueError('压缩回读与明文不一致')
        except Exception as exc:
            compressed.unlink(missing_ok=True)
            stats['failed'] += 1
            emit(f'  [checkpoint] 迁移失败，明文保留 {plain.parent.name}: {exc}')
            continue
        try:
            plain.unlink()
        except OSError as exc:
            stats['failed'] += 1
            emit(f'  [checkpoint] 明文删除失败 {plain.parent.name}: {exc}')
            continue
        stats['migrated'] += 1
    return stats


def gc_all_checkpoints(base_dir, *, max_nodes: int = 20,
                       keep_labeled: bool = True,
                       dry_run: bool = True) -> dict:
    """按每个会话自己的保留策略回收旧节点；dry_run 只返回计划。

    全局清理不会删除会话事件流，只删 checkpoint 节点及其 pre-image；
    这是不可逆的，调用方必须在 dry_run 后取得用户确认。
    """
    keep = max(1, int(max_nodes))
    summary = {'sessions': 0, 'nodes': 0, 'removed': 0, 'bytes': 0,
               'details': [], 'dry_run': bool(dry_run)}
    for entry in _session_dirs(base_dir):
        manager = CheckpointManager(base_dir, entry, entry.name,
                                    max_nodes=keep,
                                    keep_labeled=keep_labeled)
        rows = manager.list(limit=10_000)
        plan = manager.gc_plan()
        summary['sessions'] += 1
        summary['nodes'] += len(rows)
        if not plan:
            continue
        summary['removed'] += len(plan)
        summary['bytes'] += sum(item['bytes'] for item in plan)
        summary['details'].append({
            'session_id': entry.name,
            'nodes': len(rows),
            'removed': len(plan),
            'bytes': sum(item['bytes'] for item in plan),
        })
        if not dry_run:
            manager.gc()
    return summary


def _atomic_write(path: Path, data: bytes) -> None:
    with perf_segment('cp.write'):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f'.tmp{uuid.uuid4().hex[:6]}')
        tmp.write_bytes(data)
        tmp.replace(path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


def _v1_path(path: str) -> str:
    # Porcelain v2 leaves spaces unquoted; v1 quotes them. Other escapes are
    # already supplied by Git in both formats.
    return f'"{path}"' if ' ' in path and not path.startswith('"') else path


class CheckpointManager:
    """一个 session 对应一个 manager；节点按 <base_dir>/<session_id>/<cp_id>/ 落盘。"""

    def __init__(self, base_dir, workspace, session_id, *, max_nodes=20, keep_labeled=True,
                 capture_files=True, ignore=None, secrets=None, max_file_bytes=MAX_FILE_BYTES):
        # state.json、index.jsonl 与节点必须按 session 隔离；否则新会话会继承旧会话的 head。
        self.base_dir = Path(base_dir) / session_id
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

    def _messages_path(self, cp_id: str) -> Path:
        """新节点用 gzip；旧节点没有 .gz 时继续读明文 JSONL。"""
        node_dir = self._node_dir(cp_id)
        compressed = node_dir / MESSAGES_GZIP_FILE
        if compressed.is_file():
            return compressed
        plain = node_dir / MESSAGES_FILE
        if plain.is_file():
            return plain
        return compressed

    def snapshot_path(self, cp_id: str) -> Path:
        """返回指定节点的会话快照路径，并校验它属于当前 session。"""
        self.get(cp_id)
        return self._messages_path(cp_id)

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
        if ref == 'last':
            return next((row['cp_id'] for row in rows if row.get('files')), rows[0]['cp_id'])
        if ref in ('turn', 'undo'):
            for row in rows:
                if self._is_turn_start(row):
                    return row['cp_id']
            # 老会话没有 turn_start 元数据、也读不到快照最后一条时，
            # 退化为旧的“最近文件改动”语义，避免恢复入口直接失效。
            return next((row['cp_id'] for row in rows if row.get('files')), rows[0]['cp_id'])
        self.get(ref)  # 不存在时抛 CheckpointError
        return ref

    def _is_turn_start(self, row: dict) -> bool:
        if 'turn_start' in row:
            return bool(row.get('turn_start'))
        try:
            messages = self.messages_snapshot(row['cp_id'])
        except (OSError, ValueError, TypeError, CheckpointError):
            return False
        return bool(messages) and messages[-1].get('role') == 'user'

    # ---------- 节点生命周期 ----------

    def begin_turn(self, messages: list, session_store=None, label: str = '', trigger: str = 'turn') -> str:
        """开一个节点并把活跃指针指向它；上一条未 seal 的节点会先被 seal。"""
        with perf_segment('cp.begin_turn'):
            return self._begin_turn(messages, session_store,
                                    label=label, trigger=trigger)

    def _begin_turn(self, messages: list, session_store=None, label: str = '', trigger: str = 'turn') -> str:
        if self._active:
            self.seal()
        cp_id = f"cp_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
        node_dir = self._node_dir(cp_id)
        (node_dir / 'files').mkdir(parents=True, exist_ok=True)
        meta = {
            'cp_id': cp_id,
            'label': label,
            'trigger': trigger,
            'turn_start': (trigger == 'turn' and bool(messages)
                           and messages[-1].get('role') == 'user'),
            'session_id': self.session_id,
            'workspace': str(self.workspace),
            'through_seq': getattr(session_store, 'last_seq', None),
            'parent_id': self._state.get('current_head'),
            'status': 'open',
            'created_at': time.time(),
            'files': {},
            'git': self._git_baseline(),
        }
        _atomic_write(node_dir / MESSAGES_GZIP_FILE, self._encode_messages(messages))
        self._capture_baseline(node_dir, meta)
        self._write_meta(node_dir, meta)
        self._append_index(self._summary(meta))
        self._save_head(cp_id)
        self._active = cp_id
        if session_store is not None:
            session_store.append_checkpoint(cp_id, meta['through_seq'], label=label, trigger=trigger)
        return cp_id

    def label_active(self, label: str, session_store=None, *, trigger: str = 'manual') -> str:
        """给当前回合的有效快照加标签，不在未完成 tool call 中间另建快照。"""
        if not self._active:
            raise CheckpointError('no active checkpoint to label')
        node_dir = self._node_dir(self._active)
        meta = self.get(self._active)
        meta['label'] = label
        meta['trigger'] = trigger
        self._write_meta(node_dir, meta)
        self._append_index(self._summary(meta))
        if session_store is not None:
            session_store.append_checkpoint(
                self._active, session_store.last_seq, label=label, trigger=trigger,
            )
        return self._active

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
        if key is None:
            return
        if self._capture_key(node_dir, meta, key):
            self._write_meta(node_dir, meta)

    def _capture_key(self, node_dir: Path, meta: dict, key: str,
                     source: str = 'tool') -> bool:
        """记录一个 workspace 相对键；返回是否写入记录。

        pre-image 只在文件存在且非敏感/未超限时落盘。工具路径沿用「同一节点
        同一路径只记一次」：节点开始时基线已抓过的文件，工具捕获自然跳过，
        pre-image 因此是节点开始时的状态——对「回到该节点开始时」更准确。
        """
        if key in meta['files'] or self._is_ignored(key):
            return False
        path = self._abs_of(key)
        if path.is_dir():
            return False
        exists = path.is_file()
        record = {
            'path': key,
            'exists': exists,
            'pre_sha256': _sha256(path) if exists else '',
            'sensitive': self._is_secret(Path(key).name),
            'source': source,
        }
        if exists and not record['sensitive']:
            if path.stat().st_size > self.max_file_bytes:
                record['unstorable'] = True
            else:
                self._store_pre_image(node_dir, key, path.read_bytes())
        meta['files'][key] = record
        return True

    def _store_pre_image(self, node_dir: Path, key: str, data: bytes) -> None:
        dest = node_dir / 'files' / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + f'.tmp{uuid.uuid4().hex[:6]}')
        tmp.write_bytes(data)
        tmp.replace(dest)

    # ---------- bash / 外部改动的覆盖 ----------

    @staticmethod
    def _status_keys(lines) -> set[str]:
        """v1 状态行 → workspace 相对键集合；重命名/复制两侧都算键。

        源路径在节点开始时可能仍存在（copy）或已消失（rename），
        `_capture_key` 按当时的真实状态记录，回溯语义各自成立。
        """
        keys = set()
        for line in lines or []:
            if len(line) < 4 or not line.strip():
                continue
            name = line[3:].strip()
            if not name:
                continue
            if 'R' in line[:2] or 'C' in line[:2]:
                pair = _split_rename_paths(name)
                if pair is not None:
                    keys.update(path for path in pair if path)
                    continue
            name = name.strip().strip('"')
            if name:
                keys.add(name)
        return keys

    def _capture_baseline(self, node_dir: Path, meta: dict) -> None:
        """把 begin_turn 时已脏/未跟踪的文件预存 pre-image（source='baseline'）。

        这些文件不是本节点改出来的，但「回到节点开始时」必须还原它们的
        当时内容——bash 在回合里继续改它们时，工具捕获覆盖不到。
        """
        if not self.capture_files:
            return
        for key in sorted(self._status_keys((meta.get('git') or {}).get('status'))):
            try:
                self._capture_key(node_dir, meta, key, source='baseline')
            except OSError as exc:
                print(f"  \033[33m[checkpoint] baseline capture failed for {key}: {exc}\033[0m")

    def _capture_external_changes(self, node_dir: Path, meta: dict) -> None:
        """seal 时补记工具没碰过、但相对 git 基线有变化的文件（source='git'）。

        覆盖 bash / IDE / 其他进程在回合内造成的改动：
        - 干净跟踪文件被改：pre-image 从 HEAD blob 取（= 节点开始状态）；
        - 回合内新建（HEAD 里没有）：exists=False，回溯=删除；
        - bash 删掉的跟踪文件：pre-image 取 blob、post 置空，回溯=重建；
        - 创建后又删除净变化为零，不记录；敏感文件照旧只留元数据。
        """
        if not self.capture_files:
            return
        head = (meta.get('git') or {}).get('head') or ''
        if not head:
            return   # 非 git 工作区 / unborn HEAD：没有基线可比
        try:
            baseline_keys = self._status_keys((meta.get('git') or {}).get('status'))
            candidates = sorted(self._status_keys(self._git_status()[1]) - baseline_keys)
        except Exception as exc:   # 外部捕获失败不影响 seal 本身
            print(f"  \033[33m[checkpoint] external change scan failed: {exc}\033[0m")
            return
        if not candidates:
            return
        tracked = self._git_tracked(head)
        captured = False
        for key in candidates:
            try:
                if self._capture_external_key(node_dir, meta, key, head=head,
                                              exists_now=self._abs_of(key).is_file(),
                                              tracked=key in tracked):
                    captured = True
            except OSError as exc:
                print(f"  \033[33m[checkpoint] external capture failed for {key}: {exc}\033[0m")
        if captured:
            self._write_meta(node_dir, meta)

    def _capture_external_key(self, node_dir: Path, meta: dict, key: str, *,
                              head: str, exists_now: bool, tracked: bool) -> bool:
        if key in meta['files'] or self._is_ignored(key):
            return False
        path = self._abs_of(key)
        if path.is_dir():
            return False
        record = {
            'path': key,
            # exists 语义是「节点开始时是否存在」：候选键都不在基线脏名单里，
            # tracked ⇒ 基线干净存在（内容=HEAD blob）；未跟踪 ⇒ 回合内新建。
            'exists': tracked,
            'pre_sha256': '',
            'post_sha256': _sha256(path) if exists_now else '',
            'sensitive': self._is_secret(Path(key).name),
            'source': 'git',
        }
        if not exists_now and not tracked:
            return False   # 建了又删 / 竞态：净变化为零
        if exists_now and not tracked:
            # 回合内新建：节点开始时不存在，回溯应删除（敏感文件也只留元数据）
            meta['files'][key] = record
            return True
        blob = self._git_blob(head, key)
        if blob is None:
            # 读不到基线内容：只标记 unstorable，不做无依据的恢复
            record['unstorable'] = True
            meta['files'][key] = record
            return True
        record['pre_sha256'] = hashlib.sha256(blob).hexdigest()
        if not record['sensitive'] and len(blob) <= self.max_file_bytes:
            self._store_pre_image(node_dir, key, blob)
        elif len(blob) > self.max_file_bytes:
            record['unstorable'] = True
        meta['files'][key] = record
        return True

    def _git_tracked(self, head: str) -> set[str]:
        """HEAD 提交跟踪的文件集合；-z 下路径不转义。失败返回空集。"""
        if not head:
            return set()
        raw = self._git('ls-tree', '-r', '--name-only', '-z', head)
        return {path for path in raw.split('\0') if path} if raw else set()

    def _git_blob(self, head: str, key: str) -> bytes | None:
        """HEAD 里该文件的原始字节；读不到（git 故障/竞态）返回 None。"""
        with perf_segment('cp.git'):
            try:
                result = subprocess.run(
                    ['git', '--no-optional-locks', 'cat-file', 'blob',
                     f'{head}:{key}'], cwd=self.workspace,
                    capture_output=True, timeout=15)
                return result.stdout if result.returncode == 0 else None
            except Exception:
                return None

    def seal(self) -> dict | None:
        """结束当前节点：记录每个快照文件写入后的 hash，供冲突检测使用。"""
        with perf_segment('cp.seal'):
            return self._seal()

    def _seal(self) -> dict | None:
        if not self._active:
            return None
        cp_id, self._active = self._active, None
        node_dir = self._node_dir(cp_id)
        meta = self.get(cp_id)
        for key, record in meta['files'].items():
            record['post_sha256'] = _sha256(self._abs_of(key))
        self._capture_external_changes(node_dir, meta)
        meta['status'] = 'sealed'
        meta['sealed_at'] = time.time()
        self._write_meta(node_dir, meta)
        self._append_index(self._summary(meta))
        return meta

    def _summary(self, meta: dict) -> dict:
        files = []
        for key, record in (meta.get('files') or {}).items():
            if record.get('source') == 'baseline' \
                    and (record.get('pre_sha256') or '') == (record.get('post_sha256') or ''):
                # 基线脏文件本轮没被改动：不进索引 files，避免污染 last/undo
                # 的「最近有文件改动的节点」判定。seal 时 post 落定后重算。
                continue
            files.append(key)
        return {
            'cp_id': meta['cp_id'],
            'label': meta.get('label', ''),
            'trigger': meta.get('trigger', ''),
            'turn_start': bool(meta.get('turn_start')),
            'session_id': meta.get('session_id', ''),
            'through_seq': meta.get('through_seq'),
            'parent_id': meta.get('parent_id'),
            'status': meta.get('status', 'open'),
            'created_at': meta.get('created_at', 0),
            'files': sorted(files),
        }

    def changed_files_since(self, start_id: str | None) -> dict[str, str]:
        """start_id 之后（不含它）的节点里，净内容发生变化的文件。

        用于回合结束汇总，同时也是非 Git 工作区唯一能拿到改动清单的来源：
        write/edit/apply_patch 的 pre-image 与 seal 后的 post hash 都在这里；
        Git 工作区里 baseline（source='baseline'）与外部（source='git'）记录
        让 bash 造成的改动也进入这份清单。净变化为空的文件不会出现。
        """
        if self._active:
            chain = self._chain_to_head(self._active)
        elif start_id:
            chain = self._chain_to_head(start_id)
        else:
            chain = [row['cp_id'] for row in self.list(limit=10_000)]
        if start_id and chain and chain[-1] == start_id:
            chain = chain[:-1]   # start 是上一回合的节点，不属于本回合
        first: dict[str, str] = {}
        last: dict[str, str] = {}
        for node_id in reversed(chain):
            try:
                meta = self.get(node_id)
            except CheckpointError:
                continue
            # 活跃节点还没 seal：post hash 现场算，别把「本回合正在改」当成没变
            live = node_id == self._active
            for key, record in (meta.get('files') or {}).items():
                first.setdefault(key, record.get('pre_sha256') or '')
                post = record.get('post_sha256')
                if post is None and live:
                    post = _sha256(self._abs_of(key))
                last[key] = post or ''
        changed: dict[str, str] = {}
        for key, before in first.items():
            after = last.get(key, '')
            if before == after:
                continue
            changed[key] = 'A' if not before else ('D' if not after else 'M')
        return changed

    def _write_meta(self, node_dir: Path, meta: dict) -> None:
        _atomic_write(node_dir / 'meta.json',
                      json.dumps(meta, ensure_ascii=False, indent=2,
                                 default=str).encode('utf-8'))

    @staticmethod
    def _encode_messages(messages: list) -> bytes:
        body = ''.join(json.dumps(m, ensure_ascii=False, default=str) + '\n' for m in messages)
        buffer = io.BytesIO()
        # mtime=0 让相同快照得到相同字节，便于测试和跨机器比较。
        with gzip.GzipFile(fileobj=buffer, mode='wb', compresslevel=6, mtime=0) as handle:
            handle.write(body.encode('utf-8'))
        return buffer.getvalue()

    def messages_snapshot(self, cp_id: str) -> list[dict]:
        path = self._messages_path(cp_id)
        if not path.is_file():
            raise CheckpointError(f'checkpoint {cp_id} has no messages snapshot')
        if path.suffix == '.gz':
            with gzip.open(path, 'rt', encoding='utf-8') as handle:
                lines = handle.read().splitlines()
        else:
            lines = path.read_text(encoding='utf-8').splitlines()
        messages = []
        for line in lines:
            if line.strip():
                messages.append(json.loads(line))
        return messages

    # ---------- 排除规则 ----------

    def _is_ignored(self, key: str) -> bool:
        parts = Path(key).parts
        return any(part in self.ignore for part in parts)

    def _is_secret(self, name: str) -> bool:
        # 与策略层同一个判定：秘密名单只有一份，模板例外也只有一个地方解释。
        return is_secret_path(name, self.secrets)

    # ---------- git 基线 ----------

    def _git(self, *args) -> str:
        with perf_segment('cp.git'):
            try:
                # --no-optional-locks：status 可能刷新并写回 .git/index；快照是
                # 只读观察者，不跟用户/其他进程抢索引锁。
                result = subprocess.run(
                    ['git', '--no-optional-locks', *args], cwd=self.workspace,
                    capture_output=True, text=True, encoding='utf-8',
                    errors='replace', timeout=15)
                return result.stdout.rstrip('\r\n')
            except Exception:
                return ''

    def _git_status(self) -> tuple[str, list[str]]:
        """一次 git 进程取 HEAD 与工作区状态（porcelain v2 -z → v1 行）。

        -z 会关掉 quotePath 转义，unicode / 空格路径原样保留——v1 输出会把
        非ASCII 路径转义成 `\\346...`，拿去做文件键会错位甚至误删；
        `--untracked-files=all` 让未跟踪文件逐个列出（目录折叠会让基线
        漏掉目录里的新文件）。输出行格式与旧版存储兼容：`XY <path>`、
        重命名 `XY <orig> -> <path>`，含空格的路径由 _v1_path 统一加引号。
        非 git 目录返回 ('', [])。
        """
        from workspace_diff import repo_root

        workspace = getattr(self, 'workspace', None)
        if workspace is not None and repo_root(workspace) is None:
            # 非 git 工作区：没必要为每轮快照起一个注定失败的 git 进程。
            return '', []
        raw = self._git('status', '--porcelain=v2', '--branch', '-z',
                        '--untracked-files=all')
        # Keep compatibility with older Git builds, wrappers and callers/tests
        # that return porcelain-v1 text despite the requested v2/-z format.
        if raw and '\0' not in raw:
            head = ''
            status = []
            for line in raw.splitlines():
                if line.startswith('# branch.oid '):
                    head = line[len('# branch.oid '):]
                    if head == '(initial)':
                        head = ''
                    continue
                if line.startswith(('#', '!')):
                    continue
                if line.startswith('2 ') and '\t' in line:
                    fields = line.split(None, 9)
                    if len(fields) == 10:
                        destination, original = fields[9].split('\t', 1)
                        code = fields[1].replace('.', ' ')
                        status.append(
                            f'{code} {_v1_path(original)} -> {_v1_path(destination)}')
                    continue
                if len(line) < 4:
                    continue
                code, name = line[:2], line[3:].strip()
                if 'R' in code or 'C' in code:
                    original, separator, destination = name.partition(' -> ')
                    if separator:
                        original = _v1_path(original.strip().strip('"'))
                        destination = _v1_path(destination.strip().strip('"'))
                        name = f'{original} -> {destination}'
                else:
                    name = _v1_path(name.strip().strip('"'))
                status.append(f'{code} {name}')
            return head, sorted(status)
        head, status = '', []
        tokens = raw.split('\0')
        index = 0
        while index < len(tokens):
            line = tokens[index]
            index += 1
            if not line:
                continue
            if line.startswith('# branch.oid '):
                head = line[len('# branch.oid '):]
                if head == '(initial)':
                    head = ''
            elif line.startswith(('#', '!')):
                continue   # 其余头行 / 被忽略文件
            elif line.startswith('?'):
                status.append('?? ' + _v1_path(line[2:]))
            elif line.startswith('1 '):
                parts = line.split(None, 8)
                if len(parts) == 9:
                    # v2 用 '.' 表示未变一侧，v1 用空格
                    status.append(f'{parts[1].replace(".", " ")} {_v1_path(parts[8])}')
            elif line.startswith('2 '):   # 重命名/复制
                parts = line.split(None, 9)
                if len(parts) != 10:
                    continue
                if '\t' in parts[9]:   # 兜底：个别 git 未按 -z 拆分字段
                    path, original = parts[9].split('\t', 1)
                else:
                    # -z 下 origPath 是独立的 NUL 字段
                    original = tokens[index] if index < len(tokens) else ''
                    index += 1
                    path = parts[9]
                if original:
                    status.append(f'{parts[1].replace(".", " ")} '
                                  f'{_v1_path(original)} -> {_v1_path(path)}')
            elif line.startswith('u '):
                parts = line.split(None, 10)
                if len(parts) == 11:
                    status.append(f'{parts[1]} {_v1_path(parts[10])}')
        return head, sorted(status)

    def _git_baseline(self) -> dict:
        """v2 转回 v1 行格式——`_drift_files` 的基线对比与 `_status_keys`
        都依赖 v1 行结构（line[3:] 取路径），不能直接存 v2 原文。"""
        head, status = self._git_status()
        return {'head': head, 'status': status}

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

    def plan_restore(self, cp_id: str, *, scope: str = 'all') -> dict:
        """不落盘：算出将执行的文件动作、冲突与不可恢复项。"""
        meta = self.get(cp_id)
        chain = self._chain_to_head(cp_id)
        if scope != 'session':
            for node_id in chain:
                workspace = self.get(node_id).get('workspace')
                if not workspace or Path(workspace).resolve() != self.workspace:
                    raise CheckpointError(
                        '快照工作区不匹配或旧快照未记录工作区；'
                        '禁止恢复文件，可用 --session 仅恢复会话。')
        # chain 是 head → target：迭代过程中后写入的 source 来自更早的节点，
        # 因此最终 source[key] 是「该节点开始时」应恢复的那份 pre-image。
        source: dict[str, tuple] = {}
        post_hashes: dict[str, str] = {}
        for node_id in chain:
            if scope == 'session':
                break
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
            elif (record['exists'] and record.get('pre_sha256')
                  and not (self._node_dir(node_id) / 'files' / key).is_file()):
                # 有 hash 但内容没了（节点目录被手删/损坏）：诚实跳过，
                # 避免计划显示 restore、执行时才发现 pre-image 缺失。
                entry.update(action='skip', reason='pre-image content missing')
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
            'drift': self._drift_files(meta, touched) if scope != 'session' else [],
            'side_effects': self.side_effects(),
        }

    def _drift_files(self, meta: dict, touched: set) -> list[str]:
        """bash / 外部改动导致、且不在 checkpoint 清单里的文件（只提示，不恢复）。

        当前侧与基线同用 `_git_status`（v2 -z → v1 行 + -uall），保证
        行格式逐字可比——旧实现用裸 `status --porcelain`，非ASCII 路径
        会被转义成与基线不同的行，造成假 drift。
        """
        baseline = set((meta.get('git') or {}).get('status') or [])
        _, current = self._git_status()
        drift = []
        for line in sorted(set(current) - baseline):
            name = line[3:]
            if 'R' in line[:2] or 'C' in line[:2]:
                name = name.split(' -> ', 1)[-1]
            name = name.strip().strip('"')
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
        plan = self.plan_restore(cp_id, scope=scope)
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

    def gc_plan(self, max_nodes: int | None = None) -> list[dict]:
        """列出会被 gc 删除的节点（最旧优先），不改动磁盘。"""
        limit = self.max_nodes if max_nodes is None else max(1, int(max_nodes))
        rows = self.list(limit=10_000)
        head = self.head()
        removals = []
        remaining = len(rows)
        for row in reversed(rows):  # 最旧优先
            if remaining <= limit:
                break
            cp_id = row['cp_id']
            if cp_id == head or (self.keep_labeled and row.get('label')):
                continue
            removals.append({
                'session_id': self.session_id,
                'cp_id': cp_id,
                'label': row.get('label', ''),
                'bytes': _tree_bytes(self._node_dir(cp_id)),
            })
            remaining -= 1
        return removals

    def gc(self, max_nodes: int | None = None) -> list[str]:
        """删除最旧的、非 head、非 label 的节点；返回被删 cp_id。

        注意：删除会切断更早节点的回溯链，之后回滚到被回收节点之前的节点时，
        文件侧退化为「只用目标节点自己的 pre-image」（见设计文档 §12.8）。
        """
        removed = []
        for item in self.gc_plan(max_nodes):
            cp_id = item['cp_id']
            shutil.rmtree(self._node_dir(cp_id), ignore_errors=True)
            self._append_index({'cp_id': cp_id, 'gc': True, 'gc_at': time.time()})
            removed.append(cp_id)
        return removed
