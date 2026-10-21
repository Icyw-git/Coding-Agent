"""团队协作工具：派生 teammate、消息收发与 lead 协议（shutdown / plan）。

消息总线与协议状态由 agent_teams / team_protocols 持有；本模块只做调用与输出，
不复制状态，也不依赖入口全局变量。
"""
from __future__ import annotations

from agent_teams import BUS, spawn_teammate_thread
from team_protocols import (
    ProtocolState,
    consume_lead_inbox,
    new_request_id,
    pending_requests,
)


def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    return spawn_teammate_thread(name, role, prompt)


def run_send_message(to: str, content: str) -> str:
    BUS.send('lead', to, content, 'message')
    return f"Sent to {to}"


def run_check_inbox() -> str:
    # 走协议路由：shutdown/plan 响应会更新 pending_requests 状态，其余消息返回注入对话
    msgs = consume_lead_inbox(route_protocol=True)
    if not msgs:
        return "No messages in inbox."
    return "\n".join(f"[from {m['from']} ({m.get('type', 'message')})] {m['content']}"
                     for m in msgs)


def run_request_shutdown(teammate: str) -> str:
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="shutdown",
        sender="lead", target=teammate,
        status="pending", payload="")
    BUS.send("lead", teammate, "Please shut down gracefully.",
             "shutdown_request",
             {"request_id": req_id})
    print(f"  \033[35m[protocol] shutdown_request → {teammate} "
          f"({req_id})\033[0m")
    return f"Shutdown request sent to {teammate} (req: {req_id})"


def run_request_plan(teammate: str, task: str) -> str:
    """Lead asks a teammate to submit a plan for a task."""
    BUS.send("lead", teammate, f"Please submit a plan for: {task}",
             "message")
    return f"Asked {teammate} to submit a plan"


def run_review_plan(request_id: str, approve: bool, feedback: str = "") -> str:
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    if state.status != "pending":
        return f"Request {request_id} already {state.status}"
    state.status = "approved" if approve else "rejected"
    BUS.send("lead", state.sender, feedback or ("Approved" if approve else "Rejected"),
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    icon = "✓" if approve else "✗"
    print(f"  \033[32m[protocol] plan {icon} ({request_id})\033[0m")
    return f"Plan {'approved' if approve else 'rejected'} ({request_id})"
