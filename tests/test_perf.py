import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier

import perf


def test_concurrent_turns_keep_separate_stats(monkeypatch):
    monkeypatch.setenv('HARNESS_PERF', '1')
    barrier = Barrier(2)

    def turn(name):
        perf.reset()
        perf.record(name, 0.5)
        barrier.wait(timeout=5)
        return perf.snapshot()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(turn, 'first')
        second = pool.submit(turn, 'second')
        assert first.result() == {'first': (1, 0.5)}
        assert second.result() == {'second': (1, 0.5)}


def test_inherited_async_context_does_not_share_mutations(monkeypatch):
    monkeypatch.setenv('HARNESS_PERF', '1')

    async def child():
        perf.record('child', 1)
        await asyncio.sleep(0)
        return perf.snapshot()

    async def parent():
        perf.reset()
        perf.record('parent', 2)
        result = await asyncio.create_task(child())
        assert result == {'parent': (1, 2), 'child': (1, 1)}
        assert perf.snapshot() == {'parent': (1, 2)}

    asyncio.run(parent())


def test_reset_and_snapshot_do_not_mutate_other_contexts(monkeypatch):
    monkeypatch.setenv('HARNESS_PERF', '1')
    perf.reset()
    perf.record('outer', 1)
    copy_context().run(perf.reset)
    data = perf.snapshot()
    data.clear()
    assert perf.snapshot() == {'outer': (1, 1)}


def test_enabled_after_import_and_disabled_by_default(monkeypatch):
    perf.reset()
    monkeypatch.delenv('HARNESS_PERF', raising=False)
    with perf.segment('disabled'):
        pass
    assert perf.snapshot() == {}
    monkeypatch.setenv('HARNESS_PERF', '1')
    with perf.segment('enabled'):
        pass
    assert perf.snapshot()['enabled'][0] == 1
