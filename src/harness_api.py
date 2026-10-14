"""FastAPI adapter for the transport-independent agent runtime."""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from pathlib import Path

from harness_sdk import HarnessClient
from agent_runtime import SessionAlreadyExistsError

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


    class SendMessageRequest(BaseModel):
        content: str = Field(min_length=1)


def create_app(runtime=None, *, api_token: str | None = None, rate_limit: int = 60,
               access_log_path: str | None = None):
    """Create the HTTP application without starting background services."""
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import JSONResponse, StreamingResponse
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

    client = HarnessClient(runtime or _default_runtime())

    app = FastAPI(title="Harness Agent API", version="1.0")

    logger = logging.getLogger(f"harness.api.access.{id(app)}")
    access_log_path = access_log_path or os.getenv(
        "HARNESS_API_LOG", ".cache/api/api-access.log"
    )
    if access_log_path:
        log_path = Path(access_log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

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
            session = client.sessions.create(request.id)
        except SessionAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": session.id, "history": session.history}

    @app.get("/v1/sessions/{session_id}")
    def get_session(session_id: str):
        try:
            session = client.sessions.get(session_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"id": session.id, "history": session.history}

    @app.post("/v1/sessions/{session_id}/messages")
    def send_message(session_id: str, request: SendMessageRequest):
        try:
            response = client.messages.create(session_id, request.content)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"session_id": response.session_id, "content": response.content}

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

    return harness.create_runtime()
