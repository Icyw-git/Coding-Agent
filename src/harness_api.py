"""FastAPI adapter for the transport-independent agent runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import api_cron_results
import api_crons
import api_heartbeat
import api_policy
import api_sessions
import api_streams
import api_tasks
from agent_runtime import AgentRuntime
from harness_sdk import HarnessClient
from hooks import GATE as permission_gate
from hooks import enforce_headless
from scheduler_service import SchedulerService
from task_manager import TaskManager
from workspace_context import workspace_roots


def _scheduler_enabled() -> bool:
    """HARNESS_SCHEDULER=0 关闭；默认在拥有 runtime 的 server 进程里启动。"""
    raw = (os.getenv('HARNESS_SCHEDULER') or '').strip().casefold()
    return raw not in ('0', 'false', 'no', 'off')


def create_app(runtime=None, *, api_token: str | None = None, rate_limit: int = 60,
               access_log_path: str | None = None, task_root=None, workspace=None):
    """Create the HTTP application without starting background services."""
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
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
    import paths

    task_base = Path(task_root or os.getenv("HARNESS_TASK_ROOT")
                     or paths.cache_path("os_tasks"))
    allowed_roots = workspace_roots(
        workspace or os.getenv("HARNESS_WORKSPACE") or Path.cwd())
    tasks = TaskManager(
        task_base,
        allowed_roots=allowed_roots,
        # 执行策略层接线：任务命令不经过 PreToolUse hook，由 runner 在 Popen 前判定
        # （需要审批时进 /v1/permissions 队列，超时拒绝）。
        enforce=lambda command: enforce_headless('bash', {'command': command}),
    )
    if isinstance(runtime, AgentRuntime):
        runtime.configure_workspace_roots(allowed_roots)
    # 服务器模式：权限审批走远程端点（消息网关轮询 /v1/permissions）；
    # CLI 入口不经过 create_app，保持 console 后端不受影响。
    permission_gate.backend = 'remote'

    logger = logging.Logger("harness.api.access", level=logging.INFO)
    access_log_path = (access_log_path or os.getenv("HARNESS_API_LOG")
                       or paths.cache_path("api", "api-access.log"))
    @asynccontextmanager
    async def lifespan(app):
        scheduler = None
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
                # 后台预热 LLM 客户端（openai SDK import ~1.5s），首个请求不再等待
                threading.Thread(target=harness.client.warmup, daemon=True).start()
                if _scheduler_enabled():
                    # 常驻模式：cron 到点可以自主跑回合；关闭时 stop() 会 join。
                    scheduler = SchedulerService(harness.run_agent_turn_locked)
                    scheduler.start()
            yield
        finally:
            if scheduler is not None:
                if not scheduler.stop():
                    logger.warning('cron scheduler threads did not stop in time')
            if owns_runtime:
                # 回合后的标题/记忆整理可能还在写会话日志：有界等待并记录
                # 未完成的作业（线程名带会话 id），再进入 MCP 关闭流程。
                pending = await asyncio.to_thread(
                    harness.shutdown_housekeeping, 10)
                if pending:
                    logger.warning('housekeeping still running: %s',
                                   ', '.join(pending))
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

    app.include_router(api_sessions.build_router(client, runtime))
    app.include_router(api_crons.build_router())
    app.include_router(api_cron_results.build_router())
    app.include_router(api_heartbeat.build_router())
    app.include_router(api_streams.build_router(client, runtime, logger))
    app.include_router(api_tasks.build_router(tasks))
    app.include_router(api_policy.build_router(permission_gate))

    return app


def _default_runtime():
    import harness

    return harness.create_runtime(initialize_tools=False)
