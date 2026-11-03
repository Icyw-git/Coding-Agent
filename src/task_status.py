"""任务状态的中文名。

仓库里并存两套任务状态词表，别把它们当成一套：

    background_task  running / completed / failed / cancelled
    task_manager     queued / running / succeeded / failed / cancelled / interrupted

以前中文对照表只写在飞书卡片里、且只覆盖第一套，于是 `TaskRecord` 最常见的
`succeeded`（跑成功了）和 `interrupted`（被中断）在卡片上原样印英文。

这里只放「状态 → 中文」，不做判断：哪些状态算终态是 `background_task.
TERMINAL_STATUSES` 的事，哪些算活跃是各自状态机的事。
"""

from __future__ import annotations

LABELS = {
    # background_task
    'running': '运行中',
    'completed': '已完成',
    # task_manager
    'queued': '排队中',
    'succeeded': '成功',
    'interrupted': '已中断',
    # 两套共用
    'failed': '失败',
    'cancelled': '已取消',
}


def zh(status) -> str:
    """状态的中文名；不认识就返回原值（猜一个会让人以为程序认得它）。"""
    key = str(status or '').strip().lower()
    if not key:
        return '未知'
    return LABELS.get(key, key)
