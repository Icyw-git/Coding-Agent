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
            assert app.model.snapshot()[0].kind == 'user'
            assert any(block.kind == 'assistant' for block in app.model.snapshot())

    asyncio.run(scenario())


def test_completion_overlay_and_accept():
    from ui.composer.completions import apply_suggestion, suggestions

    assert apply_suggestion('/he', '/help') == '/help'
    assert apply_suggestion('see @src/ap', '@src/app.py') == 'see @src/app.py'
    assert suggestions('/he', [])[0][0] == '/help '

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app._composer.text = '/he'
            await pilot.pause()
            assert app.has_suggestions()
            assert app._popup.display is True

            app.accept_suggestion()
            await pilot.pause()
            assert app._composer.text == '/help '
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
            # 焦点在搜索框时 Enter 直接选中第一条（无需先 Tab/↓）。
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


def test_argument_completion_replaces_only_argument(monkeypatch):
    from ui.composer.completions import apply_suggestion, suggestions

    assert apply_suggestion('/resume 12', '1234') == '/resume 1234'
    assert apply_suggestion('/he', '/help') == '/help'

    monkeypatch.setattr(harness, 'session_rows', lambda **kwargs: [
        {'id': 'abc123', 'title': 'first task', 'workspace': 'ws',
         'updated': 0, 'preview': ''},
    ])
    items = suggestions('/resume ab', [])
    assert items and items[0][0] == 'abc123'

    help_items = suggestions('/help mo', [])
    assert any(value == 'model' for value, _label in help_items)


def test_help_with_argument_shows_command_details(monkeypatch):
    monkeypatch.setattr(harness, 'agent_loop',
                        lambda messages, **kwargs: (_ for _ in ()).throw(
                            AssertionError('command must not reach model')))

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('/help resume')
            await pilot.pause()
            text = app.model.snapshot()[-1].payload['text']
            assert '/resume' in text and '恢复会话' in text

            app.submit_text('/help rewind')
            await pilot.pause()
            text = app.model.snapshot()[-1].payload['text']
            assert '仅行式 REPL' in text

    asyncio.run(scenario())


def test_skill_command_expands_into_user_message(monkeypatch):
    import skill_system

    captured = []

    def fake_loop(messages, **kwargs):
        captured.append(list(messages))
        return 'ok'

    monkeypatch.setattr(harness, 'agent_loop', fake_loop)
    monkeypatch.setattr(skill_system, 'load_skill',
                        lambda name: f'BODY of {name}')

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('/skill:demo extra args')
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if captured:
                    break
            assert captured
            content = captured[-1][-1]['content']
            assert '<skill name="demo">' in content
            assert 'BODY of demo' in content
            assert 'extra args' in content
            assert any(block.kind == 'user' for block in app.model.snapshot())

    asyncio.run(scenario())


def test_unknown_skill_reports_error(monkeypatch):
    import skill_system

    monkeypatch.setattr(skill_system, 'load_skill',
                        lambda name: f'Error: skill {name} not found')
    monkeypatch.setattr(harness, 'agent_loop',
                        lambda messages, **kwargs: (_ for _ in ()).throw(
                            AssertionError('unknown skill must not run')))

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('/skill:nope')
            await pilot.pause()
            notice = app.model.snapshot()[-1]
            assert notice.kind == 'notice'
            assert 'Unknown skill' in notice.payload['text']

    asyncio.run(scenario())


def _no_agent(monkeypatch):
    monkeypatch.setattr(harness, 'agent_loop',
                        lambda messages, **kwargs: (_ for _ in ()).throw(
                            AssertionError('command must not reach model')))


def test_diff_and_sandbox_commands(monkeypatch):
    _no_agent(monkeypatch)
    monkeypatch.setattr(harness, 'format_workspace_diff',
                        lambda color=False: ' M src/app.py')
    monkeypatch.setattr(harness, 'apply_sandbox_tier',
                        lambda arg: f'switched to {arg}')

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('/diff')
            await pilot.pause()
            assert 'M src/app.py' in app.model.snapshot()[-1].payload['text']

            app.submit_text('/sandbox read-only')
            await pilot.pause()
            assert 'switched to read-only' in app.model.snapshot()[-1].payload['text']

    asyncio.run(scenario())


def test_cache_command_delegates_to_tool_cache(monkeypatch):
    _no_agent(monkeypatch)
    calls = []
    monkeypatch.setattr(harness.tool_cache, 'handle_cache_command',
                        lambda root, sessions, argument: calls.append(
                            (str(root), str(sessions), argument))
                        or 'cache-report')

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('/cache gc 7')
            await pilot.pause()
            assert calls and calls[0][2] == 'gc 7'
            assert app.model.snapshot()[-1].payload['text'] == 'cache-report'

    asyncio.run(scenario())


def test_model_picker_updates_env(monkeypatch):
    _no_agent(monkeypatch)
    monkeypatch.setattr(harness, 'MODEL_WINDOWS',
                        {'alpha': 1000, 'beta': 2000})
    monkeypatch.delenv('LLM_MODEL_ID', raising=False)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 24)) as pilot:
            app.open_model_picker()
            await pilot.pause()
            screen = app.screen
            options = screen.query_one('#model-list')
            assert options.option_count == 2
            options.focus()
            await pilot.press('enter')
            await pilot.pause()
            assert harness.os.environ['LLM_MODEL_ID'] in ('alpha', 'beta')

    asyncio.run(scenario())


def test_review_runs_in_background_and_updates_block(monkeypatch):
    import review

    _no_agent(monkeypatch)

    def fake_review(progress=None):
        if progress:
            progress('review: scanning')
        return 'review: 1 issue found'

    monkeypatch.setattr(review, 'run_review', fake_review)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('/review')
            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.02)
                block = app.model.snapshot()[-1]
                if '1 issue' in str(block.payload.get('text', '')):
                    break
            assert '1 issue' in app.model.snapshot()[-1].payload['text']

    asyncio.run(scenario())


def _temp_store(tmp_path, messages=({'role': 'user', 'content': 'hello'},)):
    import harness
    store = harness.SessionEventStore(tmp_path)
    for message in messages:
        store.append_message(dict(message))
    return store


def test_session_rename_fork_and_new(tmp_path, monkeypatch):
    import harness
    store = _temp_store(tmp_path)

    def fake_new(workspace=None, session_id=None):
        return harness.SessionEventStore(tmp_path / 'new')

    monkeypatch.setattr(harness, 'start_new_session', fake_new)

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('/rename Fresh title')
            await pilot.pause()
            assert app._title == 'Fresh title'
            assert store.session_summary()['title'] == 'Fresh title'

            old_id = app._session_id
            app.submit_text('/clone')
            await pilot.pause()
            assert app._session_id != old_id
            assert app._store.session_id == app._session_id
            assert any(block.kind == 'user' for block in app.model.snapshot())

            app.submit_text('/new')
            await pilot.pause()
            assert app._session_id != old_id
            assert app.model.snapshot()[-1].kind == 'notice'

    asyncio.run(scenario())


def test_checkpoints_and_compact_commands(tmp_path, monkeypatch):
    import harness
    store = _temp_store(tmp_path)
    monkeypatch.setattr(harness, 'get_checkpoint_manager',
                        lambda store: object())
    monkeypatch.setattr(harness, 'format_checkpoint_list',
                        lambda manager: 'cp-1  manual')
    monkeypatch.setattr(harness, 'compact_history',
                        lambda messages: [{'role': 'user',
                                           'content': '[Compacted]\n\nsummary'}])
    monkeypatch.setattr(harness, '_record_compaction',
                        lambda store, messages, strategy: None)

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('/checkpoints')
            await pilot.pause()
            assert 'cp-1' in app.model.snapshot()[-1].payload['text']

            app.submit_text('/compact')
            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.02)
                text = str(app.model.snapshot()[-1].payload.get('text', ''))
                if '已压缩' in text:
                    break
            assert '已压缩' in app.model.snapshot()[-1].payload['text']

    asyncio.run(scenario())


def test_fork_picker_forks_before_selected_message(tmp_path):
    store = _temp_store(tmp_path, (
        {'role': 'user', 'content': 'first question'},
        {'role': 'assistant', 'content': 'answer'},
        {'role': 'user', 'content': 'second question'},
    ))

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(80, 24)) as pilot:
            app.submit_text('/fork')
            await pilot.pause()
            options = app.screen.query_one('#fork-list')
            assert options.option_count == 2
            assert options.highlighted == 1        # 默认最新一条
            await pilot.press('enter')
            await pilot.pause()
            assert app._session_id != store.session_id
            assert app._composer.text == 'second question'
            contents = [str(m.get('content')) for m in app._messages]
            assert any('first question' in text for text in contents)
            assert all('second question' not in text for text in contents)

    asyncio.run(scenario())


def test_fork_picker_cancel_keeps_session(tmp_path):
    store = _temp_store(tmp_path)

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(80, 24)) as pilot:
            old = app._session_id
            app.submit_text('/fork')
            await pilot.pause()
            await pilot.press('escape')
            await pilot.pause()
            assert app._session_id == old

    asyncio.run(scenario())


def test_ctrl_c_exits_app_and_modal(tmp_path):
    store = _temp_store(tmp_path)

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press('ctrl+c')
            await pilot.pause()
            assert not app.is_running

        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(80, 24)) as pilot:
            app.submit_text('/fork')
            await pilot.pause()
            assert type(app.screen).__name__ == 'ForkPickerModal'
            await pilot.press('ctrl+c')
            await pilot.pause()
            assert not app.is_running

    asyncio.run(scenario())


def test_selector_keyboard_and_mouse(tmp_path):
    store = _temp_store(tmp_path, (
        {'role': 'user', 'content': 'first'},
        {'role': 'user', 'content': 'second'},
    ))

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(80, 24)) as pilot:
            app.submit_text('/fork')
            await pilot.pause()
            options = app.screen.query_one('#fork-list')
            assert options.highlighted == 1
            await pilot.press('up')
            assert options.highlighted == 0
            await pilot.press('down')
            assert options.highlighted == 1
            # 鼠标点第一行：应直接选中并关闭弹窗
            await pilot.click(options, offset=(4, 1))
            await pilot.pause()
            assert type(app.screen).__name__ == 'Screen'

    asyncio.run(scenario())


def test_completion_up_down_and_mouse(monkeypatch):
    _no_agent(monkeypatch)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 24)) as pilot:
            app._suggestions = [('/help', '/help'), ('/hotkeys', '/hotkeys')]
            app._suggestion_index = 0
            app._popup.show(app._suggestions, 0)
            app._composer.focus()
            await pilot.press('down')
            assert app._suggestion_index == 1
            await pilot.press('up')
            assert app._suggestion_index == 0
            app._popup.show(app._suggestions, 0)
            app._place_popup()
            await pilot.click('#completions', offset=(4, 1))
            await pilot.pause()
            assert app._composer.text == '/hotkeys'
            assert not app.has_suggestions()

    asyncio.run(scenario())


def test_enter_accepts_selected_command_before_submitting(monkeypatch):
    _no_agent(monkeypatch)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 24)) as pilot:
            app._composer.focus()
            app._composer.text = '/he'
            await pilot.pause()
            assert app.has_suggestions()
            await pilot.press('enter')
            await pilot.pause()
            assert not app.has_suggestions()
            assert app._composer.text == ''
            assert 'TUI commands' in app.model.snapshot()[-1].payload['text']

    asyncio.run(scenario())


def test_enter_accepts_path_completion_without_submitting(monkeypatch):
    _no_agent(monkeypatch)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 24)) as pilot:
            app._composer.focus()
            app._composer.text = '@src/ap'
            app._suggestions = [('@src/app.py', 'src/app.py')]
            app._suggestion_index = 0
            app._popup.show(app._suggestions, 0)
            app._place_popup()
            await pilot.press('enter')
            await pilot.pause()
            assert app._composer.text == '@src/app.py'
            assert not app.model.snapshot()   # 仅回填，不发送

    asyncio.run(scenario())


def test_tool_click_cycles_preview_lines(monkeypatch):
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 24)) as pilot:
            app._apply(UIEvent('tool_start', {
                'name': 'bash', 'args': {'command': 'seq 60'}, 'call_id': 'c1'}))
            app._apply(UIEvent('tool_end', {
                'name': 'bash', 'call_id': 'c1', 'outcome': 'completed',
                'ok': True, 'output_preview': '\n'.join(str(i) for i in range(60)),
                'duration_ms': 5}))
            await pilot.pause()
            app._flush_layout()
            block = app.model.get('tool-c1')
            assert block.payload.get('preview_lines') is None

            entry = next(item for item in app.doc_layout.entries()
                         if item.block_id == 'tool-c1')
            offset = next(i for i, rendered in enumerate(entry.lines)
                          if 'show more' in str(rendered))
            line = entry.start + offset
            app.on_transcript_click(line)
            await pilot.pause()
            assert block.payload['preview_lines'] == 40
            entry = next(item for item in app.doc_layout.entries()
                         if item.block_id == 'tool-c1')
            assert any('show less' in str(rendered) for rendered in entry.lines)

            app.on_transcript_click(line)
            await pilot.pause()
            assert block.payload['preview_lines'] == 10

    asyncio.run(scenario())


def test_final_answer_fallback_when_events_missing(monkeypatch):
    monkeypatch.setattr(harness, 'agent_loop',
                        lambda messages, **kwargs: 'fallback answer')

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('question')
            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if any(b.kind == 'assistant' for b in app.model.snapshot()):
                    break
            assistants = [b for b in app.model.snapshot()
                          if b.kind == 'assistant']
            assert assistants and assistants[-1].payload['text'] == 'fallback answer'
            assert 'fallback answer' in '\n'.join(
                str(app.view.render_line(i).text)
                for i in range(app.view.size.height))

    asyncio.run(scenario())


def test_final_answer_fallback_does_not_duplicate_stream(monkeypatch):
    def fake_loop(messages, **kwargs):
        ui_events.bus.emit('assistant_start', mid='stream-1')
        ui_events.bus.emit('assistant_delta', mid='stream-1', delta='hello ')
        ui_events.bus.emit('assistant_delta', mid='stream-1', delta='world')
        ui_events.bus.emit('assistant_end', mid='stream-1')
        return 'hello world'

    monkeypatch.setattr(harness, 'agent_loop', fake_loop)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('question')
            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.02)
                assistants = [b for b in app.model.snapshot()
                              if b.kind == 'assistant']
                if assistants and not assistants[-1].payload.get('streaming'):
                    break
            assistants = [b for b in app.model.snapshot()
                          if b.kind == 'assistant']
            assert [b.payload['text'] for b in assistants] == ['hello world']

    asyncio.run(scenario())


def test_worker_passes_current_session_store(tmp_path, monkeypatch):
    store = _temp_store(tmp_path)
    seen = {}

    def fake_loop(messages, **kwargs):
        seen.update(kwargs)
        ui_events.bus.emit('assistant_text', text='from store',
                           session_id=store.session_id)
        return 'from store'

    monkeypatch.setattr(harness, 'agent_loop', fake_loop)

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('question')
            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if any(b.kind == 'assistant' for b in app.model.snapshot()):
                    break
            assert seen.get('session_store') is store
            assert any(b.kind == 'assistant' for b in app.model.snapshot())

    asyncio.run(scenario())


def test_footer_shows_context_and_cache_hit_rate():
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(100, 20)) as pilot:
            app._apply(UIEvent('status', {
                'ctx_pct': 25.0, 'ctx_tokens': 50_000,
                'ctx_window': 200_000, 'tokens': 12_000,
                'prompt_tokens': 50_000, 'cached_tokens': 25_000,
                'cost': '0.0123', 'cost_source': 'builtin',
            }))
            await pilot.pause()
            footer = str(app._footer.render())
            assert 'ctx 25%' in footer
            assert 'cache 50%' in footer

    asyncio.run(scenario())


def test_streaming_assistant_renders_markdown_immediately():
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)):
            app._apply(UIEvent('assistant_start', {'mid': 'md-1'}))
            app._apply(UIEvent('assistant_delta', {
                'mid': 'md-1', 'delta': '**bold** and `code`\n'}))
            app._flush_layout()
            lines = app.doc_layout.entries()[0].lines
            text = '\n'.join(str(line) for line in lines)
            assert '**' not in text and '`' not in text
            assert 'bold' in text and 'code' in text

    asyncio.run(scenario())


def test_markdown_theme_has_no_black_code_background():
    from rich.markdown import Markdown

    from ui.harness_app import _render_lines

    lines = _render_lines(Markdown('`inline`\n\n```python\nprint(1)\n```'), 60)
    styles = [span.style for line in lines for span in line.spans]
    assert not any(getattr(style, 'bgcolor', None) is not None
                   and getattr(style.bgcolor, 'name', '') == 'black'
                   for style in styles)


def test_footer_starts_with_cwd_and_hides_placeholder_title(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)
    monkeypatch.setattr(harness, 'current_branch', lambda root: 'main')

    async def scenario():
        app = HarnessApp([], title='新会话')
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            footer = str(app._footer.render())
            assert '新会话' not in footer
            assert footer.startswith(tmp_path.name)
            assert '(main)' in footer

    asyncio.run(scenario())
