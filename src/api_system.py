"""系统状态路由：给网关/客户端判断本机是否空闲、是否锁屏。"""
from __future__ import annotations


def build_router():
    from fastapi import APIRouter

    import idle

    router = APIRouter()

    @router.get('/v1/system/state')
    def system_state():
        return {'idle': idle.state()}

    return router
