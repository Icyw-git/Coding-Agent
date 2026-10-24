"""Permission and execution-policy routes for the HTTP control plane."""

from __future__ import annotations

from dataclasses import asdict

from api_models import PolicyRequest, ResolvePermissionRequest, RevokePermissionRequest
from exec_policy import load_policy, set_tier


def build_router(permission_gate):
    from fastapi import APIRouter, HTTPException

    router = APIRouter()

    @router.get("/v1/permissions")
    def list_permissions(limit: int = 20):
        """Pending 审批请求 + 最近历史（消息网关轮询后推送给人工决议）。"""
        return {
            "pending": [req.to_dict() for req in permission_gate.pending()],
            "recent": [req.to_dict() for req in permission_gate.history(limit)],
        }

    @router.post("/v1/permissions/{req_id}/resolve")
    def resolve_permission(req_id: str, request: ResolvePermissionRequest):
        try:
            record = permission_gate.resolve(req_id, request.allow, by=request.by,
                                             scope=request.scope)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="permission request not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"request": record.to_dict()}

    @router.post("/v1/permissions/revoke")
    def revoke_permissions(request: RevokePermissionRequest):
        """撤销一个会话的预授权；未指定会话时撤销全部。"""
        return {"revoked": permission_gate.revoke_session(request.session_id)}

    @router.get('/v1/policy')
    def get_policy():
        """当前执行/审批档位；消息网关的 /sandbox 读这里。"""
        return {'policy': asdict(load_policy())}

    @router.post('/v1/policy')
    def change_policy(request: PolicyRequest):
        """运行时切档（写进服务端环境，对所有会话与后续工具调用立即生效）。"""
        previous = load_policy()
        try:
            policy = set_tier(sandbox_mode=request.sandbox_mode,
                              approval_policy=request.approval_policy)
        except ValueError as exc:      # 打错档位名：400 说清可选值，绝不静默回落
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if policy != previous:
            permission_gate.revoke_session()
        return {'policy': asdict(policy)}

    return router
