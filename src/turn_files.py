"""跨进程回合控制通道：另一个进程也可以 steer / 中断正在跑的回合。

进程内的 `TurnToken` 只能被同进程找到；TUI 在 A 进程、API/daemon 在 B 进程时，
B 的 `/steer` 看不到 A 的回合。这里用目录做最小控制面（不引入守护进程）：

    <cache>/control/<session_id>/
        owner.json     # {turn_id, pid, source, started_at}：哪个回合在跑
        steer/         # <request_id|uuid>.json：{content, request_id}
        cancel         # {turn_id}：取消标记

回合持有者（``agent_loop``）启动时写 owner、结束时删除；运行中轮询 steer 目录与
cancel 文件。发送方先看 owner：没有 owner 就说明空闲，交由调用方按普通消息处理。

取舍与边界：
- 只做单机文件协调，和全局单回合协调器一致；多主机请看方案的“不做”清单。
- 文件只是投递通道，不是真相源：回合内的消息仍写入会话事件流。
- 有界：steer 目录最多保留 ``MAX_PENDING_STEER`` 条、单条内容截断；
  owner 超过 ``STALE_AFTER`` 视为崩溃残留（回合结束时也会主动清理）。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

MAX_PENDING_STEER = 50
MAX_STEER_CHARS = 32_000
STALE_AFTER = 3600.0
CANCEL_POLL_INTERVAL = 0.5


def _safe_id(value: str) -> str:
    cleaned = ''.join(ch if (ch.isalnum() or ch in '-_') else '_'
                      for ch in str(value))
    return cleaned or 'unknown'


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


def _read_json(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding='utf-8')
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def control_dir(cache_root, session_id: str) -> Path:
    return Path(cache_root) / 'control' / _safe_id(session_id)


def _owner_path(directory: Path) -> Path:
    return directory / 'owner.json'


def active_owner(cache_root, session_id: str, *,
                 stale_after: float = STALE_AFTER, clock=time.time) -> dict | None:
    """返回正在执行该会话的 owner 信息；没有或已过期返回 None。"""
    directory = control_dir(cache_root, session_id)
    path = _owner_path(directory)
    try:
        info = path.stat()
    except OSError:
        return None
    if clock() - info.st_mtime > stale_after:
        return None
    data = _read_json(path)
    if not data or not data.get('turn_id'):
        return None
    return data


def send_steer(cache_root, session_id: str, content: str, *,
               request_id: str | None = None) -> dict | None:
    """把 steer 投递给活动回合；没有活动回合返回 None（调用方降级为普通消息）。"""
    owner = active_owner(cache_root, session_id)
    if owner is None:
        return None
    text = str(content)[:MAX_STEER_CHARS]
    directory = control_dir(cache_root, session_id) / 'steer'
    directory.mkdir(parents=True, exist_ok=True)
    pending = sorted(directory.glob('*.json'))
    if len(pending) >= MAX_PENDING_STEER:
        # 队列已满：拒绝而不是悄悄丢弃，调用方映射成 409/繁忙提示。
        raise RuntimeError('steer 队列已满，请稍后重试')
    name = f"{time.time_ns():020d}-{_safe_id(request_id or uuid.uuid4().hex)}"
    path = directory / f'{name}.json'
    _atomic_json(path, {'content': text, 'request_id': request_id})
    return {'status': 'queued', 'session_id': session_id,
            'turn_id': owner['turn_id'], 'source': owner.get('source', '?'),
            'channel': 'file'}


def send_cancel(cache_root, session_id: str) -> dict | None:
    """请求取消活动回合；没有活动回合返回 None。"""
    owner = active_owner(cache_root, session_id)
    if owner is None:
        return None
    directory = control_dir(cache_root, session_id)
    _atomic_json(directory / 'cancel', {'turn_id': owner['turn_id'],
                                        'ts': time.time()})
    return {'status': 'cancelling', 'session_id': session_id,
            'turn_id': owner['turn_id'], 'source': owner.get('source', '?'),
            'channel': 'file'}


class TurnChannel:
    """回合持有者一侧：写 owner、取 steer、轮询 cancel。"""

    def __init__(self, cache_root, session_id: str, token, *,
                 clock=time.time):
        self.directory = control_dir(cache_root, session_id)
        self.session_id = session_id
        self.token = token
        self._clock = clock
        self._steer_dir = self.directory / 'steer'
        self._cancel_path = self.directory / 'cancel'
        self._last_cancel_check = 0.0
        self._seen_requests: set[str] = set()
        self._started = False

    def start(self) -> 'TurnChannel':
        self.directory.mkdir(parents=True, exist_ok=True)
        self._steer_dir.mkdir(exist_ok=True)
        # 不在这里删除 cancel：旧标记带的是上一回合的 turn_id，轮询时会因不匹配被
        # 忽略；保留它反而消除了「先删标记、后写 owner」之间取消请求被漏掉的窗口。
        self._prune_stale_steer()
        _atomic_json(_owner_path(self.directory), {
            'turn_id': self.token.turn_id,
            'session_id': self.session_id,
            'source': self.token.source,
            'pid': os.getpid(),
            'started_at': self._clock(),
        })
        self._started = True
        return self

    def _prune_stale_steer(self) -> None:
        cutoff = self._clock() - STALE_AFTER
        for path in self._steer_dir.glob('*.json'):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def drain_steer(self) -> list[str]:
        """取走并删除待注入消息；同 request_id 只返回一次。"""
        texts: list[str] = []
        for path in sorted(self._steer_dir.glob('*.json')):
            data = _read_json(path)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            if not data:
                continue
            request_id = data.get('request_id')
            if request_id:
                if request_id in self._seen_requests:
                    continue
                if len(self._seen_requests) < 256:
                    self._seen_requests.add(str(request_id))
            content = data.get('content')
            if isinstance(content, str) and content.strip():
                texts.append(content)
        return texts

    def cancel_requested(self, *, interval: float = CANCEL_POLL_INTERVAL) -> bool:
        """按 TTL 轮询 cancel 文件；只接受属于本回合的 turn_id。"""
        now = self._clock()
        if now - self._last_cancel_check < interval:
            return False
        self._last_cancel_check = now
        data = _read_json(self._cancel_path)
        if not data:
            return False
        if str(data.get('turn_id') or '') != self.token.turn_id:
            return False
        self.token.request_cancel()
        return True

    def close(self) -> None:
        """清理 owner 与取消标记；steer 文件留给下一回合（避免吞消息）。"""
        if not self._started:
            return
        try:
            owner = _read_json(_owner_path(self.directory))
            if owner and owner.get('turn_id') == self.token.turn_id:
                _owner_path(self.directory).unlink(missing_ok=True)
            self._cancel_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._started = False
