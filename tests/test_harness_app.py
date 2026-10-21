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
