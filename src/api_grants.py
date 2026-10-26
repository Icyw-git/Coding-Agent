"""目录级持久授权路由：人怎么增删授权，模型只能撤销本会话创建的那些。

授权只能由人给出：这里没有暴露给模型的工具；飞书命令/卡片与 CLI 都走这组
路由。新增/删除写进 ``grants.json``，并向创建者会话追加审计事件。
"""
from __future__ import annotations

from api_models import (
    AddDomainGrantRequest,
    AddGrantRequest,
    RemoveDomainGrantRequest,
    RemoveGrantRequest,
)


def _audit(session_id: str | None, event: str, record: dict, by: str) -> None:
    """授权变更进会话事件流；没有会话或文件不存在时静默跳过。

    审计是观察性的，不能反过来让一次已经落盘的授权变更失败。
    """
    session_id = str(session_id or '').strip()
    if not session_id:
        return
    try:
        import harness

        path = harness.SESSIONS_DIR / f'session_{session_id}.jsonl'
        if not path.is_file():
            return
        harness.SessionEventStore.open(path).append(
            event,
            path=record.get('path', ''),
            scope=record.get('scope', 'write'),
            expires=record.get('expires', ''),
            by=str(by or ''),
            note=record.get('note', ''),
            session_id=session_id,
        )
    except Exception as exc:
        print(f'  \033[33m[grants] failed to record {event}: '
              f'{type(exc).__name__}: {exc}\033[0m')


def _audit_domain(session_id: str | None, event: str, record: dict,
                  by: str) -> None:
    """域名规则的审计：与目录授权同一套事件流，但字段分开。"""
    session_id = str(session_id or '').strip()
    if not session_id:
        return
    try:
        import harness

        path = harness.SESSIONS_DIR / f'session_{session_id}.jsonl'
        if not path.is_file():
            return
        harness.SessionEventStore.open(path).append(
            event,
            host=record.get('host', ''),
            scope=record.get('scope', ''),
            expires=record.get('expires', ''),
            by=str(by or ''),
            note=record.get('note', ''),
            session_id=session_id,
        )
    except Exception as exc:
        print(f'  \033[33m[grants] failed to record {event}: '
              f'{type(exc).__name__}: {exc}\033[0m')


def build_router():
    from fastapi import APIRouter, HTTPException

    import grants

    router = APIRouter()

    @router.get('/v1/grants')
    def list_path_grants():
        return {'grants': grants.list_grants()}

    @router.post('/v1/grants', status_code=201)
    def add_path_grant(request: AddGrantRequest):
        try:
            record = grants.add_path(
                request.path, expires=request.expires, note=request.note or '',
                created_by=request.by, session_id=request.session_id or '')
        except grants.GrantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request.session_id, 'grant_added', record, request.by)
        return record

    @router.post('/v1/grants/remove')
    def remove_path_grant(request: RemoveGrantRequest):
        try:
            record = grants.remove_path(request.path)
        except grants.GrantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail='path grant not found')
        _audit(request.session_id, 'grant_removed', record, request.by)
        return {'removed': record}

    @router.get('/v1/net-grants')
    def list_domain_grants():
        import net_grants

        return {'domains': net_grants.list_rules()}

    @router.post('/v1/net-grants', status_code=201)
    def add_domain_grant(request: AddDomainGrantRequest):
        import net_grants

        try:
            record = net_grants.add(
                request.host, scope=request.scope, expires=request.expires,
                note=request.note or '', created_by=request.by,
                session_id=request.session_id or '')
        except net_grants.NetGrantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:      # NetPolicyError 等：参数不合法
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit_domain(request.session_id, 'domain_added', record, request.by)
        return record

    @router.post('/v1/net-grants/remove')
    def remove_domain_grant(request: RemoveDomainGrantRequest):
        import net_grants

        try:
            record = net_grants.remove(request.host)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail='domain rule not found')
        _audit_domain(request.session_id, 'domain_removed', record, request.by)
        return {'removed': record}

    return router
