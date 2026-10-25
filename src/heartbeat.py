"""默认每日只读心跳：确定性摘要 + 本地 outbox，由飞书网关领走投递。

为什么不用 cron 的「prompt → 模型」路径
--------------------------------------
心跳的初始形态是只读巡检（任务板、工作区最近改动、待审批数量），不需要模型
参与。让模型跑一轮会把它变成一次不可预测的写入口，还会占用唯一的全局回合槽；
这里仍复用同一张 cron 表计时，按稳定 job id 在回合执行入口拦截并本地生成。

时区与漏触发
------------
cron 表达式按本机本地时间匹配（``cron_scheduler`` 一直如此，心跳不另建时区）。
进程在触发那一分钟没运行 → 该 occurrence 直接跳过、不补跑：巡检摘要是时效性
内容，迟到几小时没有意义，补跑还会和下一次撞在一起。触发标记（``last_fired``）
随 durable job 落盘，同一分钟重启不会重复触发；occurrence id（精确到触发分钟）
再做一层去重。

投递语义
--------
至少一次：摘要先写 outbox 再记 state；网关发送成功后才 ack。发送与 ack 之间
崩溃会重复推送一次，但不会漏发。outbox 有上限，长期离线的网关不会无限堆积
历史摘要（只保留最近几次）。

开关：``HARNESS_HEARTBEAT=0`` 关闭注册；``HARNESS_HEARTBEAT_CRON`` 覆盖默认
表达式（五段，本机时间）。用户手动删除 heartbeat 任务后，下次服务启动会按
稳定 ID 重新注册 —— 默认任务不该因为一次误删就永久消失。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import cron_scheduler
import paths

JOB_ID = 'heartbeat'
DEFAULT_CRON = '0 9 * * *'          # 每天 09:00（本机时间）
PROMPT = '[heartbeat] 每日只读摘要（本地生成，不经过模型）'
OUTBOX_LIMIT = 7                     # 离线网关最多补发最近几次摘要
STATUS_TAIL_LIMIT = 500              # 任务板只读最近 500 个任务文件

STATE_PATH = paths.cache_path('heartbeat', 'state.json')
OUTBOX_PATH = paths.cache_path('heartbeat', 'outbox.json')

_LOCK = threading.RLock()


# ---------------------------------------------------------------- 注册 / 开关

def enabled() -> bool:
    raw = (os.getenv('HARNESS_HEARTBEAT') or '').strip().casefold()
    return raw not in ('0', 'false', 'no', 'off')


def cron_expr() -> str:
    return (os.getenv('HARNESS_HEARTBEAT_CRON') or '').strip() or DEFAULT_CRON


def ensure_registered(cron: str | None = None):
    """按稳定 ID 注册默认心跳；已存在时原样返回（保留用户改过的表达式）。

    返回 CronJob 或 None（关闭/注册失败）。重复调用幂等：重启不会多出一份。
    """
    if not enabled():
        return None
    with cron_scheduler.cron_lock:
        existing = cron_scheduler.scheduled_jobs.get(JOB_ID)
    if existing is not None:
        return existing
    job = cron_scheduler.schedule_job(cron or cron_expr(), PROMPT,
                                      recurring=True, durable=True,
                                      job_id=JOB_ID)
    if isinstance(job, str):
        # 另一个入口在检查与注册之间抢先注册：读回那一份，不重复添加。
        with cron_scheduler.cron_lock:
            return cron_scheduler.scheduled_jobs.get(JOB_ID)
    return job


# ---------------------------------------------------------------- 摘要

def occurrence_id(job, now: datetime | None = None) -> str:
    """稳定 occurrence：优先用 tick 写下的触发分钟，退化为当前分钟。

    ``job.last_fired`` 是 ``YYYY-MM-DD HH:MM``；队列消费晚几十秒也不会改变
    occurrence，因此重启/重放不会生成第二份摘要。
    """
    now = now or datetime.now()
    marker = getattr(job, 'last_fired', '') or now.strftime('%Y-%m-%d %H:%M')
    return f'{JOB_ID}:{marker.replace(" ", "T")}'


def build_summary(now: datetime | None = None, *, workspace=None,
                  task_root=None, pending=None) -> str:
    """只读汇总：任务板计数、工作区改动、待审批数量。

    不包含命令正文、凭据或工具参数：待审批只按工具名计数，最近改动只取
    ``git status`` 的计数与最后一条提交标题。
    """
    now = now or datetime.now()
    lines = [f'🫀 每日摘要 · {_stamp(now)}',
             _task_summary(task_root),
             _workspace_summary(workspace),
             _pending_summary(pending)]
    return '\n'.join(lines)


def _stamp(now: datetime) -> str:
    local = now.astimezone()
    zone = local.strftime('%Z') or local.strftime('UTC%z')
    return f'{local.strftime("%Y-%m-%d %H:%M")}（{zone}）'


def _task_root(task_root) -> Path:
    return Path(task_root or os.getenv('HARNESS_TASK_ROOT')
                or paths.cache_path('os_tasks'))


def _task_summary(task_root) -> str:
    root = _task_root(task_root)
    if not root.is_dir():
        return '任务板：无后台任务'
    counts: dict[str, int] = {}
    paths_list = sorted(root.glob('task_*.json'))[-STATUS_TAIL_LIMIT:]
    for path in paths_list:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue   # 外来的/损坏的文件不是我们的任务：跳过
        if not isinstance(data, dict):
            continue
        status = str(data.get('status') or 'unknown')
        counts[status] = counts.get(status, 0) + 1
    total = sum(counts.values())
    if not total:
        return '任务板：无后台任务'
    running = counts.get('running', 0) + counts.get('queued', 0)
    failed = counts.get('failed', 0)
    done = counts.get('completed', 0)
    other = total - running - failed - done
    return f'任务板：运行 {running} · 失败 {failed} · 完成 {done} · 其他 {other}（共 {total}）'


def _workspace_summary(workspace) -> str:
    root = Path(workspace or os.getenv('HARNESS_WORKSPACE') or Path.cwd())
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')   # 只读：不刷新索引
    try:
        status = subprocess.run(
            ['git', '-C', str(root), 'status', '--porcelain', '--branch'],
            capture_output=True, text=True, timeout=5, env=env,
            encoding='utf-8', errors='replace')
    except (OSError, subprocess.SubprocessError):
        return f'最近改动：{root.name} 不是 git 工作区'
    if status.returncode != 0:
        return f'最近改动：{root.name} 不是 git 工作区'
    rows = [row for row in status.stdout.splitlines() if row.strip()]
    branch = ''
    if rows and rows[0].startswith('## '):
        branch = rows.pop(0)[3:].split('...')[0].strip()
    changed = sum(1 for row in rows if not row.startswith('??'))
    untracked = sum(1 for row in rows if row.startswith('??'))
    parts = [root.name + (f' @ {branch}' if branch else ''),
             f'修改 {changed} · 未跟踪 {untracked}']
    commit = _last_commit(root, env)
    if commit:
        parts.append(f'最近提交 {commit}')
    return '最近改动：' + ' · '.join(parts)


def _last_commit(root: Path, env: dict) -> str:
    try:
        result = subprocess.run(
            ['git', '-C', str(root), '--no-pager', 'log', '-1', '--pretty=%h %s'],
            capture_output=True, text=True, timeout=5, env=env,
            encoding='utf-8', errors='replace')
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout.strip()[:80] if result.returncode == 0 else ''


def _pending_summary(pending=None) -> str:
    if pending is None:
        try:
            import hooks
            pending = hooks.GATE.pending()
        except Exception:
            pending = []
    if not pending:
        return '待审批：无'
    tally: dict[str, int] = {}
    for item in pending:
        tool = (item.get('tool') if isinstance(item, dict)
                else getattr(item, 'tool', '')) or '?'
        tally[str(tool)] = tally.get(str(tool), 0) + 1
    detail = ' · '.join(f'{tool} {count}' for tool, count in sorted(tally.items()))
    return f'待审批：{len(pending)} 项（{detail}）'


# ---------------------------------------------------------------- 生成 / 投递

def run_due(job, now: datetime | None = None, *, workspace=None,
            task_root=None) -> dict:
    """为一个到点的心跳 job 生成摘要并放进 outbox；同一 occurrence 只生成一次。"""
    occurrence = occurrence_id(job, now)
    with _LOCK:
        state = _read_state()
        if state.get('last_occurrence') == occurrence:
            return {'occurrence': occurrence, 'skipped': 'already-generated'}
        text = build_summary(now, workspace=workspace, task_root=task_root)
        queue_summary(occurrence, text)
        _write_state({'last_occurrence': occurrence,
                      'generated_at': time.time()})
    print(f'  \033[35m[heartbeat] {occurrence} queued '
          f'({len(text)} chars)\033[0m')
    return {'occurrence': occurrence, 'text': text}


def queue_summary(occurrence: str, text: str) -> None:
    """把摘要放进 outbox（原子替换）；同一 occurrence 不重复入队。"""
    with _LOCK:
        data = _read_outbox()
        if any(item.get('occurrence_id') == occurrence for item in data['pending']):
            return
        data['pending'].append({'occurrence_id': occurrence,
                                'text': text,
                                'created_at': time.time()})
        data['pending'] = data['pending'][-OUTBOX_LIMIT:]
        _write_outbox(data)


def pending() -> list[dict]:
    """尚未被网关 ack 的摘要（GET /v1/heartbeat）。"""
    with _LOCK:
        return [dict(item) for item in _read_outbox()['pending']]


def ack(occurrence: str) -> bool:
    """网关发送成功后确认；未知/已确认的 occurrence 返回 False（幂等）。"""
    with _LOCK:
        data = _read_outbox()
        kept = [item for item in data['pending']
                if item.get('occurrence_id') != occurrence]
        if len(kept) == len(data['pending']):
            return False
        data['pending'] = kept
        _write_outbox(data)
        return True


# ---------------------------------------------------------------- 状态文件

def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    tmp.replace(path)


def _read_state() -> dict:
    return _read_json(STATE_PATH)


def _write_state(state: dict) -> None:
    _write_json(STATE_PATH, state)


def _read_outbox() -> dict:
    data = _read_json(OUTBOX_PATH)
    items = data.get('pending')
    if not isinstance(items, list):
        return {'version': 1, 'pending': []}
    cleaned = [item for item in items
               if isinstance(item, dict) and item.get('occurrence_id')
               and item.get('text')]
    return {'version': 1, 'pending': cleaned}


def _write_outbox(data: dict) -> None:
    _write_json(OUTBOX_PATH, {'version': 1, 'pending': data['pending']})
