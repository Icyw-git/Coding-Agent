"""Session, workspace, and message routes for the HTTP control plane."""

from __future__ import annotations

import paths
import turn_screenshots
from agent_runtime import SessionAlreadyExistsError
from api_models import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    ForkSessionRequest,
    RenameSessionRequest,
    SendMessageRequest,
    SessionActionRequest,
    SteerRequest,
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
        from session_locks import SessionBusy

        try:
            new_id = client.sessions.fork(session_id, request.upto_seq)
        except SessionBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"session_id": new_id}

    @router.post("/v1/sessions/{session_id}/title")
    def rename_session(session_id: str, request: RenameSessionRequest):
        """设置会话主题；404=会话不存在，400=标题为空，409=会话正忙。"""
        from session_locks import SessionBusy

        try:
            title = client.sessions.rename(session_id, request.title)
        except SessionBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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

    @router.post("/v1/sessions/{session_id}/steer")
    def steer_session(session_id: str, request: SteerRequest):
        """活动回合中插入消息；空闲时降级为普通消息；同 request_id 幂等。"""
        try:
            return runtime.steer(session_id, request.content,
                                 request_id=request.request_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/v1/sessions/{session_id}/interrupt")
    def interrupt_session(session_id: str):
        """请求取消本进程内该会话的活动回合；无活动回合返回 idle。"""
        try:
            return runtime.interrupt(session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/v1/sessions/{session_id}/messages")
    def send_message(session_id: str, request: SendMessageRequest):
        from agent_runtime import TurnCancelled
        from turn_coordinator import TurnBusy

        store = None
        before = 0
        try:
            opener = getattr(runtime, 'open_session', None)
            if opener is not None:
                store = opener(session_id)
                before = turn_screenshots.offset(store)
            response = client.messages.create(session_id, request.content,
                                              origin_chat=request.origin_chat)
        except TurnCancelled as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TurnBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"session_id": response.session_id, "content": response.content,
                "screenshots": turn_screenshots.collect_tail(store, before)}

    @router.get("/v1/sessions/{session_id}/screenshots/{name}")
    def download_screenshot(session_id: str, name: str):
        from fastapi.responses import FileResponse

        opener = getattr(runtime, 'open_session', None)
        if opener is not None:
            try:
                opener(session_id)
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not turn_screenshots.valid_name(name):
            raise HTTPException(status_code=400, detail='invalid screenshot name')
        target = paths.cache_path('screenshots') / name
        if target.is_symlink() or not target.is_file():
            raise HTTPException(status_code=404, detail='screenshot not found')
        return FileResponse(target, media_type='image/png',
                            headers={'Cache-Control': 'private, max-age=300'})

    return router
