"""关闭时的 housekeeping 收尾：有界等待、记录未完成作业、清理已结束线程。"""
import threading
import time

import harness


def _reset(monkeypatch):
    monkeypatch.setattr(harness, '_housekeeping_threads', [])
    return harness._housekeeping_threads


def test_shutdown_waits_for_finished_work_and_prunes(monkeypatch):
    threads = _reset(monkeypatch)
    thread = threading.Thread(target=lambda: time.sleep(0.05),
                              name='housekeeping:s1:1')
    threads.append(thread)
    thread.start()

    assert harness.shutdown_housekeeping(timeout=1.0) == []
    assert harness.housekeeping_pending() == []


def test_shutdown_reports_unfinished_work(monkeypatch, capsys):
    threads = _reset(monkeypatch)
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True,
                              name='housekeeping:s2:3')
    threads.append(thread)
    thread.start()
    try:
        pending = harness.shutdown_housekeeping(timeout=0.05)
        assert pending == ['housekeeping:s2:3']
        assert '未在 0.05s 内结束' in capsys.readouterr().out
        assert harness.housekeeping_busy() is True
    finally:
        release.set()
        thread.join(timeout=1.0)
    assert harness.housekeeping_pending() == []
