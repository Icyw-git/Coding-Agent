"""团队协议：邮箱总线 + 协议状态机（shutdown / plan_approval）。

BUS 由 agent_teams 持有，这里通过 _bus() 延迟引用，避免模块级循环导入
（agent_teams 会在模块级 import 本模块）。
"""
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


def _bus():
    """延迟取 agent_teams.BUS：模块级不 import agent_teams，只在调用时取。"""
    from agent_teams import BUS
    return BUS


@dataclass
class ProtocolState:
    request_id: str
    type: Literal['shutdown', 'plan_approval']
    sender: str
    target: str
    status: Literal['pending', 'approved', 'rejected']
    payload: str
    created_at: float = field(default_factory=time.time)


pending_requests: dict[str, ProtocolState] = {}


def new_request_id() -> str:
    return f'req_{random.randint(0, 999999):06d}'


def handle_inbox_message(name, msg, messages):
    """teammate 侧协议处理：
    - shutdown_request → 批准并回 shutdown_response，返回 True（应停止循环）；
    - plan_approval_response → 把 [Plan approved/rejected] 注入 messages，返回 False。
    """
    msg_type = msg.get('type', 'message')
    meta = msg.get('metadata', {})
    req_id = meta.get('request_id', '')

    if msg_type == 'shutdown_request':
        _bus().send(name, 'lead', 'Shutting down gracefully.', 'shutdown_response',
                    {'request_id': req_id, 'approve': True})   # approve=True：同意关闭
        print(f"  \033[35m[protocol] {name} approved shutdown ({req_id})\033[0m")
        return 'shutdown'

    if msg_type == 'plan_approval_response':
        approve = meta.get('approve', False)
        feedback = msg.get('content', '')
        messages.append({
            'role': 'user',
            'content': '[Plan approved] Proceed with the plan.' if approve
                       else f'[Plan rejected] Feedback: {feedback}',
        })
        return 'awake'
    return 'message'


def match_response(response_type, request_id, approve):
    state = pending_requests.get(request_id)
    if not state:
        return
    if state.type == 'shutdown' and response_type != 'shutdown_response':
        return
    if state.type == 'plan_approval' and response_type != 'plan_approval_response':
        return
    if state.status != 'pending':
        return
    state.status = 'approved' if approve else 'rejected'


def consume_lead_inbox(route_protocol=True) -> list:
    """Lead 收件箱：路由协议响应（更新 pending_requests 状态），返回全部消息供注入对话。"""
    msgs = _bus().read_inbox('lead')
    if route_protocol:
        for msg in msgs:
            meta = msg.get('metadata', {})
            req_id = meta.get('request_id', '')
            msg_type = msg.get('type', '')
            if req_id and msg_type.endswith('_response'):
                match_response(msg_type, req_id, meta.get('approve', False))
    return msgs
