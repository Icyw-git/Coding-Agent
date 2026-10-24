"""Task routes for the HTTP control plane."""

from __future__ import annotations

from dataclasses import asdict

from api_models import CreateTaskRequest


def build_router(tasks):
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import PlainTextResponse

    router = APIRouter()

    @router.post("/v1/tasks", status_code=201)
    def create_task(request: CreateTaskRequest):
        try:
            record = tasks.create(request.command, request.cwd)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"task": asdict(record)}

    @router.get("/v1/tasks")
    def list_tasks(limit: int = 50):
        return {"tasks": [asdict(record) for record in tasks.list(limit)]}

    @router.get("/v1/tasks/{task_id}")
    def get_task(task_id: str):
        try:
            record = tasks.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        return {"task": asdict(record)}

    @router.post("/v1/tasks/{task_id}/stop")
    def stop_task(task_id: str):
        try:
            record = tasks.stop(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"task": asdict(record)}

    @router.get("/v1/tasks/{task_id}/logs")
    def task_logs(task_id: str, tail: int = 200):
        try:
            tasks.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        return PlainTextResponse(tasks.tail_logs(task_id, tail))

    return router
