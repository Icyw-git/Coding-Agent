"""Cron job management routes (list / create / delete).

调度器本身仍是进程内单例（`cron_scheduler`）；这里只做参数校验与状态读写，不复制
调度逻辑。cron 表达式错误返回 400，稳定 ID 冲突返回 409，删除未知 ID 返回 404。
"""
from __future__ import annotations

from api_models import CreateCronRequest, UpdateCronRequest


def _as_dict(job) -> dict:
    return {'id': job.id, 'cron': job.cron, 'prompt': job.prompt,
            'recurring': job.recurring, 'durable': job.durable,
            'last_fired': job.last_fired,
            'allow_side_effects': bool(getattr(job, 'allow_side_effects', False)),
            'deliver_to': str(getattr(job, 'deliver_to', '') or '')}


def build_router():
    from fastapi import APIRouter, HTTPException

    import cron_scheduler

    router = APIRouter()

    @router.get('/v1/crons')
    def list_crons():
        with cron_scheduler.cron_lock:
            jobs = [_as_dict(job) for job in cron_scheduler.scheduled_jobs.values()]
        return {'jobs': jobs}

    @router.post('/v1/crons', status_code=201)
    def create_cron(request: CreateCronRequest):
        job = cron_scheduler.schedule_job(
            request.cron, request.prompt, request.recurring,
            request.durable, job_id=request.id,
            allow_side_effects=request.allow_side_effects,
            deliver_to=request.deliver_to or '')
        if isinstance(job, str):
            status = 409 if 'already exists' in job else 400
            raise HTTPException(status_code=status, detail=job)
        return _as_dict(job)

    @router.patch('/v1/crons/{job_id}')
    def update_cron(job_id: str, request: UpdateCronRequest):
        job = cron_scheduler.update_job(
            job_id,
            allow_side_effects=request.allow_side_effects,
            deliver_to=request.deliver_to)
        if isinstance(job, str):
            raise HTTPException(status_code=404, detail=job)
        return _as_dict(job)

    @router.delete('/v1/crons/{job_id}')
    def delete_cron(job_id: str):
        with cron_scheduler.cron_lock:
            exists = job_id in cron_scheduler.scheduled_jobs
        if not exists:
            raise HTTPException(status_code=404, detail=f'Job {job_id} not found')
        cron_scheduler.cancel_job(job_id)
        return {'status': 'deleted', 'id': job_id}

    return router
