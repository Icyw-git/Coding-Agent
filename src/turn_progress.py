"""Read live tool progress from the existing session event stream."""

import sys
import threading
import time


def format_duration(seconds: float) -> str:
    """Compact elapsed time for progress and completion messages."""
    total = max(0, int(seconds))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:02d}:{seconds:02d}'


def relative_time(ts: float | None, now: float | None = None) -> str:
    """时间戳 → '刚刚 / N 分钟前 / N 小时前 / N 天前'，一周外显示日期。"""
    if not ts:
        return '时间未知'
    now = time.time() if now is None else now
    delta = max(0, int(now - ts))
    if delta < 60:
        return '刚刚'
    if delta < 3600:
        return f'{delta // 60} 分钟前'
    if delta < 86400:
        return f'{delta // 3600} 小时前'
    if delta < 86400 * 7:
        return f'{delta // 86400} 天前'
    stamp = time.localtime(ts)
    if stamp.tm_year != time.localtime(now).tm_year:
        return time.strftime('%Y-%m-%d %H:%M', stamp)   # 跨年补年份，避免误读
    return time.strftime('%m-%d %H:%M', stamp)


class TurnProgress:
    def __init__(self):
        self.started = time.monotonic()
        self.store = None
        self.offset = 0
        self.tool = None
        self.tool_started = None
        # 本回合工具汇总：名称计数、失败项、累计耗时（供飞书过程卡使用）
        self.tool_counts: dict[str, int] = {}
        self.tool_errors: list[dict] = []
        self.tool_total_ms = 0.0
        # 每个工具批次一条（飞书过程卡按批次显示「工具摘要（N）」）
        self.tool_batches: list[dict] = []

    def follow(self, store, offset):
        self.store, self.offset = store, offset

    def snapshot(self):
        if self.store is not None:
            events, self.offset = self.store.tail_events(self.offset)
            for event in events:
                kind = event.get('type')
                if kind == 'tool_started':
                    self.tool = event.get('name')
                    self.tool_started = event.get('ts')
                    if self.tool:
                        self.tool_counts[self.tool] = self.tool_counts.get(self.tool, 0) + 1
                elif kind == 'tool_profile':
                    tools = [item.get('name') or 'tool'
                             for item in event.get('tools') or []]
                    if tools:
                        self.tool_batches.append({
                            'tools': tools,
                            'status': event.get('status') or 'ok',
                            'error_type': event.get('error_type') or '',
                        })
                    for item in event.get('tools') or []:
                        try:
                            self.tool_total_ms += float(item.get('duration_ms') or 0)
                        except (TypeError, ValueError):
                            pass
                    if event.get('status') == 'error':
                        for item in event.get('tools') or []:
                            self.tool_errors.append({
                                'tool': item.get('name') or 'tool',
                                'error': event.get('error_type') or 'error',
                            })
                elif kind == 'message' and event.get('message', {}).get('role') == 'tool':
                    self.tool = self.tool_started = None
        elapsed = max(0, time.monotonic() - self.started)
        # 工具内部耗时只留在诊断日志；客户端只看总时钟与阶段
        return {
            'phase': 'tool' if self.tool else 'thinking',
            'tool': self.tool,
            'elapsed': int(elapsed),
            'elapsed_text': format_duration(elapsed),
            'tool_counts': dict(self.tool_counts),
            'tool_errors': [dict(item) for item in self.tool_errors],
            'tool_total_ms': round(self.tool_total_ms, 1),
            'tool_batches': [dict(batch) for batch in self.tool_batches],
        }


def format_progress_line(snapshot: dict) -> str:
    """单行进度：阶段、当前工具与已用时；不换行，由调用方负责清除。"""
    elapsed = snapshot.get('elapsed_text') or format_duration(
        snapshot.get('elapsed', 0))
    if snapshot.get('phase') == 'tool':
        return f"  ⏳ {elapsed} · 正在执行 {snapshot.get('tool') or '工具'}"
    return f'  ⏳ {elapsed} · 思考中（Ctrl-C 中断）'


class ProgressTicker:
    """在等待模型/工具时于同一行刷新进度；停止时清除该行。

    只用于交互终端：非 TTY（测试、管道、无头）不启动线程，避免污染 stdout。
    """

    def __init__(self, progress: TurnProgress, stream=None, interval: float = 0.5):
        self.progress = progress
        self.stream = stream or sys.stdout
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start(self) -> 'ProgressTicker':
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._write(format_progress_line(self.progress.snapshot()))

    def _write(self, text: str) -> None:
        with self._lock:
            self.stream.write('\r\033[K' + text)
            self.stream.flush()

    def emit_line(self, text: str) -> None:
        """在进度行位置原子地换行输出一条（工具活动行），避免与 tick 互相覆盖。"""
        with self._lock:
            self.stream.write('\r\033[K' + text + '\n')
            self.stream.flush()

    def stop(self) -> None:
        """停止并清除进度行；可重复调用。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        with self._lock:
            self.stream.write('\r\033[K')
            self.stream.flush()

    def __enter__(self) -> 'ProgressTicker':
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
