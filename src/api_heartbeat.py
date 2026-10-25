"""心跳摘要投递路由：网关拉取未确认摘要，推送成功后 ack。

生成在 ``heartbeat``（cron 拦截点），这里只暴露 outbox；真正的发送方是飞书
网关。发送与 ack 不在同一事务内，语义是至少一次 —— 崩溃窗口内允许重复推送。
"""
from __future__ import annotations


def build_router():
    from fastapi import APIRouter, HTTPException

    import heartbeat

    router = APIRouter()

    @router.get('/v1/heartbeat')
    def list_heartbeat():
        return {'pending': heartbeat.pending()}

    @router.post('/v1/heartbeat/{occurrence_id}/ack')
    def ack_heartbeat(occurrence_id: str):
        if not heartbeat.ack(occurrence_id):
            raise HTTPException(status_code=404,
                                detail=f'heartbeat {occurrence_id} not pending')
        return {'status': 'acked', 'occurrence_id': occurrence_id}

    return router
