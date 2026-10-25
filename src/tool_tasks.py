"""任务与 cron 工具：封装 task_system / cron_scheduler 并统一输出格式。

两个子系统各自持有存储与锁（`.cache/tasks`、`.cache/os_tasks`、crons.json），
本模块只做调用与展示，不复制状态；测试通过 monkeypatch 本模块的依赖名控制行为。
"""
from __future__ import annotations

from typing import List, Optional

import hooks
from cron_scheduler import cancel_job, cron_lock, schedule_job, scheduled_jobs
from task_system import claim_task, complete_task, create_task, get_task, list_tasks


def run_create_task(subject: str, description: str = "",
                    blockedBy: Optional[List[str]] = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "●",
                "completed": "✓"}.get(t.status, "?")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} "
                     f"[{t.status}]{owner}{deps}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    # 模型不能给自己授权副作用：schedule_job 的默认值就是只读；需要写操作
    # 由用户在 /cron add --writes 里显式开启。来源聊天只在飞书回合里有值：
    # 任务结束后结果推回同一处，而不是留在一个没人看的后台会话里。
    deliver_to = hooks.current_origin_chat()
    result = schedule_job(cron, prompt, recurring, durable,
                          deliver_to=deliver_to)
    if isinstance(result, str):
        return f"Error: {result}"
    push = ' → 结果推送到当前聊天' if deliver_to else ''
    return f"Scheduled {result.id}: '{cron}' → {prompt} (read-only){push}"


def run_list_crons() -> str:
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs. Use schedule_cron to add one."
    lines = []
    for j in jobs:
        tag = "recurring" if j.recurring else "one-shot"
        dur = "durable" if j.durable else "session"
        mode = "" if getattr(j, 'allow_side_effects', False) else " (read-only)"
        push = ' → 推送' if getattr(j, 'deliver_to', '') else ''
        lines.append(f"  {j.id}: '{j.cron}' → {j.prompt[:40]} "
                     f"[{tag}, {dur}]{mode}{push}")
    return "\n".join(lines)


def run_cancel_cron(job_id: str) -> str:
    return cancel_job(job_id)
