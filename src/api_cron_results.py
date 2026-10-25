"""定时任务结果的投递路由：网关拉取未确认结果，推送成功后 ack。

生成在 ``cron_delivery``（定时回合收尾），这里只暴露 outbox；真正的发送方
是飞书网关。与心跳一致：发送与 ack 不在同一事务内，允许崩溃窗口内重复。
"""
from __future__ import annotations


def build_router():
    from fastapi import APIRouter, HTTPException

    import cron_delivery

    router = APIRouter()

    @router.get('/v1/cron-results')
    def list_results():
        return {'pending': cron_delivery.pending()}

    @router.post('/v1/cron-results/{item_id}/ack')
    def ack_result(item_id: str):
        if not cron_delivery.ack(item_id):
            raise HTTPException(status_code=404,
                                detail=f'cron result {item_id} not pending')
        return {'status': 'acked', 'id': item_id}

    return router
