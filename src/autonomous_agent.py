"""自主 teammate 空闲行为：轮询收件箱（协议 + 普通消息）+ 自动认领任务系统的待办任务。"""
import json
import time

from task_system import TASKS_DIR, can_start, claim_task, load_task
from team_protocols import handle_inbox_message
from worktree import WORKTREES_DIR

IDLE_POLL_INTERVAL = 5
IDLE_TIMEOUT = 60


def scan_unclaimed_tasks() -> list:
    unclaimed = []
    for f in sorted(TASKS_DIR.glob('task_*.json')):
        task = json.loads(f.read_text()) # 读取任务 JSON 内容
        if (task.get('status') == 'pending' and not task.get('owner')
                and can_start(task['id'])): # 如果任务是待办状态且未被认领且可以开始
            unclaimed.append(task)
    return unclaimed


def idle_poll(name: str, messages: list, role: str, wt_ctx: dict = None) -> str:
    """空闲轮询一轮：
    - 收 inbox：shutdown→'shutdown'；plan 决定/Lead 消息→'work'；
    - 自动认领待办任务，成功→'work'（任务绑定了 worktree 则切换 wt_ctx）；
    - IDLE_TIMEOUT 内无任何事→'timeout'。"""
    # 延迟导入避免 agent_teams ↔ autonomous_agent 的模块级循环。
    from agent_teams import ack_inbox, claim_inbox

    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)

        # 收 inbox：先 claim，注入对话后再 ack；未确认消息在下一轮/重启后重放。
        inbox = claim_inbox(name)
        new_content = False
        non_protocol = []
        for msg in inbox:
            result = handle_inbox_message(name, msg, messages)
            if result == 'shutdown':
                ack_inbox(name, inbox)
                return 'shutdown'
            elif result == 'awake':
                new_content = True
            else:
                non_protocol.append(msg)
        if non_protocol:
            messages.append({
                'role': 'user',
                'content': f"<inbox>{json.dumps(non_protocol, ensure_ascii=False)}</inbox>",
            })
            new_content = True
        if new_content:
            ack_inbox(name, inbox)
            return 'work'

        # 自动认领任务系统里的待办任务
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task = unclaimed[0]
            result = claim_task(task['id'], name)
            if 'Claimed' in result:
                if wt_ctx is not None:
                    try:
                        claimed = load_task(task['id'])
                        wt_ctx['path'] = str(WORKTREES_DIR / claimed.worktree) if claimed.worktree else None
                    except Exception:
                        wt_ctx['path'] = None
                messages.append({
                    'role': 'user',
                    'content': f"<auto-claimed>Task {task['id']}: {task['subject']}</auto-claimed>",
                })
                print(f"  \033[32m[idle] {name} auto-claimed: {task['subject']}\033[0m")
                return 'work'
            print(f"  \033[33m[idle] {name} claim failed: {result}\033[0m")
    print(f"  \033[31m[idle] {name} timeout ({IDLE_TIMEOUT}s)\033[0m")
    return 'timeout'
