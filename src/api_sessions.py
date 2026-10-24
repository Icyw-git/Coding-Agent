"""Session, workspace, and message routes for the HTTP control plane."""

from __future__ import annotations

from agent_runtime import SessionAlreadyExistsError
from api_models import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    ForkSessionRequest,
    RenameSessionRequest,
    SendMessageRequest,
    SessionActionRequest,
    WorkspaceRequest,
)


def build_router(client, runtime):
    from fastapi import APIRouter, HTTPException

    router = APIRouter()

    @router.post("/v1/sessions", status_code=201)
    def create_session(request: CreateSessionRequest):
        try:
            session = client.sessions.create(request.id, workspace=request.workspace)
        except SessionAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": session.id, "history": session.history}

    @router.get('/v1/session-archives')
    def list_archives():
        return {'sessions': runtime.archive.list()}

    @router.post('/v1/session-actions')
    def session_action(request: SessionActionRequest):
        try:
            runtime.manage_session(request.action, request.session)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {'status': 'ok'}

    @router.get("/v1/sessions/search")
    def search_sessions(q: str, limit: int = 10):
        """跨会话内容搜索：命中会话（摘要 + 片段），按最近优先。

        必须注册在 /v1/sessions/{session_id} 之前，否则 'search' 会被当成
        会话 ID 吃掉。
        """
        try:
            rows = client.sessions.search(q, limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"sessions": rows}

    @router.post("/v1/sessions/{session_id}/fork")
    def fork_session(session_id: str, request: ForkSessionRequest):
        try:
            new_id = client.sessions.fork(session_id, request.upto_seq)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"session_id": new_id}

    @router.post("/v1/sessions/{session_id}/title")
    def rename_session(session_id: str, request: RenameSessionRequest):
        """设置会话主题；404=会话不存在，400=标题为空。"""
        try:
            title = client.sessions.rename(session_id, request.title)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"title": title}

    @router.get("/v1/sessions/{session_id}")
    def get_session(session_id: str):
        try:
            session = client.sessions.get(session_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"id": session.id, "history": session.history}

    @router.get("/v1/sessions")
    def list_sessions(limit: int = 20):
        return {"sessions": runtime.list_sessions(limit)}

    @router.get('/v1/sessions/{session_id}/workspace')
    def get_workspace(session_id: str):
        try:
            return {'workspace': runtime.get_workspace(session_id)}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put('/v1/sessions/{session_id}/workspace')
    def set_workspace(session_id: str, request: WorkspaceRequest):
        try:
            return {'workspace': runtime.set_workspace(session_id, request.path)}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get('/v1/workspaces')
    def list_workspaces(current: str | None = None):
        return runtime.list_workspaces(current)

    @router.post('/v1/workspaces', status_code=201)
    def add_workspace(request: CreateWorkspaceRequest):
        try:
            entry = runtime.add_workspace(request.path, request.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {'workspace': entry}

    @router.delete('/v1/workspaces/{workspace_id}')
    def remove_workspace(workspace_id: str):
        try:
            removed = runtime.remove_workspace(workspace_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail='workspace not found') from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {'removed': removed}

    @router.post("/v1/sessions/{session_id}/messages")
    def send_message(session_id: str, request: SendMessageRequest):
        try:
            response = client.messages.create(session_id, request.content)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"session_id": response.session_id, "content": response.content}

    return router
