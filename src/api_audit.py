"""审计查询路由：只读列出安全相关事件。"""
from __future__ import annotations


def build_router():
    from fastapi import APIRouter, HTTPException

    import audit
    import harness

    router = APIRouter()

    @router.get('/v1/audit')
    def list_audit(since: str | None = None, limit: int = audit.DEFAULT_LIMIT,
                   types: str | None = None, session_id: str | None = None):
        """跨会话审计：审批超时、屏幕/剪贴板/桌面操作、授权变更。

        ``types`` 用逗号分隔；未知类型直接 400，避免拼错后静默返回空列表。
        """
        since_ts = audit.since_from(since)
        if since and not since_ts:
            raise HTTPException(status_code=400, detail='invalid since')
        wanted = None
        if types:
            wanted = [item.strip() for item in types.split(',') if item.strip()]
            unknown = sorted(set(wanted) - set(audit.AUDIT_EVENTS))
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f'unknown audit event type(s): {", ".join(unknown)}')
        events = audit.collect(
            getattr(harness, 'SESSIONS_DIR', None),
            since=since_ts, limit=limit, types=wanted,
            session_id=(session_id or None))
        return {'events': events, 'counts': audit.summarize(events),
                'since': since_ts}

    return router
