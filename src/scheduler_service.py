"""Cron / 队列后台服务的可停止生命周期。

原先两个 ``while True`` 循环只在 CLI 入口用 daemon 线程启动，API server 的 lifespan
完全没有接线，而且无法停止。这里把启动/停止收进一个对象，供 entrypoint 与 FastAPI
lifespan 共用：

- ``start()`` 幂等：重复调用不会起第二组线程；只在第一次加载 durable jobs。
- ``stop()`` 置停止信号并 join，有界等待；进程退出时不会留下后台循环。
- 循环本身仍是非阻塞抢 ``agent_lock``，与 TUI 的单回合协调配合。
"""
from __future__ import annotations

import threading

import cron_scheduler


class SchedulerService:
    """启动/停止 cron 调度与队列处理线程。"""

    def __init__(self, run_turn, *, load_jobs: bool = True,
                 queue_poll: float = 0.2, stop_timeout: float = 5.0) -> None:
        self._run_turn = run_turn
        self._load_jobs = load_jobs
        self._queue_poll = queue_poll
        self._stop_timeout = stop_timeout
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._threads) and not self._stop_event.is_set()

    @property
    def threads(self) -> tuple[threading.Thread, ...]:
        with self._lock:
            return tuple(self._threads)

    def start(self) -> bool:
        """启动后台线程；已在运行则返回 False。"""
        with self._lock:
            if self._threads:
                return False
            if self._load_jobs:
                cron_scheduler.load_durable_jobs()
            self._stop_event = threading.Event()
            scheduler = threading.Thread(
                target=cron_scheduler.cron_scheduler_loop,
                args=(self._stop_event,),
                name='cron-scheduler', daemon=True)
            worker = threading.Thread(
                target=cron_scheduler.queue_processor_loop,
                args=(self._run_turn, self._queue_poll, self._stop_event),
                name='cron-worker', daemon=True)
            self._threads = [scheduler, worker]
            for thread in self._threads:
                thread.start()
            return True

    def stop(self, timeout: float | None = None) -> bool:
        """请求停止并 join；返回是否全部线程已退出。"""
        with self._lock:
            threads = list(self._threads)
            self._threads = []
            self._stop_event.set()
        deadline_budget = self._stop_timeout if timeout is None else timeout
        stopped = True
        for thread in threads:
            thread.join(timeout=max(0.0, deadline_budget))
            if thread.is_alive():
                stopped = False
        return stopped
