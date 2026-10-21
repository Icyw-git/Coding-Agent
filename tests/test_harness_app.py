"""新 TUI 骨架：历史 → 块模型 → 虚拟视口；流式与工具事件。"""
import asyncio

import pytest

pytest.importorskip('textual')

import harness  # noqa: E402
import ui_events  # noqa: E402
from ui.harness_app import HarnessApp  # noqa: E402


def _line_texts(app):
    return [str(app.view.render_line(i).text) for i in range(app.view.size.height)]


def test_history_renders_through_virtual_transcript():
    messages = []
    for index in range(300):
        role = 'user' if index % 2 == 0 else 'assistant'
        messages.append({'role': role, 'content': f'history-{index}'})

    async def scenario():
        app = HarnessApp(messages)
        async with app.run_test(size=(80, 20)) as pilot:
            for _ in range(40):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if app.view.content_line_count:
                    break
            assert len(app.model) == 300
            assert app.view.content_line_count >= 300
            text = '\n'.join(_line_texts(app))
            assert 'history-299' in text

    asyncio.run(scenario())


def test_streaming_and_tool_events_update_the_model():
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            ui_events.bus.emit('assistant_start', mid='m1')
            ui_events.bus.emit('assistant_delta', mid='m1', delta='hello ')
            ui_events.bus.emit('assistant_delta', mid='m1', delta='world')
            ui_events.bus.emit('assistant_end', mid='m1')
            ui_events.bus.emit('tool_start', name='bash',
                               args={'command': 'pytest -q'}, call_id='c1')
            ui_events.bus.emit('tool_end', name='bash', call_id='c1',
                               outcome='completed', ok=True,
                               output_preview='ok\nline2', duration_ms=12)

            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if app.model.get('m1') and app.model.get('tool-c1'):
                    break
            assert app.model.get('m1').payload['text'] == 'hello world'
            assert app.model.get('m1').payload['streaming'] is False
            tool = app.model.get('tool-c1')
            assert tool.payload['state'] == 'completed'
            assert 'pytest -q' in str(tool.payload['args'])

            before = app.view.content_line_count
            app.toggle_last_tool()
            await pilot.pause()
            await asyncio.sleep(0.08)
            assert app.model.get('tool-c1').expanded is True
            assert app.view.content_line_count >= before

    asyncio.run(scenario())


def test_submit_queues_and_calls_agent(monkeypatch):
    calls = []

    def fake_loop(messages, **kwargs):
        calls.append([m['content'] for m in messages])
        return 'ok'

    monkeypatch.setattr(harness, 'agent_loop', fake_loop)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('hello agent')
            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if calls:
                    break
            assert calls and calls[0] == ['hello agent']
            assert len(app.model) == 1
            assert app.model.snapshot()[0].kind == 'user'

    asyncio.run(scenario())


def test_completion_overlay_and_accept():
    from ui.composer.completions import apply_suggestion, suggestions

    assert apply_suggestion('/he', '/help') == '/help'
    assert apply_suggestion('see @src/ap', '@src/app.py') == 'see @src/app.py'
    assert suggestions('/he', [])[0][0] == '/help'

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app._composer.text = '/he'
            await pilot.pause()
            assert app.has_suggestions()
            assert app._popup.display is True

            app.accept_suggestion()
            await pilot.pause()
            assert app._composer.text == '/help'
            assert not app.has_suggestions()

            app._composer.text = '/st'
            await pilot.pause()
            assert app._popup.display is True
            app._composer.action_escape()
            assert app._popup.display is False

    asyncio.run(scenario())


def test_queue_strip_and_take_back(monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()

    def blocking_loop(messages, **kwargs):
        started.set()
        release.wait(5)
        return 'ok'

    monkeypatch.setattr(harness, 'agent_loop', blocking_loop)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('one')
            for _ in range(50):
                await pilot.pause()
                await asyncio.sleep(0.01)
                if started.is_set():
                    break
            assert started.is_set()

            app.submit_text('two')
            app.submit_text('three')
            await pilot.pause()
            assert len(app._pending) == 2
            assert app._queue_widget.display is True
            assert '2 queued' in str(app._queue_widget.render())

            app.take_last_queued()
            assert app._composer.text == 'three'
            assert len(app._pending) == 1

            release.set()
            app._exiting = True
            app._pending.put(None)

    asyncio.run(scenario())


def test_working_status_in_composer_border(monkeypatch):
    import threading

    release = threading.Event()
    monkeypatch.setattr(harness, 'agent_loop',
                        lambda messages, **kwargs: release.wait(5))

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('work')
            await pilot.pause()
            app._tick_border_status()
            assert 'Working' in app._composer.border_title
            release.set()
            app._exiting = True
            app._pending.put(None)

    asyncio.run(scenario())
