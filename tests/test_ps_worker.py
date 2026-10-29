"""常驻 PowerShell worker：协议、复用、失败降级。"""
import base64
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


def test_non_ascii_arguments_survive_the_protocol(worker):
    """中文参数必须原样到达 PowerShell。

    踩过：请求行原来发明文 JSON，而重定向的 stdin 在 PowerShell 里按控制台
    代码页（中文系统 = GBK）解码，`记事本` 被读成 `璁颁簨鏈` 并吞掉闭合引号，
    整个 JSON 解析失败：

        传入的对象无效，应为":"或"}"。

    受影响的是所有非 ASCII 参数：输入中文、等一个中文控件名、写剪贴板。
    现在请求行是 base64(UTF-8 JSON)，只含 ASCII。
    """
    reply = worker.call({'op': 'echo', 'value': '记事本 · 中文 ✓ 日本語'})
    assert reply['ok'] is True
    assert reply['value'] == '记事本 · 中文 ✓ 日本語'


def test_request_line_is_ascii_only():
    """协议行只能有 ASCII：任何非 ASCII 都会重新引入代码页问题。"""
    import json

    for request in ({'op': 'echo', 'value': '中文'},
                    {'action': 'wait', 'text': '保存', 'timeout_ms': 500}):
        line = ps_worker.Worker._encode(request)
        assert line.endswith(chr(10))
        line.encode('ascii')
        assert json.loads(base64.b64decode(line.strip()).decode('utf-8')) == request


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


@pytest.mark.slow   # real PowerShell restart (~5s)
def test_timeout_stops_the_worker(worker):
    with pytest.raises(ps_worker.WorkerError, match='timed out'):
        worker.call({'op': 'slow', 'ms': 2000}, timeout=0.2)
    # 卡住的 worker 已被丢弃，下次调用自动重启
    assert worker.call({'op': 'echo', 'value': 'ok'})['value'] == 'ok'
    assert worker.stats['restarts'] >= 2


def test_failure_text_is_shared_by_both_transports():
    """常驻 worker 与独立进程必须给出同一句话。

    上游按前缀分流（"拒绝" vs "没做成"）：`sensitive_target: …` 是拒绝，
    「读不到…」是失败。两条传输的格式不一致时，其中一条上就会判错。
    """
    reply = {'ok': False, 'error': 'sensitive_target',
             'message': '这是密码窗口'}
    assert ps_worker.failure_text(reply) == 'sensitive_target: 这是密码窗口'
    # message 里已经含 code 时不重复拼
    assert ps_worker.failure_text(
        {'error': 'sensitive_target',
         'message': 'sensitive_target: 已包含'}) == 'sensitive_target: 已包含'
    assert ps_worker.failure_text({'error': 'unknown'}) == 'unknown'
    assert ps_worker.failure_text({}) == 'worker reported failure'


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
