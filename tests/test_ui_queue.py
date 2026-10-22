"""排队消息：Enter/Alt+Enter 分流、选择器单条取回与删除。"""
import asyncio

import pytest

pytest.importorskip('textual')

from ui.harness_app import HarnessApp, PendingQueue  # noqa: E402


def _busy(app, messages: list[tuple[str, str]]) -> None:
    app._busy = True
    app._operation = 'agent'
    for text, mode in messages:
        app.submit_text(text, mode=mode)


def test_enter_queues_next_and_alt_enter_steers_immediately():
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            _busy(app, [])
            app._composer.text = 'after turn'
            await pilot.press('enter')
            await pilot.pause()
            app._composer.text = 'inject now'
            await pilot.press('alt+enter')
            await pilot.pause()
            assert app._pending.snapshot() == ['after turn']
            assert app._steering.snapshot() == ['inject now']
            assert app._composer.text == ''
            app._busy = False

    asyncio.run(scenario())


def test_single_queued_message_restores_directly():
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            _busy(app, [('only one', 'followup')])
            await pilot.press('ctrl+g')
            await pilot.pause()
            assert type(app.screen).__name__ != 'QueueModal'
            assert app._pending.snapshot() == []
            assert app._composer.text == 'only one'
            app._busy = False

    asyncio.run(scenario())


def test_queue_picker_restores_selected_message_only():
    async def scenario():
        from textual.widgets import OptionList

        from ui.modals.queue import QueueModal

        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            _busy(app, [('steer one', 'steer'),
                        ('next one', 'followup'),
                        ('next two', 'followup')])
            await pilot.press('ctrl+g')
            await pilot.pause()
            assert isinstance(app.screen, QueueModal)
            app.screen.query_one('#queue-list', OptionList).focus()
            await pilot.pause()
            await pilot.press('down')
            await pilot.press('down')              # steer one -> next two
            options = app.screen.query_one('#queue-list', OptionList)
            assert options.highlighted == 2
            await pilot.press('enter')
            await pilot.pause()
            assert app._composer.text == 'next two'
            assert app._steering.snapshot() == ['steer one']
            assert app._pending.snapshot() == ['next one']
            app._busy = False

    asyncio.run(scenario())


def test_queue_picker_deletes_selected_after_confirmation():
    async def scenario():
        from ui.modals.confirm import ConfirmModal
        from ui.modals.queue import QueueModal

        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            _busy(app, [('keep steer', 'steer'),
                        ('drop next', 'followup')])
            await pilot.press('ctrl+g')
            await pilot.pause()
            assert isinstance(app.screen, QueueModal)
            await pilot.press('down')              # 进列表
            await pilot.press('down')              # 选中 drop next
            await pilot.press('delete')
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press('y')
            await pilot.pause()
            assert app._steering.snapshot() == ['keep steer']
            assert app._pending.snapshot() == []
            assert app._composer.text == ''
            app._busy = False

    asyncio.run(scenario())


def test_cancelled_delete_keeps_message():
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            _busy(app, [('keep me', 'followup'),
                        ('also keep', 'followup')])
            await pilot.press('ctrl+g')
            await pilot.pause()
            await pilot.press('delete')
            await pilot.pause()
            await pilot.press('n')
            await pilot.pause()
            assert app._pending.snapshot() == ['keep me', 'also keep']
            app._busy = False

    asyncio.run(scenario())


def test_take_at_rejects_stale_expected_text():
    queue = PendingQueue()
    queue.put('a')
    queue.put('b')
    assert queue.take_at(0, expected='b') is None
    assert queue.snapshot() == ['a', 'b']
    assert queue.take_at(1, expected='b') == 'b'
    assert queue.snapshot() == ['a']


def test_stale_picker_row_does_not_restore_another_message():
    async def scenario():
        from ui.modals.queue import QueueModal

        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            _busy(app, [('steer gone', 'steer'),
                        ('next stays', 'followup')])
            await pilot.press('ctrl+g')
            await pilot.pause()
            assert isinstance(app.screen, QueueModal)
            app._steering.drain()          # 模拟 Agent 在弹窗打开期间消费了消息
            await pilot.press('enter')     # 仍指向已失效的 steer:0
            await pilot.pause()
            assert app._composer.text == ''
            assert app._pending.snapshot() == ['next stays']
            app._busy = False

    asyncio.run(scenario())
