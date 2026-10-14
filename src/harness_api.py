"""FastAPI adapter for the transport-independent agent runtime."""

from __future__ import annotations

import json
from collections.abc import Iterable

from harness_sdk import HarnessClient
from agent_runtime import SessionAlreadyExistsError


def create_app(runtime=None):
    """Create the HTTP application without starting background services."""
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import StreamingResponse
        from pydantic import BaseModel, Field
    except ImportError as exc:  # optional dependency for CLI-only installs
        raise RuntimeError(
            "FastAPI support requires optional dependencies; install with "
            "`pip install -r requirements-api.txt`."
        ) from exc

    client = HarnessClient(runtime or _default_runtime())

    class CreateSessionRequest(BaseModel):
        id: str | None = Field(default=None, min_length=1)

    class SendMessageRequest(BaseModel):
        content: str = Field(min_length=1)

    app = FastAPI(title="Harness Agent API", version="1.0")

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
