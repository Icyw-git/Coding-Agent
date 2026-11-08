"""排队消息：Enter/Alt+Enter 分流、选择器单条取回与删除。"""
import asyncio

import pytest

pytest.importorskip('textual')

from ui.harness_app import HarnessApp, PendingQueue  # noqa: E402
from ui.widgets import TurnRequest  # noqa: E402


def _busy(app, messages: list[tuple[str, str]]) -> None:
    app._busy = True
    app._operation = 'agent'
    for text, mode in messages:
        app.submit_text(text, mode=mode)


async def _press_until_highlight(pilot, app, target: int, *, tries: int = 200):
    """按一次 down，并等高亮*真的*走到 target 再返回。

    按键与“高亮已变”是两件事：`pilot.press` 返回不等于事件已被处理。全线跑时
    （前 3500 个用例之后）偶发只走一步——所以每按一次就等一次，失败时能看出
    停在哪一步，而不是笼统地“highlighted == 1”。
    """
    from textual.widgets import OptionList

    await pilot.press('down')
    for _ in range(tries):
        if app.screen.query_one('#queue-list', OptionList).highlighted >= target:
            return
        await pilot.pause()
        await asyncio.sleep(0.005)      # pause() 在负载下可能只让出几毫秒
    raise AssertionError(
        f'按 down 之后高亮停在 '
        f'{app.screen.query_one("#queue-list", OptionList).highlighted}，期望 {target}')


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
            assert app._pending.texts() == ['after turn']
            assert app._steering.texts() == ['inject now']
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
            assert app._pending.texts() == []
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
            # 每按一次都等它到位：原来两个 press 之后直接 assert，全线跑时偶发只走一步。
            await _press_until_highlight(pilot, app, 1)
            await _press_until_highlight(pilot, app, 2)   # steer one -> next two
            options = app.screen.query_one('#queue-list', OptionList)
            assert options.highlighted == 2
            await pilot.press('enter')
            await pilot.pause()
            assert app._composer.text == 'next two'
            assert app._steering.texts() == ['steer one']
            assert app._pending.texts() == ['next one']
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
            assert app._steering.texts() == ['keep steer']
            assert app._pending.texts() == []
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
            assert app._pending.texts() == ['keep me', 'also keep']
            app._busy = False

    asyncio.run(scenario())


def test_take_at_rejects_stale_expected_text():
    queue = PendingQueue()
    queue.put(TurnRequest(text='a'))
    queue.put(TurnRequest(text='b'))
    assert queue.take_at(0, expected='b') is None
    assert queue.texts() == ['a', 'b']
    taken = queue.take_at(1, expected='b')
    assert taken is not None and taken.text == 'b'
    assert queue.texts() == ['a']


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
            assert app._pending.texts() == ['next stays']
            app._busy = False

    asyncio.run(scenario())


# ---------- 队列条目可以带"这一回合怎么跑" ----------

def test_turn_request_defaults_to_a_plain_turn():
    from ui.widgets import TurnRequest

    item = TurnRequest(text='hello')
    assert item.text == 'hello'
    assert item.tools is None        # None = 全部工具
    assert item.proactive is False


def test_queue_carries_turn_requests_and_keeps_text_helpers():
    """队列条目换成对象后，取回/去重/取出仍按**文本**工作。

    `/suggest` 要靠这个类型把「只用只读工具」带到 worker，而取回队列、去重、
    按位置取走这些既有行为不能跟着变。
    """
    from ui.widgets import PendingQueue, TurnRequest

    queue = PendingQueue()
    queue.put(TurnRequest(text='a'))
    queue.put(TurnRequest(text='b', tools=[], proactive=True))

    assert [item.text for item in queue.snapshot()] == ['a', 'b']
    taken = queue.take_at(1, expected='b')
    assert taken is not None and taken.text == 'b' and taken.proactive is True
    assert queue.take_at(0, expected='nope') is None     # 期望不匹配 → 拒绝误取
    assert queue.take_last().text == 'a'
    assert len(queue) == 0


def test_queue_drain_returns_requests_for_restore():
    from ui.widgets import PendingQueue, TurnRequest

    queue = PendingQueue()
    queue.put(TurnRequest(text='one'))
    queue.put(None)          # 退出哨兵不算消息
    queue.put(TurnRequest(text='two'))

    drained = queue.drain()
    assert [item.text for item in drained] == ['one', 'two']
    assert len(queue) == 0
