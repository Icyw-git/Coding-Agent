"""常驻 PowerShell worker：协议、复用、失败降级。"""
import sys

import pytest

import ps_worker

pytestmark = pytest.mark.skipif(sys.platform != 'win32',
                                reason='PowerShell worker is Windows-only')

ECHO_SCRIPT = ps_worker.PRELUDE + r'''
function Invoke-HarnessOp($req) {
    if ($req.op -eq 'echo') { return @{ ok = $true; value = $req.value } }
    if ($req.op -eq 'slow') { Start-Sleep -Milliseconds $req.ms; return @{ ok = $true } }
    if ($req.op -eq 'boom') { throw 'intentional failure' }
    if ($req.op -eq 'fail') {
        $script:HarnessFailure = @{ ok = $false; error = 'expected_failure'; detail = 'x' }
        throw [System.Exception]::new('__harness_failure__')
    }
    return @{ ok = $false; error = 'unknown op' }
}
''' + ps_worker.LOOP


@pytest.fixture()
def worker():
    instance = ps_worker.Worker('test-echo', ECHO_SCRIPT)
    try:
        yield instance
    finally:
        instance._stop()


def test_roundtrip_reuses_one_process(worker):
    first = worker.call({'op': 'echo', 'value': 'a'})
    second = worker.call({'op': 'echo', 'value': 'b'})
    assert first['value'] == 'a' and second['value'] == 'b'
    assert worker.stats['calls'] == 2
    assert worker.stats['restarts'] == 1        # 只启动过一次


def test_resident_calls_are_fast(worker):
    """复用进程的核心收益：后续调用不再付启动成本。"""
    import time

    worker.call({'op': 'echo', 'value': 'warm'})
    times = []
    for _ in range(10):
        started = time.perf_counter()
        worker.call({'op': 'echo', 'value': 'x'})
        times.append((time.perf_counter() - started) * 1000)
    # 保守阈值：独立进程要 260ms+，这里必须远低于它
    assert sum(times) / len(times) < 100


def test_exception_becomes_error_reply_without_killing_worker(worker):
    reply = worker.call({'op': 'boom'})
    assert reply['ok'] is False
    assert 'intentional failure' in reply['error']
    # 进程还活着，下一个请求照常
    assert worker.call({'op': 'echo', 'value': 'after'})['value'] == 'after'


def test_structured_failure_payload_is_preserved(worker):
    """能力模块的失败对象要原样回包（带 state/step），不能压成一句 error。"""
    reply = worker.call({'op': 'fail'})
    assert reply['ok'] is False
    assert reply['error'] == 'expected_failure'
    assert reply['detail'] == 'x'


def test_timeout_stops_the_worker(worker):
    with pytest.raises(ps_worker.WorkerError, match='timed out'):
        worker.call({'op': 'slow', 'ms': 2000}, timeout=0.2)
    # 卡住的 worker 已被丢弃，下次调用自动重启
    assert worker.call({'op': 'echo', 'value': 'ok'})['value'] == 'ok'
    assert worker.stats['restarts'] >= 2


def test_repeated_failures_break_the_worker():
    instance = ps_worker.Worker('test-broken', ECHO_SCRIPT)
    instance._broken = True
    try:
        with pytest.raises(ps_worker.WorkerError):
            instance.call({'op': 'echo', 'value': 'x'})
        assert not instance.healthy()
    finally:
        instance._stop()


def test_run_falls_back_when_worker_is_broken(monkeypatch):
    def broken(*args, **kwargs):
        raise ps_worker.WorkerError('no worker')

    monkeypatch.setattr(ps_worker, 'get_worker', broken)
    sentinel = object()
    assert ps_worker.run('x', lambda: '', {'op': 'a'},
                         fallback=lambda: sentinel) is sentinel


def test_run_raises_failure_without_fallback(monkeypatch):
    def broken(*args, **kwargs):
        raise ps_worker.WorkerError('no worker')

    monkeypatch.setattr(ps_worker, 'get_worker', broken)
    with pytest.raises(ps_worker.WorkerError):
        ps_worker.run('x', lambda: '', {'op': 'a'})


def test_get_worker_is_singleton_per_name():
    a = ps_worker.get_worker('singleton-test', ECHO_SCRIPT)
    b = ps_worker.get_worker('singleton-test', ECHO_SCRIPT)
    assert a is b


def test_shutdown_clears_registry():
    ps_worker.get_worker('shutdown-test', ECHO_SCRIPT)._ensure()
    ps_worker.shutdown()
    assert ps_worker.stats() == {}
