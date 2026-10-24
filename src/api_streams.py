"""SSE routes for live session following and streamed turns."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import time
import traceback
from collections.abc import Iterable

from api_models import SendMessageRequest
from session_store import SessionReplayError
from turn_progress import TurnProgress, format_duration

STREAM_PROGRESS_INTERVAL = float(os.getenv('HARNESS_PROGRESS_INTERVAL', '1'))
STREAM_DIAGNOSTIC_INTERVAL = 60


def build_router(client, runtime, logger):
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import StreamingResponse

    router = APIRouter()

    @router.get("/v1/sessions/{session_id}/stream")
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

    @router.post("/v1/sessions/{session_id}/messages/stream")
    def stream_message(session_id: str, request: SendMessageRequest):
        """Run one turn and emit text deltas as SSE frames."""
        from agent_runtime import TurnCancelled
        from turn_coordinator import TurnBusy

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
            except TurnCancelled as exc:
                deltas.put(("cancelled", {'detail': str(exc),
                                          'duration_ms': round((time.monotonic() - started) * 1000),
                                          'duration': format_duration(time.monotonic() - started)}))
            except TurnBusy as exc:
                deltas.put(("error", {'detail': str(exc), 'code': 'turn_busy',
                                       'duration_ms': round((time.monotonic() - started) * 1000),
                                       'duration': format_duration(time.monotonic() - started)}))
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
                elif kind == "cancelled":
                    yield ("event: cancelled\ndata: "
                           + json.dumps(payload, ensure_ascii=False) + "\n\n")
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

    @router.get("/v1/sessions/{session_id}/events")
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

    return router
