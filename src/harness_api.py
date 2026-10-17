"""FastAPI adapter for the transport-independent agent runtime."""

from __future__ import annotations

import json
import asyncio
import logging
import os
import queue
import threading
import secrets
import time
import sys
import traceback
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from harness_sdk import HarnessClient
from agent_runtime import AgentRuntime, SessionAlreadyExistsError
from session_store import SessionReplayError
from task_manager import TaskManager
from turn_progress import TurnProgress, format_duration
from hooks import GATE as permission_gate

STREAM_PROGRESS_INTERVAL = float(os.getenv('HARNESS_PROGRESS_INTERVAL', '1'))
STREAM_DIAGNOSTIC_INTERVAL = 60

try:
    from pydantic import BaseModel, Field
except ImportError:  # Keep CLI-only installs importable without API dependencies.
    BaseModel = None
    Field = None

# 请求模型必须放在模块层：端点注解经 `from __future__ import annotations`
# 惰性化后，FastAPI 只在模块全局命名空间解析；若定义在 create_app 内部会
# 解析失败，request 被降级成 query 参数（表现为 422 Field required）。
if BaseModel is not None:
    class CreateSessionRequest(BaseModel):
        id: str | None = Field(default=None, min_length=1)
        workspace: str | None = Field(default=None, min_length=1)


    class SendMessageRequest(BaseModel):
        content: str = Field(min_length=1)

    class SessionActionRequest(BaseModel):
        action: str
        session: dict


    class WorkspaceRequest(BaseModel):
        path: str = Field(min_length=1)


    class CreateTaskRequest(BaseModel):
        command: str = Field(min_length=1)
        cwd: str | None = None


    class ResolvePermissionRequest(BaseModel):
        allow: bool
        by: str = Field(default='remote', min_length=1)
        scope: str | None = None     # 'session' = 本会话同类自动通过
        klass: str | None = None     # 同类判定键（bash 命令首词）


    class ForkSessionRequest(BaseModel):
        upto_seq: int | None = None  # 复制到的事件序号（缺省=全部）


    class RenameSessionRequest(BaseModel):
        title: str = Field(min_length=1)  # 新会话主题


def create_app(runtime=None, *, api_token: str | None = None, rate_limit: int = 60,
               access_log_path: str | None = None, task_root=None, workspace=None):
    """Create the HTTP application without starting background services."""
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
        from starlette.middleware.base import BaseHTTPMiddleware
    except ImportError as exc:  # optional dependency for CLI-only installs
        raise RuntimeError(
            "FastAPI support requires optional dependencies; install with "
            "`pip install -r requirements-api.txt`."
        ) from exc

    token = api_token if api_token is not None else os.getenv("HARNESS_API_TOKEN")
    if not token:
        raise RuntimeError("HARNESS_API_TOKEN must be configured before starting the API")
    if rate_limit < 1:
        raise ValueError("rate_limit must be at least 1")

    owns_runtime = runtime is None
    runtime = _default_runtime() if owns_runtime else runtime
    client = HarnessClient(runtime)
    # 独立命名空间：.cache/tasks 已被 task_system（agent TODO 列表）占用
    task_base = Path(task_root or os.getenv("HARNESS_TASK_ROOT")
                     or Path.cwd() / ".cache" / "os_tasks")
    tasks = TaskManager(
        task_base,
        allowed_root=workspace or os.getenv("HARNESS_WORKSPACE") or Path.cwd(),
    )
    if isinstance(runtime, AgentRuntime):
        runtime.configure_workspace_root(tasks.allowed_root)
    # 服务器模式：权限审批走远程端点（消息网关轮询 /v1/permissions）；
    # CLI 入口不经过 create_app，保持 console 后端不受影响。
    permission_gate.backend = 'remote'

    logger = logging.Logger("harness.api.access", level=logging.INFO)
    access_log_path = access_log_path or os.getenv(
        "HARNESS_API_LOG", ".cache/api/api-access.log"
    )
    @asynccontextmanager
    async def lifespan(app):
        if owns_runtime:
            import harness
        handler = None
        try:
            if access_log_path:
                log_path = Path(access_log_path)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                handler = logging.FileHandler(log_path, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                logger.addHandler(handler)
            if owns_runtime:
                await asyncio.to_thread(harness.initialize_mcp_tools)
            yield
        finally:
            try:
                try:
                    await asyncio.to_thread(tasks.close)
                finally:
                    if owns_runtime:
                        await asyncio.to_thread(harness.shutdown_mcp)
            finally:
                if handler is not None:
                    logger.removeHandler(handler)
                    handler.close()

    app = FastAPI(title="Harness Agent API", version="1.0", lifespan=lifespan)

    request_times: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    class ApiMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            started = time.monotonic()
            status = 500
            path = request.url.path
            client_host = request.client.host if request.client else "unknown"
            if path.startswith("/v1/"):
                auth = request.headers.get("authorization", "")
                supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
                if not secrets.compare_digest(supplied, token):
                    response = JSONResponse(
                        {"detail": "Invalid or missing bearer token"}, status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    status = response.status_code
                else:
                    key = (client_host, path)
                    now = time.monotonic()
                    window = request_times[key]
                    while window and now - window[0] >= 60:
                        window.popleft()
                    if len(window) >= rate_limit:
                        response = JSONResponse(
                            {"detail": "Rate limit exceeded"}, status_code=429,
                            headers={"Retry-After": "60"},
                        )
                        status = response.status_code
                    else:
                        window.append(now)
                        response = await call_next(request)
                        status = response.status_code
            else:
                response = await call_next(request)
                status = response.status_code
            elapsed_ms = (time.monotonic() - started) * 1000
            logger.info("client=%s method=%s path=%s status=%s duration_ms=%.1f",
                        client_host, request.method, path, status, elapsed_ms)
            return response

    app.add_middleware(ApiMiddleware)

    @app.post("/v1/sessions", status_code=201)
    def create_session(request: CreateSessionRequest):
        try:
            session = client.sessions.create(request.id, workspace=request.workspace)
        except SessionAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": session.id, "history": session.history}

    @app.get('/v1/session-archives')
    def list_archives():
        return {'sessions': runtime.archive.list()}

    @app.post('/v1/session-actions')
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

    @app.get("/v1/sessions/search")
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

    @app.post("/v1/sessions/{session_id}/fork")
    def fork_session(session_id: str, request: ForkSessionRequest):
        try:
            new_id = client.sessions.fork(session_id, request.upto_seq)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"session_id": new_id}

    @app.post("/v1/sessions/{session_id}/title")
    def rename_session(session_id: str, request: RenameSessionRequest):
        """设置会话主题；404=会话不存在，400=标题为空。"""
        try:
            title = client.sessions.rename(session_id, request.title)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"title": title}

    @app.get("/v1/sessions/{session_id}")
    def get_session(session_id: str):
        try:
            session = client.sessions.get(session_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"id": session.id, "history": session.history}

    @app.get("/v1/sessions")
    def list_sessions(limit: int = 20):
        return {"sessions": runtime.list_sessions(limit)}

    @app.get('/v1/sessions/{session_id}/workspace')
    def get_workspace(session_id: str):
        try:
            return {'workspace': runtime.get_workspace(session_id)}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put('/v1/sessions/{session_id}/workspace')
    def set_workspace(session_id: str, request: WorkspaceRequest):
        try:
            return {'workspace': runtime.set_workspace(session_id, request.path)}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/sessions/{session_id}/stream")
    def stream_session(session_id: str, after: int = 0, poll: float = 0.3,
                       once: bool = False, follow_seconds: float = 0.0):
        """SSE live monitoring: replay events after ``seq > after``, then follow.

        Emits every raw session event (message / tool_started / compaction /
        turn_completed / ...) as ``event: <type>`` frames, with heartbeat
        comments while idle. ``once=1`` closes right after the replay
        (snapshot pull); ``follow_seconds`` bounds the follow phase (0 = open
        until client disconnect).
        """
        try:
            store = runtime.open_session(session_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        poll = min(max(poll, 0.05), 5.0)
        follow_budget = min(max(follow_seconds, 0.0), 3600.0)

        def render(event: dict) -> str:
            return (f"event: {event.get('type', 'event')}\n"
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n")

        async def follow():
            try:
                history, offset = await asyncio.to_thread(store.tail_events, strict=True)
            except SessionReplayError as exc:
                yield ("event: stream_error\ndata: "
                       + json.dumps({"detail": str(exc)}, ensure_ascii=False)
                       + "\n\n")
                return
            for event in history:
                if int(event.get("seq", 0)) > after:
                    yield render(event)
            if once:
                yield "event: done\ndata: {}\n\n"
                return
            deadline = time.monotonic() + follow_budget if follow_budget else None
            last_beat = time.monotonic()
            while True:
                fresh, offset = await asyncio.to_thread(store.tail_events, offset)
                for event in fresh:
                    if int(event.get("seq", 0)) > after:
                        last_beat = time.monotonic()
                        yield render(event)
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    yield "event: done\ndata: {}\n\n"
                    return
                if now - last_beat >= 15:
                    yield ": ping\n\n"
                    last_beat = now
                await asyncio.sleep(poll)

        return StreamingResponse(
            follow(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/tasks", status_code=201)
    def create_task(request: CreateTaskRequest):
        try:
            record = tasks.create(request.command, request.cwd)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"task": asdict(record)}

    @app.get("/v1/tasks")
    def list_tasks(limit: int = 50):
        return {"tasks": [asdict(record) for record in tasks.list(limit)]}

    @app.get("/v1/tasks/{task_id}")
    def get_task(task_id: str):
        try:
            record = tasks.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        return {"task": asdict(record)}

    @app.post("/v1/tasks/{task_id}/stop")
    def stop_task(task_id: str):
        try:
            record = tasks.stop(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"task": asdict(record)}

    @app.get("/v1/tasks/{task_id}/logs")
    def task_logs(task_id: str, tail: int = 200):
        try:
            tasks.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        return PlainTextResponse(tasks.tail_logs(task_id, tail))

    @app.get("/v1/permissions")
    def list_permissions(limit: int = 20):
        """Pending 审批请求 + 最近历史（消息网关轮询后推送给人工决议）。"""
        return {
            "pending": [req.to_dict() for req in permission_gate.pending()],
            "recent": [req.to_dict() for req in permission_gate.history(limit)],
        }

    @app.post("/v1/permissions/{req_id}/resolve")
    def resolve_permission(req_id: str, request: ResolvePermissionRequest):
        try:
            record = permission_gate.resolve(req_id, request.allow, by=request.by,
                                             scope=request.scope, klass=request.klass)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="permission request not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"request": record.to_dict()}

    @app.post("/v1/sessions/{session_id}/messages")
    def send_message(session_id: str, request: SendMessageRequest):
        try:
            response = client.messages.create(session_id, request.content)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"session_id": response.session_id, "content": response.content}

    @app.post("/v1/sessions/{session_id}/messages/stream")
    def stream_message(session_id: str, request: SendMessageRequest):
        """Run one turn and emit text deltas as SSE frames."""
        deltas: queue.Queue = queue.Queue()
        finished = object()

        def run() -> None:
            started = time.monotonic()
            try:
                opener = getattr(runtime, 'open_session', None)
                if opener is not None:
                    store = opener(session_id)
                    deltas.put(('source', (store, store.path.stat().st_size)))
                response = client.messages.create(
                    session_id, request.content,
                    on_delta=lambda text: deltas.put(("delta", text)),
                )
                duration_ms = round((time.monotonic() - started) * 1000)
                deltas.put(("done", {"session_id": response.session_id,
                                     "content": response.content,
                                     "duration_ms": duration_ms,
                                     "duration": format_duration(duration_ms / 1000)}))
            except Exception as exc:
                logger.exception('streamed agent turn failed session=%s', session_id)
                deltas.put(("error", {'detail': str(exc),
                                       'duration_ms': round((time.monotonic() - started) * 1000),
                                       'duration': format_duration(time.monotonic() - started)}))
            finally:
                deltas.put(("close", finished))

        worker = threading.Thread(target=run, daemon=True, name='agent-api-turn')
        worker.start()

        def events():
            progress = TurnProgress()
            last_progress = last_diagnostic = time.monotonic()
            last_marker = None
            initial = progress.snapshot()
            last_marker = (initial.get('phase'), initial.get('tool'),
                           initial.get('elapsed_text'))
            yield f"event: progress\ndata: {json.dumps(initial)}\n\n"
            while True:
                try:
                    kind, payload = deltas.get(timeout=STREAM_PROGRESS_INTERVAL)
                except queue.Empty:
                    kind, payload = 'heartbeat', None
                if kind == 'source':
                    progress.follow(*payload)
                elif kind == "delta":
                    yield f"event: delta\ndata: {json.dumps({'text': payload}, ensure_ascii=False)}\n\n"
                elif kind == "done":
                    yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                elif kind == "error":
                    yield f"event: error\ndata: {json.dumps({'detail': payload}, ensure_ascii=False)}\n\n"
                elif kind == 'close':
                    return
                now = time.monotonic()
                if kind not in ('done', 'error') and now - last_progress >= STREAM_PROGRESS_INTERVAL:
                    status = progress.snapshot()
                    # 内容没有变化（阶段/工具/秒数都相同）时不重复推送
                    marker = (status.get('phase'), status.get('tool'),
                              status.get('elapsed_text'))
                    if marker != last_marker:
                        yield f"event: progress\ndata: {json.dumps(status)}\n\n"
                        last_marker = marker
                    last_progress = now
                    if now - last_diagnostic >= STREAM_DIAGNOSTIC_INTERVAL:
                        frame = sys._current_frames().get(worker.ident)
                        if frame is not None:
                            logger.warning('agent turn still running session=%s progress=%s\n%s',
                                           session_id, status, ''.join(traceback.format_stack(frame)))
                        last_diagnostic = now

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.get("/v1/sessions/{session_id}/events")
    def stream_events(session_id: str):
        try:
            history = client.sessions.get(session_id).history
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        def events() -> Iterable[str]:
            for message in history:
                yield f"event: {message.get('role', 'message')}\n"
                yield f"data: {json.dumps(message, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return app


def _default_runtime():
    import harness

    return harness.create_runtime(initialize_tools=False)
