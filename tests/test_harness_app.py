"""新 TUI 骨架：历史 → 块模型 → 虚拟视口；流式与工具事件。"""
import asyncio

import pytest

pytest.importorskip('textual')

import harness  # noqa: E402
import ui_events  # noqa: E402
from ui.harness_app import HarnessApp  # noqa: E402
from ui_events import UIEvent  # noqa: E402


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


def test_slash_help_is_handled_locally(monkeypatch):
    def fail_loop(messages, **kwargs):
        raise AssertionError('command must not reach the model')

    monkeypatch.setattr(harness, 'agent_loop', fail_loop)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('/help')
            await pilot.pause()
            assert len(app.model) == 1
            assert app.model.snapshot()[0].kind == 'notice'
            assert 'commands:' in str(
                app.model.snapshot()[0].payload['text']).lower()
            assert len(app._pending) == 0

    asyncio.run(scenario())


def test_sessions_picker_resumes_real_session(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path)
    store = harness.SessionEventStore(tmp_path, session_id='next-resume')
    store.append_message({'role': 'user', 'content': 'old goal'})
    store.append_message({'role': 'assistant', 'content': 'old answer'})

    async def scenario():
        app = HarnessApp([], session_id='current')
        async with app.run_test(size=(80, 24)) as pilot:
            app.open_session_picker()
            await pilot.pause()
            screen = app.screen
            assert type(screen).__name__ == 'SessionPickerModal'
            options = screen.query_one('#session-list')
            assert options.option_count == 1
            options.focus()
            await pilot.press('enter')
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if app._session_id == 'next-resume':
                    break
            assert app._session_id == 'next-resume'
            assert any(m.get('content') == 'old goal' for m in app._messages)

    asyncio.run(scenario())


def test_permission_request_opens_modal_and_resolves():
    resolved = []

    def record(event):
        if event.type == 'permission_resolve':
            resolved.append(event)

    ui_events.bus.subscribe(record)
    try:
        async def scenario():
            app = HarnessApp([])
            async with app.run_test(size=(80, 24)) as pilot:
                app._apply(UIEvent('permission_request', {
                    'req_id': 'perm-1', 'tool': 'bash',
                    'args': {'command': 'rm x'}, 'title': 'destructive'}))
                await pilot.pause()
                assert type(app.screen).__name__ == 'PermissionModal'
                await asyncio.sleep(0.25)     # 防误触
                await pilot.press('y')
                await pilot.pause()
                assert resolved and resolved[-1].payload['approved'] is True
                assert resolved[-1].payload['req_id'] == 'perm-1'

        asyncio.run(scenario())
    finally:
        ui_events.bus.unsubscribe(record)


def test_bang_shell_runs_without_agent(monkeypatch):
    def fail_loop(messages, **kwargs):
        raise AssertionError('shell must not reach the model')

    monkeypatch.setattr(harness, 'agent_loop', fail_loop)
    monkeypatch.setattr(harness, 'run_bash', lambda command: f'shell:{command}')

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('!echo hi')
            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.02)
                block = app.model.get('tool-1') or next(
                    (b for b in app.model.snapshot() if b.kind == 'tool'), None)
                if block and block.payload.get('state') != 'running':
                    break
            block = next(b for b in app.model.snapshot() if b.kind == 'tool')
            assert 'shell:echo hi' in block.payload['output']
            assert block.payload['state'] == 'complete'
            assert len(app._pending) == 0

    asyncio.run(scenario())


def test_escape_cancels_only_when_busy(monkeypatch):
    import turn_control

    calls = []
    monkeypatch.setattr(turn_control, 'request_cancel',
                        lambda: calls.append(True))

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)):
            app._composer.action_escape()      # 空闲：不取消
            assert calls == []

            app._busy = True
            app._composer.action_escape()
            assert calls == [True]

            app._suggestions = [('/help', '/help')]
            app._composer.action_escape()      # 有候选：只关列表
            assert app._suggestions == []
            assert calls == [True]

    asyncio.run(scenario())


def test_ctrl_o_expands_last_tool(monkeypatch):
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app._apply(UIEvent('tool_start', {
                'name': 'bash', 'args': {'command': 'pytest'}, 'call_id': 'c1'}))
            await pilot.pause()
            app._apply(UIEvent('tool_end', {
                'name': 'bash', 'call_id': 'c1', 'outcome': 'completed',
                'ok': True, 'output_preview': 'done', 'duration_ms': 5}))
            await pilot.pause()
            app._composer.action_expand_tool()
            block = app.model.get('tool-c1')
            assert block.expanded is True

    asyncio.run(scenario())


def test_approval_preview_prefers_patch_or_content():
    from ui.modals.approval import PermissionModal

    patch_modal = PermissionModal('r1', 'apply_patch',
                                  {'patch': '*** Begin Patch\n' + 'line\n' * 30})
    assert 'Patch (preview)' in patch_modal._detail_text()
    assert 'more lines' in patch_modal._detail_text()

    content_modal = PermissionModal('r2', 'write',
                                    {'path': 'a.txt', 'content': 'hello\n'})
    assert 'Content (preview)' in content_modal._detail_text()

    command_modal = PermissionModal('r3', 'bash', {'command': 'rm x'})
    assert 'Target: rm x' in command_modal._detail_text()


def test_unknown_command_suggests_and_hotkeys(monkeypatch):
    monkeypatch.setattr(harness, 'agent_loop',
                        lambda messages, **kwargs: (_ for _ in ()).throw(
                            AssertionError('command must not reach model')))

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('/modl')
            await pilot.pause()
            notice = app.model.snapshot()[-1].payload['text']
            assert '/model' in notice

            app.submit_text('/hotkeys')
            await pilot.pause()
            hotkeys = app.model.snapshot()[-1].payload['text']
            assert 'Ctrl+O' in hotkeys and 'Esc' in hotkeys

    asyncio.run(scenario())
