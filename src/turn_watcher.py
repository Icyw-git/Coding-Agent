"""回合完成/失败/取消的主动通知（飞书 watcher 的核心逻辑）。

设计约束（见 docs/PERSONAL_AGENT_PLAN.md 阶段 1）：

- 只消费增量：每个会话保存 ``cursor``（已处理的最大 ``seq``）；
- 终态只认 ``turn_terminal``（阶段 1 引入），不靠“长时间没进展”推断失败；
- 审批超时（``approval_timeout``，由审批门观察者写入）单独推送一次：远程端要能
  区分“用户拒绝”与“无人应答”；同样按 request id 去重；
- 投递语义是 **至少一次**：先发送再落盘状态，崩溃窗口内允许重复，不允许漏发；
- 按 ``(session_id, turn_id, status)`` 去重，重放事件不会重复通知；
- 来源过滤：``source='feishu'`` 的回合已经在手机上流式展示过，不再主动推；
- 无进展只提示“仍在运行，可能卡住”，不伪造失败。

状态文件默认 ``<cache>/turn_watch.json``，原子替换；与 ``feishu_bindings.json`` 分开，
避免破坏后者严格的 ``chat_id -> session_id`` 结构。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

STATE_VERSION = 1
MAX_NOTIFIED_PER_SESSION = 200
MAX_ATTEMPTS_PER_SESSION = 200


class WatchState:
    """游标 + 已通知集合 + 发送尝试记录；线程安全、原子落盘。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding='utf-8')
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, dict) or data.get('version') != STATE_VERSION:
            return
        sessions = data.get('sessions')
        if not isinstance(sessions, dict):
            return
        cleaned = {}
        for session_id, entry in sessions.items():
            if not isinstance(session_id, str) or not isinstance(entry, dict):
                continue
            cursor = entry.get('cursor', 0)
            notified = entry.get('notified')
            attempts = entry.get('attempts')
            cleaned[session_id] = {
                'cursor': int(cursor) if isinstance(cursor, int) else 0,
                'notified': dict(notified) if isinstance(notified, dict) else {},
                'attempts': dict(attempts) if isinstance(attempts, dict) else {},
            }
        self._sessions = cleaned

    def cursor(self, session_id: str) -> int:
        with self._lock:
            return int(self._sessions.get(session_id, {}).get('cursor', 0))

    def notified(self, session_id: str, key: str) -> bool:
        with self._lock:
            return key in self._sessions.get(session_id, {}).get('notified', {})

    def attempts(self, session_id: str, key: str) -> int:
        with self._lock:
            return int(self._sessions.get(session_id, {}).get('attempts', {})
                       .get(key, 0))

    def _entry(self, session_id: str) -> dict:
        entry = self._sessions.setdefault(
            session_id, {'cursor': 0, 'notified': {}, 'attempts': {}})
        return entry

    def prune(self, session_id: str) -> None:
        """限制单会话状态大小；旧通知记录按时间戳截断。"""
        with self._lock:
            entry = self._sessions.get(session_id)
            if not entry:
                return
            for field, limit in (('notified', MAX_NOTIFIED_PER_SESSION),
                                 ('attempts', MAX_ATTEMPTS_PER_SESSION)):
                items = entry.get(field)
                if isinstance(items, dict) and len(items) > limit:
                    ordered = sorted(items.items(), key=lambda kv: kv[1])
                    keep = dict(ordered[-limit:])
                    entry[field] = keep

    def commit(self, session_id: str, *, cursor: int, notified_key: str | None,
               notified_at: float | None = None,
               attempt_key: str | None = None) -> None:
        """把游标/已通知/尝试一次性写盘（原子替换）。"""
        with self._lock:
            entry = self._entry(session_id)
            entry['cursor'] = max(int(entry.get('cursor', 0)), int(cursor))
            if notified_key:
                entry['notified'][notified_key] = notified_at or time.time()
            if attempt_key:
                attempts = entry.setdefault('attempts', {})
                attempts[attempt_key] = attempts.get(attempt_key, 0) + 1
            self.prune(session_id)
            self._save_locked()

    def bump_attempt(self, session_id: str, key: str) -> None:
        """发送前记录尝试（用于诊断重复投递窗口）。"""
        with self._lock:
            entry = self._entry(session_id)
            attempts = entry.setdefault('attempts', {})
            attempts[key] = attempts.get(key, 0) + 1
            self.prune(session_id)
            self._save_locked()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'version': STATE_VERSION, 'sessions': self._sessions}
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding='utf-8')
        temp.replace(self.path)


class TurnWatcher:
    """轮询各绑定会话的增量事件，把终态转成通知。"""

    def __init__(self, state: WatchState, *, fetch, notify,
                 sessions=None, on_missing=None,
                 poll_interval: float = 1.0,
                 stalled_after: float = 300.0, clock=time.time) -> None:
        self.state = state
        self._fetch = fetch               # (session_id, after) -> (events, next_after)
        self._notify = notify             # (session_id, kind, payload) -> None，失败抛异常
        self._sessions = sessions         # () -> list[str]；None 表示由调用方驱动
        self._on_missing = on_missing     # (session_id) -> None：绑定指向的会话已不存在
        self._missing: set[str] = set()
        self.poll_interval = max(0.2, poll_interval)
        self.stalled_after = stalled_after
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_turns: dict[str, dict] = {}   # turn_id -> {session, started_at}

    # ---------- 状态 ----------

    def _key(self, session_id: str, turn_id: str, status: str) -> str:
        return f'{session_id}:{turn_id}:{status}'

    def _session_ids(self) -> list[str]:
        if callable(self._sessions):
            try:
                items = [str(item) for item in self._sessions()]
            except Exception:
                return []
        else:
            items = list(self._sessions or [])
        return [item for item in items if item not in self._missing]

    # ---------- 单次扫描（可测试入口） ----------

    def poll_once(self, session_id: str) -> dict:
        """拉一次增量；返回统计信息（不抛异常，除非状态写盘失败）。"""
        if session_id in self._missing:
            return {'session': session_id, 'error': 'missing', 'notified': 0}
        after = self.state.cursor(session_id)
        try:
            events, next_after = self._fetch(session_id, after)
        except FileNotFoundError:
            # 绑定指向的会话已被删除/归档，或换了数据目录：只报一次，之后不再轮询，
            # 避免每秒打一条 404。真正的清理由 on_missing（解除绑定）负责。
            self._missing.add(session_id)
            if self._on_missing is not None:
                try:
                    self._on_missing(session_id)
                except Exception as exc:
                    print(f'[watch] missing-session handler failed: {exc}')
            return {'session': session_id, 'error': 'missing', 'notified': 0}
        except Exception as exc:
            return {'session': session_id, 'error': type(exc).__name__, 'notified': 0}
        notified = 0
        latest = after
        for event in events:
            seq = int(event.get('seq') or 0)
            latest = max(latest, seq)
            kind = event.get('type')
            if kind == 'turn_started':
                turn_id = str(event.get('turn_id') or '')
                if turn_id:
                    self._started_turns[turn_id] = {
                        'session': session_id,
                        'started_at': float(event.get('ts') or self._clock()),
                    }
                continue
            if kind == 'approval_timeout':
                # 审批无人应答：与回合终态独立的一次性通知。事件可能属于已经
                # 结束的回合（超时后回合继续跑完），所以不依赖 _started_turns。
                request_id = str(event.get('request_id') or f'seq{seq}')
                key = f'{session_id}:approval:{request_id}'
                if self.state.notified(session_id, key):
                    continue
                self.state.bump_attempt(session_id, key)
                try:
                    self._notify(session_id, 'approval_timeout', event)
                except Exception:
                    # 与终态一致：发送失败不推进游标，下一轮重试（至少一次）
                    return {'session': session_id, 'notified': notified,
                            'error': 'notify_failed'}
                self.state.commit(session_id, cursor=latest, notified_key=key)
                notified += 1
                continue
            if kind != 'turn_terminal':
                continue
            turn_id = str(event.get('turn_id') or '')
            status = str(event.get('status') or 'completed')
            if not turn_id:
                continue
            self._started_turns.pop(turn_id, None)
            if event.get('source') == 'feishu':
                continue   # 手机上已经流式看过这个回合
            key = self._key(session_id, turn_id, status)
            if self.state.notified(session_id, key):
                continue
            self.state.bump_attempt(session_id, key)
            try:
                self._notify(session_id, 'terminal', event)
            except Exception:
                # 发送失败：不推进游标，下一次轮询重试（至少一次）
                return {'session': session_id, 'notified': notified,
                        'error': 'notify_failed'}
            self.state.commit(session_id, cursor=latest, notified_key=key)
            notified += 1
        next_after = max(latest, int(next_after or 0))
        if next_after != self.state.cursor(session_id):
            self.state.commit(session_id, cursor=next_after, notified_key=None)
        return {'session': session_id, 'notified': notified, 'cursor': next_after}

    def check_stalled(self, session_id: str | None = None) -> int:
        """对“已开始但长时间没有终态”的回合发一次提示（不推断失败）。"""
        now = self._clock()
        sent = 0
        for turn_id, info in list(self._started_turns.items()):
            if session_id is not None and info['session'] != session_id:
                continue
            if now - info['started_at'] < self.stalled_after:
                continue
            key = self._key(info['session'], turn_id, 'stalled')
            if self.state.notified(info['session'], key):
                continue
            self.state.bump_attempt(info['session'], key)
            try:
                self._notify(info['session'], 'stalled',
                             {'turn_id': turn_id,
                              'elapsed_s': round(now - info['started_at'])})
            except Exception:
                continue
            self.state.commit(info['session'], cursor=self.state.cursor(info['session']),
                              notified_key=key)
            sent += 1
        return sent

    def poll_all(self) -> list[dict]:
        results = [self.poll_once(session_id) for session_id in self._session_ids()]
        self.check_stalled()
        return results

    # ---------- 后台循环 ----------

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop = threading.Event()

        def loop():
            while not self._stop.wait(self.poll_interval):
                try:
                    self.poll_all()
                except Exception as exc:   # 单轮失败不影响后续
                    print(f'[watch] poll failed: {type(exc).__name__}: {exc}')

        self._thread = threading.Thread(target=loop, name='turn-watcher',
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        return thread is None or not thread.is_alive()
