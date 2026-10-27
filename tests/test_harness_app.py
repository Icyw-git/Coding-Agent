"""新 TUI 骨架：历史 → 块模型 → 虚拟视口；流式与工具事件。"""
import asyncio
import threading
import time

import pytest

pytest.importorskip('textual')

import harness  # noqa: E402
import ui_events  # noqa: E402
from ui.event_reduction import HISTORY_INITIAL_MESSAGES, HISTORY_PAGE_MESSAGES  # noqa: E402
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
            loaded = min(len(messages), HISTORY_INITIAL_MESSAGES)
            assert len(app.model) == loaded
            assert app.view.content_line_count >= loaded
            assert app.model.snapshot()[0].payload['text'] == f'history-{300 - loaded}'
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


def test_turn_slot_busy_keeps_text_in_composer(monkeypatch):
    import turn_coordinator

    def busy(*args, **kwargs):
        raise turn_coordinator.TurnBusy('另一个入口正在执行回合')

    monkeypatch.setattr(turn_coordinator, 'acquire', busy)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('keep me')
            await pilot.pause()
            assert app._composer.text == 'keep me'
            assert not [block for block in app.model.snapshot()
                        if block.kind == 'user']
            notices = [block.payload.get('text', '')
                       for block in app.model.snapshot()
                       if block.kind == 'notice']
            assert any('另一个入口' in text for text in notices)
            assert not app._busy

    asyncio.run(scenario())


def test_turn_slot_released_after_turn(monkeypatch):
    """worker 结束后释放槽：下一个提交仍能执行。"""
    calls = []

    def fake_loop(messages, **kwargs):
        calls.append(len(messages))
        return 'ok'

    monkeypatch.setattr(harness, 'agent_loop', fake_loop)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            for index, text in enumerate(('one', 'two'), 1):
                app.submit_text(text)
                # 这里用的是真实回合锁（.cache/locks/agent-turn.lock），机器上同时跑着
                # 网关/TUI 时可能被短暂占用；给足 10s，不用固定次数的短等待，
                # 否则并行验收期间会偶发失败（本机 2026-10-25 实际遇到）。
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and len(calls) < index:
                    await pilot.pause()
                    await asyncio.sleep(0.02)
                assert not app._busy, text
                assert len(calls) == index, f'{text} 未被接受（可能被别的进程占用回合槽）'
            assert len(calls) == 2

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

            app.submit_text('two', mode='followup')
            app.submit_text('three', mode='followup')
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


def test_session_picker_sort_scope_search_and_preview(tmp_path, monkeypatch):
    workspace_a = tmp_path / 'workspace-a'
    workspace_b = tmp_path / 'workspace-b'
    workspace_a.mkdir()
    workspace_b.mkdir()
    rows = [
        {'id': 'session-alpha', 'title': 'Alpha', 'workspace': str(workspace_a),
         'created': 10, 'updated': 90, 'preview': 'alpha summary'},
        {'id': 'session-beta', 'title': 'Beta', 'workspace': str(workspace_b),
         'created': 30, 'updated': 20, 'preview': 'beta-only summary'},
        {'id': 'session-gamma', 'title': 'Gamma', 'workspace': str(workspace_a),
         'created': 40, 'updated': 60, 'preview': 'gamma details'},
    ]
    rows.extend(
        {
            'id': f'older-{index:02}',
            'title': f'Zeta {index:02}',
            'workspace': str(workspace_b),
            'created': 1,
            'updated': 1,
            'preview': f'older summary {index}',
        }
        for index in range(12)
    )
    monkeypatch.setattr(harness, 'session_rows', lambda **kwargs: rows)
    monkeypatch.setattr(harness, 'get_workdir', lambda: workspace_a)

    def option_ids(options):
        return [str(options.get_option_at_index(index).id)
                for index in range(options.option_count)]

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 24)) as pilot:
            app.open_session_picker()
            await pilot.pause()
            modal = app.screen
            box = modal.query_one('#session-box')
            assert box.region.right <= app.size.width
            assert box.region.bottom <= app.size.height
            options = modal.query_one('#session-list')
            assert options.option_count == 15
            assert options.region.height <= 12
            assert option_ids(options)[:3] == [
                'session-alpha', 'session-gamma', 'session-beta']
            assert 'alpha summary' in str(
                modal.query_one('#session-preview').render())
            # Shortcuts work while the search input owns focus.
            await pilot.press('alt+s')
            await pilot.pause()
            assert option_ids(options)[:3] == [
                'session-gamma', 'session-beta', 'session-alpha']
            await pilot.press('alt+s')
            await pilot.pause()
            assert option_ids(options)[:3] == [
                'session-alpha', 'session-beta', 'session-gamma']

            await pilot.press('alt+w')
            await pilot.pause()
            assert option_ids(options) == ['session-alpha', 'session-gamma']

            # Moving the list highlight updates the selected session preview.
            await pilot.press('down')
            await pilot.press('down')
            await pilot.pause()
            assert 'gamma details' in str(
                modal.query_one('#session-preview').render())

            search = modal.query_one('#session-search')
            search.value = 'beta-only'
            await pilot.pause()
            assert options.option_count == 0
            assert 'No matching sessions.' in str(
                modal.query_one('#session-preview').render())

            # All-workspace scope restores the matching remote session.
            await pilot.press('alt+w')
            await pilot.pause()
            assert option_ids(options) == ['session-beta']
            assert 'beta-only summary' in str(
                modal.query_one('#session-preview').render())

    asyncio.run(scenario())


def _picker_session(sessions_dir, session_id, text, workspace):
    store = harness.SessionEventStore(sessions_dir, session_id=session_id)
    store.record_workspace(workspace)
    store.append_message({'role': 'user', 'content': text})
    return store


def test_session_picker_renames_selected_session(tmp_path, monkeypatch):
    sessions_dir = tmp_path / 'sessions'
    checkpoints_dir = tmp_path / 'checkpoints'
    current = _picker_session(sessions_dir, 'current', 'current request', tmp_path)
    target = _picker_session(sessions_dir, 'rename-target', 'target request', tmp_path)
    monkeypatch.setattr(harness, 'SESSIONS_DIR', sessions_dir)
    monkeypatch.setattr(harness, 'CHECKPOINT_DIR', checkpoints_dir)

    async def scenario():
        app = HarnessApp(current.rebuild_openai_history(),
                         session_id=current.session_id, session_store=current)
        async with app.run_test(size=(90, 28)) as pilot:
            app.open_session_picker()
            await pilot.pause()
            search = app.screen.query_one('#session-search')
            search.value = 'rename-target'
            await pilot.pause()
            await pilot.press('alt+r')
            await pilot.pause()
            assert type(app.screen).__name__ == 'RenameSessionModal'
            editor = app.screen.query_one('#rename-session-input')
            editor.value = 'Renamed target'
            await pilot.press('enter')
            await pilot.pause()
            updated = harness.SessionEventStore.open(
                harness.resolve_session_path(target.session_id))
            assert updated.session_summary()['title'] == 'Renamed target'
            assert app.model.snapshot()[-1].payload['text'] == (
                'Session title: Renamed target')

    asyncio.run(scenario())


def test_session_picker_archive_confirms_and_protects_current(tmp_path, monkeypatch):
    sessions_dir = tmp_path / 'sessions'
    checkpoints_dir = tmp_path / 'checkpoints'
    current = _picker_session(sessions_dir, 'current', 'current request', tmp_path)
    target = _picker_session(sessions_dir, 'archive-target', 'target request', tmp_path)
    monkeypatch.setattr(harness, 'SESSIONS_DIR', sessions_dir)
    monkeypatch.setattr(harness, 'CHECKPOINT_DIR', checkpoints_dir)
    monkeypatch.setattr(harness, 'wait_for_housekeeping', lambda: None)
    monkeypatch.setattr(harness, 'housekeeping_busy', lambda: False)

    async def wait_for_screen(pilot, name):
        for _ in range(80):
            await pilot.pause()
            await asyncio.sleep(0.01)
            if type(app.screen).__name__ == name:
                return
        raise AssertionError(f'{name} did not open')

    async def scenario():
        nonlocal app
        app = HarnessApp(current.rebuild_openai_history(),
                         session_id=current.session_id, session_store=current)
        async with app.run_test(size=(90, 28)) as pilot:
            app.open_session_picker()
            await pilot.pause()
            search = app.screen.query_one('#session-search')
            search.value = current.session_id
            await pilot.pause()
            await pilot.press('alt+a')
            await pilot.pause()
            assert current.path.is_file()
            assert 'Cannot archive the current session.' in _notice_texts(app)[-1]

            app.open_session_picker()
            await pilot.pause()
            search = app.screen.query_one('#session-search')
            search.value = target.session_id
            await pilot.pause()
            await pilot.press('alt+a')
            await wait_for_screen(pilot, 'ConfirmModal')
            await pilot.press('n')
            await pilot.pause()
            assert target.path.is_file()
            archive_dir = sessions_dir / '.archive' / target.session_id
            assert not archive_dir.exists()

            app.open_session_picker()
            await pilot.pause()
            search = app.screen.query_one('#session-search')
            search.value = target.session_id
            await pilot.pause()
            await pilot.press('alt+a')
            await wait_for_screen(pilot, 'ConfirmModal')
            await pilot.press('y')
            for _ in range(100):
                await pilot.pause()
                await asyncio.sleep(0.01)
                if (archive_dir / target.path.name).is_file():
                    break
            assert not target.path.exists()
            assert (archive_dir / target.path.name).is_file()

    app = None
    asyncio.run(scenario())


def test_archived_picker_restores_and_requires_phrase_to_delete(tmp_path, monkeypatch):
    sessions_dir = tmp_path / 'sessions'
    checkpoints_dir = tmp_path / 'checkpoints'
    current = _picker_session(sessions_dir, 'current', 'current request', tmp_path)
    target = _picker_session(sessions_dir, 'archived-target', 'target request', tmp_path)
    monkeypatch.setattr(harness, 'SESSIONS_DIR', sessions_dir)
    monkeypatch.setattr(harness, 'CHECKPOINT_DIR', checkpoints_dir)
    monkeypatch.setattr(harness, 'wait_for_housekeeping', lambda: None)
    monkeypatch.setattr(harness, 'housekeeping_busy', lambda: False)
    from session_cleanup import SessionArchive

    archive = SessionArchive(sessions_dir, checkpoints_dir)
    archive.archive(target.session_summary(), current.session_id)
    archive_dir = sessions_dir / '.archive' / target.session_id

    async def wait_for_archived_rows(pilot):
        for _ in range(80):
            await pilot.pause()
            await asyncio.sleep(0.01)
            if (app.screen.query_one('#session-list').option_count
                    and app.screen.query_one('#session-list').get_option_at_index(0).id
                    == target.session_id):
                return
        raise AssertionError('archived session did not load')

    async def wait_for_screen(pilot, name):
        for _ in range(80):
            await pilot.pause()
            await asyncio.sleep(0.01)
            if type(app.screen).__name__ == name:
                return
        raise AssertionError(f'{name} did not open')

    async def scenario():
        nonlocal app
        app = HarnessApp(current.rebuild_openai_history(),
                         session_id=current.session_id, session_store=current)
        async with app.run_test(size=(90, 28)) as pilot:
            app.open_session_picker()
            await pilot.pause()
            await pilot.press('alt+v')
            await wait_for_archived_rows(pilot)
            await pilot.press('enter')
            await wait_for_screen(pilot, 'ConfirmModal')
            await pilot.press('y')
            for _ in range(100):
                await pilot.pause()
                await asyncio.sleep(0.01)
                if target.path.is_file():
                    break
            assert target.path.is_file()
            assert not archive_dir.exists()

            archive.archive(target.session_summary(), current.session_id)
            app.open_session_picker()
            await pilot.pause()
            await pilot.press('alt+v')
            await wait_for_archived_rows(pilot)
            await pilot.press('alt+d')
            await wait_for_screen(pilot, 'TypedConfirmModal')
            typed = app.screen.query_one('#typed-input')
            typed.value = 'wrong'
            await pilot.press('enter')
            await pilot.pause()
            assert archive_dir.exists()
            assert '请输入“删除”' in str(
                app.screen.query_one('#typed-error').render())
            typed.value = '删除'
            await pilot.press('enter')
            for _ in range(100):
                await pilot.pause()
                await asyncio.sleep(0.01)
                if not archive_dir.exists():
                    break
            assert not archive_dir.exists()

    app = None
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


def test_permission_request_can_allow_same_kind_for_session():
    resolved = []

    def record(event):
        if event.type == 'permission_resolve':
            resolved.append(event)

    ui_events.bus.subscribe(record)
    try:
        async def scenario():
            app = HarnessApp([])
            async with app.run_test(size=(80, 24)) as pilot:
                app._apply(
                    UIEvent(
                        'permission_request',
                        {
                            'req_id': 'perm-a',
                            'tool': 'bash',
                            'args': {'command': 'del x'},
                            'title': 'destructive',
                            'allow_session': True,
                        },
                    )
                )
                await pilot.pause()
                assert type(app.screen).__name__ == 'PermissionModal'
                await asyncio.sleep(0.25)
                await pilot.press('a')
                await pilot.pause()
                assert resolved and resolved[-1].payload['approved'] is True
                assert resolved[-1].payload['scope'] == 'session'

        asyncio.run(scenario())
    finally:
        ui_events.bus.unsubscribe(record)


def test_permission_request_ignores_session_key_when_classless():
    resolved = []

    def record(event):
        if event.type == 'permission_resolve':
            resolved.append(event)

    ui_events.bus.subscribe(record)
    try:
        async def scenario():
            app = HarnessApp([])
            async with app.run_test(size=(80, 24)) as pilot:
                app._apply(
                    UIEvent(
                        'permission_request',
                        {
                            'req_id': 'perm-b',
                            'tool': 'bash',
                            'args': {'command': 'python -c "import os"'},
                            'title': 'opaque',
                            'allow_session': False,
                        },
                    )
                )
                await pilot.pause()
                await asyncio.sleep(0.25)
                await pilot.press('a')
                await pilot.pause()
                assert resolved == []
                await pilot.press('n')

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


def test_bang_shell_failure_restores_ui_gate_and_busy_state(monkeypatch):
    def fail_shell(command):
        raise RuntimeError(f'failed: {command}')

    def fail_loop(messages, **kwargs):
        raise AssertionError('shell must not reach the model')

    monkeypatch.setattr(harness, 'agent_loop', fail_loop)
    monkeypatch.setattr(harness, 'run_bash', fail_shell)
    monkeypatch.setattr(harness.permission_gate, 'use_ui', False)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('!bad-command')
            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.01)
                block = next((b for b in app.model.snapshot()
                              if b.kind == 'tool'), None)
                if block and block.payload.get('state') != 'running':
                    break
            block = next(b for b in app.model.snapshot() if b.kind == 'tool')
            assert block.payload['state'] == 'failed'
            assert 'failed: bad-command' in block.payload['output']
            assert not app._busy
            assert not harness.permission_gate.use_ui

            app._busy = True
            app.submit_text('!must-not-run')
            assert len([b for b in app.model.snapshot() if b.kind == 'tool']) == 1
            assert '尚未结束' in app.model.snapshot()[-1].payload['text']

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
            assert 'Ctrl+F' in hotkeys and 'Shift+Enter' in hotkeys
            assert 'Ctrl+Shift+P' in hotkeys

    asyncio.run(scenario())


def test_command_palette_filters_dispatches_and_prefills_arguments():
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press('ctrl+shift+p')
            await pilot.pause()
            modal = app.screen
            assert type(modal).__name__ == 'CommandPaletteModal'
            box = modal.query_one('#command-palette-box')
            assert box.region.x >= 0 and box.region.right <= app.size.width
            assert box.region.y >= 0 and box.region.bottom <= app.size.height
            options = modal.query_one('#command-palette-list')
            assert options.get_option_at_index(0).id == 'group:commands'
            assert options.get_option_at_index(0).disabled

            search = modal.query_one('#command-palette-search')
            search.value = 'mdl'
            await pilot.pause()
            # 不写死条数：``memories`` 的描述里含 readonly，模糊匹配也会命中它。
            # 这里只关心「分组头 + 至少一个命中的命令」且第 0 项是禁止选中的分组。
            assert options.option_count >= 2
            ids = [options.get_option_at_index(i).id
                   for i in range(options.option_count)]
            assert ids[0] == 'group:commands'
            assert 'model' in ids

            search.value = 'find'
            await pilot.pause()
            await pilot.press('enter')
            await pilot.pause()
            assert app.screen is app.screen_stack[0]
            assert app._composer.text == '/find '
            assert app._composer.cursor_location == (0, len('/find '))

            await pilot.press('ctrl+shift+p')
            await pilot.pause()
            modal = app.screen
            search = modal.query_one('#command-palette-search')
            search.value = 'status'
            await pilot.pause()
            await pilot.press('enter')
            await pilot.pause()
            assert app.screen is app.screen_stack[0]
            assert app.model.snapshot()[-1].kind == 'notice'
            assert 'workspace:' in app.model.snapshot()[-1].payload['text']

            await pilot.press('ctrl+shift+p')
            await pilot.pause()
            modal = app.screen
            search = modal.query_one('#command-palette-search')
            options = modal.query_one('#command-palette-list')
            search.value = 'zzzz-no-command'
            await pilot.pause()
            assert options.option_count == 0
            assert 'No matching commands' in str(
                modal.query_one('#command-palette-empty').render())

            await pilot.press('escape')
            await pilot.pause()
            assert app.screen is app.screen_stack[0]

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

            app.submit_text('/help archive')
            await pilot.pause()
            text = app.model.snapshot()[-1].payload['text']
            assert '/archive' in text and '归档旧会话' in text

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


def test_model_picker_searches_marks_current_and_updates_runtime(monkeypatch):
    _no_agent(monkeypatch)
    monkeypatch.setattr(harness, 'MODEL_WINDOWS',
                        {'alpha': 1000, 'beta': 2000})
    monkeypatch.setenv('LLM_MODEL_ID', 'beta')
    monkeypatch.setenv('HARNESS_MODEL_MAIN', '')

    async def scenario():
        app = HarnessApp([])
        app._status['ctx_tokens'] = 500
        async with app.run_test(size=(80, 24)) as pilot:
            app.open_model_picker()
            await pilot.pause()
            screen = app.screen
            box = screen.query_one('#model-box')
            assert box.region.x >= 0 and box.region.right <= app.size.width
            assert box.region.y >= 0 and box.region.bottom <= app.size.height
            options = screen.query_one('#model-list')
            assert options.option_count == 2
            assert options.get_option_at_index(options.highlighted).id == 'beta'
            assert 'current' in str(
                options.get_option_at_index(options.highlighted).prompt)
            assert '2,000 tokens' in str(
                options.get_option_at_index(options.highlighted).prompt)

            search = screen.query_one('#model-search')
            search.value = 'alp'
            await pilot.pause()
            assert options.option_count == 1
            assert options.get_option_at_index(0).id == 'alpha'
            await pilot.press('enter')
            await pilot.pause()
            assert harness.os.environ['HARNESS_MODEL_MAIN'] == 'alpha'
            assert harness.os.environ['LLM_MODEL_ID'] == 'alpha'
            assert app._status['model'] == 'alpha'
            assert app._status['ctx_window'] == 1000
            assert app._status['ctx_pct'] == 50

    asyncio.run(scenario())


def test_model_picker_switches_configured_provider_and_context(monkeypatch):
    _no_agent(monkeypatch)
    config = '''
providers:
  default: {api_key_env: MAIN_KEY}
  alternate: {api_key_env: ALT_KEY}
models:
  main-ref: {provider: default, model: main-api, context_window: 100000}
  other-ref: {provider: alternate, model: other-api, context_window: 200000}
roles: {main: main-ref}
'''
    monkeypatch.setenv('HARNESS_LLM_CONFIG', config)
    monkeypatch.setenv('HARNESS_MODEL_MAIN', '')
    monkeypatch.setenv('LLM_MODEL_ID', '')
    monkeypatch.delenv('LLM_CONTEXT_WINDOW', raising=False)

    async def scenario():
        app = HarnessApp([])
        app._status['ctx_tokens'] = 50_000
        app._status['model'] = 'other-api'
        async with app.run_test(size=(80, 24)) as pilot:
            app.open_model_picker()
            await pilot.pause()
            screen = app.screen
            options = screen.query_one('#model-list')
            assert options.get_option_at_index(options.highlighted).id == 'other-ref'
            assert 'alternate/other-api' in str(
                options.get_option_at_index(options.highlighted).prompt)
            assert '200,000 tokens' in str(
                options.get_option_at_index(options.highlighted).prompt)
            search = screen.query_one('#model-search')
            search.value = 'main-ref'
            await pilot.pause()
            assert options.option_count == 1
            await pilot.press('enter')
            await pilot.pause()
            assert harness.os.environ['HARNESS_MODEL_MAIN'] == 'main-ref'
            assert harness.os.environ['LLM_MODEL_ID'] == 'main-api'
            assert harness.model_for('main') == 'main-api'
            assert app._status['ctx_window'] == 100_000
            assert app._status['ctx_pct'] == 50

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
                'model': 'runtime-fallback',
                'ctx_pct': 25.0, 'ctx_tokens': 50_000,
                'ctx_window': 200_000, 'tokens': 12_000,
                'prompt_tokens': 50_000, 'cached_tokens': 25_000,
                'cost': '0.0123', 'cost_source': 'builtin',
            }))
            await pilot.pause()
            footer = str(app._footer.render())
            assert 'runtime-fallback' in footer
            assert 'ctx 25%' in footer
            assert 'cache 50%' in footer

    asyncio.run(scenario())


def test_footer_skips_unchanged_updates_but_refreshes_changed_state(monkeypatch):
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(100, 20)):
            updates = []
            original = app._footer.update

            def record_update(value):
                updates.append(str(value))
                return original(value)

            monkeypatch.setattr(app._footer, 'update', record_update)
            app._refresh_footer()
            app._refresh_footer()
            assert updates == []

            app._status['ctx_pct'] = 25.0
            app._refresh_footer()
            assert len(updates) == 1
            assert 'ctx 25%' in updates[0]
            app._refresh_footer()
            assert len(updates) == 1

            replacement = type(app._footer)()
            replacement_updates = []
            replacement_update = replacement.update

            def record_replacement_update(value):
                replacement_updates.append(str(value))
                return replacement_update(value)

            monkeypatch.setattr(replacement, 'update', record_replacement_update)
            app._footer = replacement
            app._refresh_footer()
            app._refresh_footer()
            assert len(replacement_updates) == 1
            assert 'ctx 25%' in replacement_updates[0]

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


def test_copy_last_tool_output(monkeypatch):
    copied = []
    async def scenario():
        app = HarnessApp([])
        monkeypatch.setattr(app, 'copy_to_clipboard', copied.append)
        async with app.run_test(size=(80, 20)) as pilot:
            app._apply(UIEvent('tool_start', {
                'name': 'bash', 'args': {'command': 'x'}, 'call_id': 'c1'}))
            app._apply(UIEvent('tool_end', {
                'name': 'bash', 'call_id': 'c1', 'outcome': 'completed',
                'ok': True, 'output_preview': 'line1\nline2', 'duration_ms': 1}))
            app.submit_text('/copy')
            await pilot.pause()
            assert copied == ['line1\nline2']
            assert app.model.snapshot()[-1].payload['text'] == (
                'Copied 10 non-whitespace chars from the last tool output.')
    asyncio.run(scenario())


def test_ctrl_c_copies_selection_instead_of_quitting(monkeypatch):
    copied = []
    async def scenario():
        app = HarnessApp([])
        monkeypatch.setattr(app, '_selected_text', lambda: 'selected text')
        monkeypatch.setattr(app, 'copy_to_clipboard', copied.append)
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.press('ctrl+c')
            await pilot.pause()
            assert copied == ['selected text']
            assert app.is_running
    asyncio.run(scenario())


def test_malformed_markdown_falls_back_to_text(monkeypatch):
    from ui import harness_app as module
    from ui import rendering

    monkeypatch.setattr(rendering, 'RichMarkdown',
                        lambda text: (_ for _ in ()).throw(ValueError('bad link')))
    lines = module._render_markdown('[broken](', 40)
    assert 'broken' in '\n'.join(str(line) for line in lines)


def test_permission_modal_is_centered_box_not_fullscreen():
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(100, 30)) as pilot:
            app._apply(UIEvent('permission_request', {
                'req_id': 'p', 'tool': 'bash',
                'args': {'command': 'rm x'}, 'title': 'confirm'}))
            await pilot.pause()
            box = app.screen.query_one('#perm-box')
            assert box.region.width < 100
            assert box.region.height < 30
    asyncio.run(scenario())


def test_steering_queue_and_dequeue_all(monkeypatch):
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
            app.submit_text('steer me', mode='steer')
            app.submit_text('follow up', mode='followup')
            await pilot.pause()
            assert app._steering.snapshot() == ['steer me']
            assert app._pending.snapshot() == ['follow up']
            # 多条时 Alt+Up 打开选择器，Ctrl+R 取回全部
            await pilot.press('alt+up')
            await pilot.pause()
            assert type(app.screen).__name__ == 'QueueModal'
            await pilot.press('ctrl+r')
            await pilot.pause()
            assert not app._steering.snapshot()
            assert not app._pending.snapshot()
            assert 'steer me' in app._composer.text
            assert 'follow up' in app._composer.text
            release.set()
            app._exiting = True
            app._pending.put(None)

    asyncio.run(scenario())


def test_cancel_restores_queued_messages(monkeypatch):
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
            app.submit_text('follow up', mode='followup')
            await pilot.pause()
            app.cancel_turn()
            await pilot.pause()
            assert not app._pending.snapshot()
            assert 'follow up' in app._composer.text
            release.set()
            app._exiting = True
            app._pending.put(None)

    asyncio.run(scenario())


def test_steering_does_not_render_until_agent_consumes_it():
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app._busy = True
            app.submit_text('steer now', mode='steer')
            await pilot.pause()
            assert app._steering.snapshot() == ['steer now']
            assert not any(block.kind == 'user' for block in app.model.snapshot())
            app._apply(UIEvent('user_message', {'text': 'steer now'}))
            await pilot.pause()
            assert any(block.kind == 'user' for block in app.model.snapshot())
            app._busy = False
    asyncio.run(scenario())


def test_checkpoint_rewind_and_undo_commands(tmp_path, monkeypatch):
    import harness
    snapshot = tmp_path / 'cp_messages.jsonl'
    snapshot.write_text(
        '{"role": "user", "content": "first"}\n'
        '{"role": "assistant", "content": "old answer"}\n',
        encoding='utf-8')

    class FakeManager:
        head = 'cp_test'

        def __init__(self):
            self.begun = None
            self.sealed = False

        def begin_turn(self, messages, store, label='', trigger='turn'):
            self.begun = (list(messages), label, trigger)
            return 'cp_test'

        def seal(self):
            self.sealed = True

        def resolve(self, ref):
            return 'cp_test'

        def snapshot_path(self, cp_id):
            return snapshot

        def plan_restore(self, cp_id, scope='all'):
            return {
                'cp_id': cp_id, 'label': 'manual', 'scope': scope,
                'actions': [], 'conflicts': [], 'unverified': [],
                'drift': [], 'applied': [], 'ok': True, 'reason': '',
            }

        def restore(self, cp_id, force=False, scope='all'):
            return self.plan_restore(cp_id, scope=scope)

    manager = FakeManager()
    monkeypatch.setattr(harness, 'get_checkpoint_manager', lambda store: manager)
    store = _temp_store(tmp_path, (
        {'role': 'user', 'content': 'first'},
        {'role': 'assistant', 'content': 'old answer'},
        {'role': 'user', 'content': 'second'},
    ))

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(80, 24)) as pilot:
            app.submit_text('/checkpoint before refactor')
            await pilot.pause()
            assert manager.begun[1] == 'before refactor'
            assert manager.begun[2] == 'manual'
            assert manager.sealed
            assert 'Created checkpoint' in app.model.snapshot()[-1].payload['text']

            app.submit_text('/rewind head')
            await pilot.pause()
            assert app.screen.query_one('#confirm-box') is not None
            await pilot.press('y')
            await pilot.pause()
            assert all('second' not in str(m.get('content'))
                       for m in app._messages)
            assert any('Rewound to cp_test' in str(m.get('content'))
                       for m in app._messages)

            app.submit_text('/undo extra')
            await pilot.pause()
            assert '用法：/undo' in app.model.snapshot()[-1].payload['text']

    asyncio.run(scenario())


def test_rewind_cancel_keeps_history(tmp_path, monkeypatch):
    import harness
    snapshot = tmp_path / 'cp_messages.jsonl'
    snapshot.write_text('{"role": "user", "content": "kept"}\n',
                        encoding='utf-8')

    class FakeManager:
        def resolve(self, ref):
            return 'cp_test'

        def snapshot_path(self, cp_id):
            return snapshot

        def plan_restore(self, cp_id, scope='all'):
            return {
                'cp_id': cp_id, 'label': 'manual', 'scope': scope,
                'actions': [], 'conflicts': [], 'unverified': [],
                'drift': [], 'applied': [], 'ok': True, 'reason': '',
            }

    monkeypatch.setattr(harness, 'get_checkpoint_manager',
                        lambda store: FakeManager())
    store = _temp_store(tmp_path, (
        {'role': 'user', 'content': 'kept'},
        {'role': 'user', 'content': 'later'},
    ))

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(80, 24)) as pilot:
            before = [dict(m) for m in app._messages]
            app.submit_text('/rewind last')
            await pilot.pause()
            await pilot.press('n')
            await pilot.pause()
            assert app._messages == before

    asyncio.run(scenario())


def _notice_texts(app):
    return [str(block.payload.get('text', ''))
            for block in app.model.snapshot()]


def test_find_opens_result_picker_and_resumes(tmp_path, monkeypatch):
    sessions = tmp_path / 'find-sessions'
    current = harness.SessionEventStore(sessions, session_id='current')
    current.append_message({'role': 'user', 'content': 'unrelated chat'})
    target = harness.SessionEventStore(sessions, session_id='found-one')
    target.append_message({'role': 'user', 'content': 'needle in haystack'})
    monkeypatch.setattr(harness, 'SESSIONS_DIR', sessions)
    _no_agent(monkeypatch)

    async def scenario():
        app = HarnessApp(current.rebuild_openai_history(),
                         session_id='current', session_store=current)
        async with app.run_test(size=(90, 28)) as pilot:
            app.submit_text('/find needle')
            for _ in range(100):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if type(app.screen).__name__ == 'ChoiceModal':
                    break
            assert type(app.screen).__name__ == 'ChoiceModal'
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if app.screen.query_one('#choice-list').option_count:
                    break
            assert app.screen.query_one('#choice-list').option_count == 1

            await pilot.press('enter')
            for _ in range(100):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if app._session_id == 'found-one':
                    break
            assert app._session_id == 'found-one'
            assert any('needle in haystack' in text
                       for text in _notice_texts(app))

    asyncio.run(scenario())


def _cleanup_fakes(monkeypatch, calls, rows, action='archive'):
    monkeypatch.setattr(harness, 'wait_for_housekeeping', lambda: None)
    monkeypatch.setattr(harness, 'housekeeping_busy', lambda: False)
    monkeypatch.setattr(harness, 'session_cleanup_plan',
                        lambda command, argument, current, **kwargs: (object(), rows))
    monkeypatch.setattr(
        harness, 'apply_session_cleanup',
        lambda archive, chosen, mode, current: (
            calls.append(([row['id'] for row in chosen], mode)) or (1, [])))
    _no_agent(monkeypatch)


def _cleanup_rows(tmp_path):
    return [{'id': 'old-one', 'title': 'Old task',
             'workspace': str(tmp_path), 'updated': 0.0}]


def test_archive_command_picker_and_confirmation(tmp_path, monkeypatch):
    store = _temp_store(tmp_path)
    rows = _cleanup_rows(tmp_path)
    calls = []
    _cleanup_fakes(monkeypatch, calls, rows)

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(90, 28)) as pilot:
            app.submit_text('/archive all')
            for _ in range(100):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if type(app.screen).__name__ == 'CleanupPickerModal':
                    break
            assert type(app.screen).__name__ == 'CleanupPickerModal'
            picker = app.screen
            picker.action_select_all()
            await pilot.pause()
            assert picker.selected == {'old-one'}

            await pilot.press('enter')
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if type(app.screen).__name__ == 'ConfirmModal':
                    break
            assert type(app.screen).__name__ == 'ConfirmModal'
            await pilot.press('y')
            for _ in range(100):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if any('已归档 1/1 个会话' in text
                       for text in _notice_texts(app)):
                    break
            assert calls == [(['old-one'], 'archive')]
            assert any('已归档 1/1 个会话' in text
                       for text in _notice_texts(app))

    asyncio.run(scenario())


def test_delete_requires_typed_confirmation(tmp_path, monkeypatch):
    store = _temp_store(tmp_path)
    rows = _cleanup_rows(tmp_path)
    calls = []
    _cleanup_fakes(monkeypatch, calls, rows, action='delete')

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(90, 28)) as pilot:
            app.submit_text('/delete')
            for _ in range(100):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if type(app.screen).__name__ == 'CleanupPickerModal':
                    break
            assert type(app.screen).__name__ == 'CleanupPickerModal'
            app.screen.action_select_all()
            await pilot.press('enter')

            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if type(app.screen).__name__ == 'TypedConfirmModal':
                    break
            assert type(app.screen).__name__ == 'TypedConfirmModal'
            modal = app.screen
            typed = modal.query_one('#typed-input')
            typed.focus()
            await pilot.press('enter')
            await pilot.pause()
            assert app.screen is modal
            assert '未执行改动' in str(modal.query_one('#typed-error').render())

            typed.value = '删除'
            await pilot.press('enter')
            for _ in range(100):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if calls:
                    break
            assert calls == [(['old-one'], 'delete')]

    asyncio.run(scenario())


def test_workspace_commands_and_cd(tmp_path, monkeypatch):
    store = _temp_store(tmp_path)
    target = tmp_path / 'switched'
    target.mkdir()
    switched = []
    monkeypatch.setattr(harness, 'format_workspace_list',
                        lambda: 'registered workspaces')
    monkeypatch.setattr(
        harness, 'remove_workspace_selector',
        lambda selector: {'name': 'old', 'path': str(target)})
    monkeypatch.setattr(harness, 'set_workspace',
                        lambda path: switched.append(str(path)) or target)
    _no_agent(monkeypatch)

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(90, 28)) as pilot:
            app.submit_text('/workspaces')
            await pilot.pause()
            assert app.model.snapshot()[-1].payload['text'] == 'registered workspaces'

            app.submit_text('/workspace remove 1')
            await pilot.pause()
            assert '已移除登记' in app.model.snapshot()[-1].payload['text']

            app.submit_text(f'/cd "{target}"')
            await pilot.pause()
            assert switched == [str(target)]
            assert app.model.snapshot()[-1].payload['text'] == f'工作区：{target}'
            assert store.session_summary()['workspace'] == str(target)

    asyncio.run(scenario())


def test_new_ws_uses_registered_workspace(tmp_path, monkeypatch):
    store = _temp_store(tmp_path)
    target = tmp_path / 'other-project'
    target.mkdir()
    captured = {}

    class FakeStore:
        session_id = 'new-ws'

        def session_summary(self):
            return {'title': 'New workspace session'}

    def fake_start_new(workspace=None, session_id=None):
        captured['workspace'] = workspace
        return FakeStore()

    monkeypatch.setattr(harness, 'registered_workspaces',
                        lambda: [{'id': 'ws-1', 'name': 'other',
                                  'path': str(target)}])
    monkeypatch.setattr(harness, 'start_new_session', fake_start_new)
    _no_agent(monkeypatch)

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(90, 28)) as pilot:
            app.submit_text('/new --ws 1')
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if app._session_id == 'new-ws':
                    break
            assert str(captured['workspace']) == str(target)
            assert app._session_id == 'new-ws'

    asyncio.run(scenario())


def test_watch_command_updates_notice_block(monkeypatch):
    import background_watch

    def fake_watch(argument, emit=None):
        emit('  ● bg_1  running  00:01  pytest -q')
        return '  [bg_1] completed\n  Output:\nok'

    monkeypatch.setattr(background_watch, 'watch_command', fake_watch)
    _no_agent(monkeypatch)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('/watch')
            for _ in range(100):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if any('Output:' in text for text in _notice_texts(app)):
                    break
            assert any('[bg_1] completed' in text
                       for text in _notice_texts(app))

    asyncio.run(scenario())


def test_checkpoint_gc_reports_other_sessions(tmp_path, monkeypatch):
    import harness
    from checkpoints import CheckpointManager

    root = tmp_path / 'checkpoints'
    store = _temp_store(tmp_path)
    current = CheckpointManager(root, tmp_path, store.session_id, max_nodes=20)
    other = CheckpointManager(root, tmp_path, 'other', max_nodes=20)
    for index in range(3):
        current.begin_turn([{'role': 'user', 'content': f'c{index}'}])
    for index in range(2):
        other.begin_turn([{'role': 'user', 'content': f'o{index}'}])
    current.seal()
    other.seal()
    monkeypatch.setattr(harness, 'CHECKPOINT_DIR', root)
    monkeypatch.setattr(harness, 'CHECKPOINT_MAX_NODES', 20)
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            app.submit_text('/checkpoint-gc')
            await pilot.pause()
            text = app.model.snapshot()[-1].payload['text']
            assert '当前会话无需清理' in text
            assert '其他会话还有 2 个快照' in text
            assert len(current.list(limit=100)) == 3

    asyncio.run(scenario())


def test_checkpoint_gc_all_previews_confirms_and_cleans(tmp_path, monkeypatch):
    import harness
    from checkpoints import CheckpointManager

    root = tmp_path / 'checkpoints'
    store = _temp_store(tmp_path)
    current = CheckpointManager(root, tmp_path, store.session_id, max_nodes=2)
    other = CheckpointManager(root, tmp_path, 'other', max_nodes=2)
    for index in range(4):
        current.begin_turn([{'role': 'user', 'content': f'c{index}'}])
    for index in range(3):
        other.begin_turn([{'role': 'user', 'content': f'o{index}'}])
    current.seal()
    other.seal()
    monkeypatch.setattr(harness, 'CHECKPOINT_DIR', root)
    monkeypatch.setattr(harness, 'CHECKPOINT_MAX_NODES', 2)
    monkeypatch.setattr(harness, 'get_workdir', lambda: tmp_path)

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            app.submit_text('/checkpoint-gc --all')
            await pilot.pause()
            assert app.screen.query_one('#confirm-box') is not None
            await pilot.press('n')
            await pilot.pause()
            assert len(current.list(limit=100)) == 4
            assert len(other.list(limit=100)) == 3

            app.submit_text('/checkpoint-gc --all')
            await pilot.pause()
            await pilot.press('y')
            await pilot.pause()
            assert len(current.list(limit=100)) == 2
            assert len(other.list(limit=100)) == 2
            assert '已清理 3 个快照' in app.model.snapshot()[-1].payload['text']

            app.submit_text('/checkpoint-gc --all --keep')
            await pilot.pause()
            assert '--keep' in app.model.snapshot()[-1].payload['text']

    asyncio.run(scenario())


def test_cache_profile_command_uses_storage_profile(monkeypatch):
    _no_agent(monkeypatch)
    monkeypatch.setattr(harness, 'format_storage_profile',
                        lambda: 'storage-profile-report')

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            app.submit_text('/cache profile')
            await pilot.pause()
            assert app.model.snapshot()[-1].payload['text'] == 'storage-profile-report'

    asyncio.run(scenario())


def test_markdown_console_cache_is_bounded():
    from ui import rendering as module

    module._CONSOLES.clear()
    try:
        for width in range(800, 820):
            module._console_for(width)
        assert len(module._CONSOLES) <= 16
    finally:
        module._CONSOLES.clear()


def test_textual_run_swaps_ansi_renderer_and_restores_it(monkeypatch):
    from renderer import default_renderer
    from ui import harness_app as module
    from ui_events import bus

    while default_renderer.on_event in bus._subs:
        bus.unsubscribe(default_renderer.on_event)
    bus.subscribe(default_renderer.on_event)
    seen = []

    class FakeApp:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            seen.append(default_renderer.on_event in bus._subs)

    monkeypatch.setattr(module, 'HarnessApp', FakeApp)
    module.run([])

    assert seen == [False]
    assert default_renderer.on_event in bus._subs


def test_history_starts_with_recent_window_and_loads_older_page():
    messages = []
    for index in range(1000):
        role = 'user' if index % 2 == 0 else 'assistant'
        messages.append({'role': role, 'content': f'history-{index}'})

    async def scenario():
        app = HarnessApp(messages)
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            initial_start = len(messages) - HISTORY_INITIAL_MESSAGES
            page = HISTORY_PAGE_MESSAGES
            assert len(app._messages) == 1000
            assert len(app.model) == HISTORY_INITIAL_MESSAGES
            assert app.model.snapshot()[0].payload['text'] == f'history-{initial_start}'
            assert app._history_start == initial_start

            before = app.view.content_line_count
            assert app.load_older_history() == min(page, initial_start)
            await pilot.pause()
            first_start = max(0, initial_start - page)
            assert app._history_start == first_start
            assert len(app.model) == 1000 - first_start
            assert app.model.snapshot()[0].payload['text'] == f'history-{first_start}'
            assert app.view.content_line_count > before
            assert app.load_older_history() == min(page, first_start)
            await pilot.pause()
            second_start = max(0, first_start - page)
            assert app._history_start == second_start
            assert app.model.snapshot()[0].payload['text'] == f'history-{second_start}'

    asyncio.run(scenario())


def test_transcript_search_navigates_full_history_and_highlights():
    messages = []
    for index in range(450):
        role = 'user' if index % 2 == 0 else 'assistant'
        text = f'history-{index}'
        if index in (2, 410):
            text = f'Needle result-{index}'
        messages.append({'role': role, 'content': text})

    async def scenario():
        app = HarnessApp(messages)
        async with app.run_test(size=(80, 24)) as pilot:
            app.model.append('assistant', {'text': 'Live needle result'},
                             block_id='live-response')
            app._mark_dirty()
            app._flush_layout(force=True)
            await pilot.press('ctrl+f')
            await pilot.pause()
            modal = app._transcript_search_modal
            editor = modal.query_one('#transcript-search-input')
            editor.value = 'needle'
            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.01)
                if len(app._transcript_search_hits) == 3:
                    break

            modal = app._transcript_search_modal
            assert modal is not None
            assert app._history_start == 0
            assert app._transcript_search_hits[0].block_id == 'history:2'
            assert app.view._search_active_block == 'history:2'
            status = modal.query_one('#transcript-search-status').render()
            assert '1 / 3 messages' in str(status)
            row = app.doc_layout.block_start('history:2')
            rendered = app.view._strip_for(row, app.view.size.width)
            assert any('needle' in segment.text.casefold()
                       and segment.style is not None
                       and segment.style.bgcolor is not None
                       for segment in rendered._segments)

            await pilot.press('enter')
            await pilot.pause()
            assert app._transcript_search_index == 1
            assert app.view._search_active_block == 'history:410'
            await pilot.press('enter')
            await pilot.pause()
            assert app._transcript_search_index == 2
            assert app.view._search_active_block == 'live-response'
            await pilot.press('enter')
            await pilot.pause()
            assert app._transcript_search_index == 0
            await pilot.press('shift+enter')
            await pilot.pause()
            assert app._transcript_search_index == 2

            editor.value = 'not-present'
            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.01)
                status = modal.query_one('#transcript-search-status').render()
                if 'No matching messages' in str(status):
                    break
            assert not app._transcript_search_hits
            assert 'No matching messages' in str(status)
            await pilot.press('escape')
            await pilot.pause()
            assert app.view._search_query == ''

    asyncio.run(scenario())


def test_scrolling_to_top_loads_older_history():
    messages = [
        {'role': 'user' if index % 2 == 0 else 'assistant',
         'content': f'history-{index}'}
        for index in range(1000)
    ]

    async def scenario():
        app = HarnessApp(messages)
        async with app.run_test(size=(80, 20)) as pilot:
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.01)
                if app._history_auto_armed and app._history_last_offset > 0:
                    break
            initial_start = len(messages) - HISTORY_INITIAL_MESSAGES
            assert app._history_start == initial_start
            app.view.scroll_home(animate=False)
            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.01)
                if app._history_start < initial_start:
                    break
            expected_start = max(
                0, initial_start - HISTORY_PAGE_MESSAGES)
            assert app._history_start == expected_start
            assert len(app.model) == len(messages) - expected_start

    asyncio.run(scenario())


def test_streaming_render_interval_adapts_to_frame_cost(monkeypatch):
    from types import SimpleNamespace

    from ui import harness_app as module

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)):
            app._busy = True
            app.model.append('assistant', {'text': 'stream', 'streaming': True},
                             block_id='stream-test')
            app._dirty = True
            # 真实 sleep 会受 Windows 调度影响；用确定时钟验证帧耗时策略。
            ticks = iter((100.0, 100.03))
            with monkeypatch.context() as patch:
                patch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
                patch.setattr(app.view, 'refresh_layout', lambda: None)
                app._flush_layout(force=True)
            assert app._stream_interval == pytest.approx(0.06)
            app._busy = False

    asyncio.run(scenario())


def test_rename_reports_busy_session(monkeypatch):
    from file_lock import exclusive_file_lock
    from session_locks import lock_path

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)) as pilot:
            store = harness.SessionEventStore(harness.SESSIONS_DIR,
                                              session_id='rename-busy')
            store.append('session_created')
            app._store = store
            with exclusive_file_lock(
                    lock_path(harness.SESSIONS_DIR, 'rename-busy'), timeout=0):
                app.rename_session('另一个标题')
            await pilot.pause()
            notices = [block.payload.get('text', '')
                       for block in app.model.snapshot()
                       if block.kind == 'notice']
            assert any('执行任务' in text for text in notices)

    asyncio.run(scenario())


def test_wait_for_shutdown_cancels_worker_and_waits_housekeeping(monkeypatch):
    waited = []
    monkeypatch.setattr(harness, 'wait_for_housekeeping',
                        lambda timeout=30.0: waited.append(timeout))

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)):
            started, release = threading.Event(), threading.Event()

            def slow_worker():
                started.set()
                release.wait(5)

            worker = threading.Thread(target=slow_worker, daemon=True)
            worker.start()
            assert started.wait(5)
            app._worker = worker
            release.set()
            app.wait_for_shutdown(timeout=2)
            assert app._exiting
            assert not worker.is_alive()
            assert waited, '退出时应等待回合后整理线程'

    asyncio.run(scenario())


def test_border_status_shows_reasoning_progress():
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 20)):
            app._busy = True
            app._turn_started = time.monotonic()
            app._apply(UIEvent('activity', {'state': 'thinking',
                                            'detail': '模型思考中（120 字）'}))
            app._tick_border_status()
            title = app._composer.border_title
            assert 'Working' in title and '思考中' in title

    asyncio.run(scenario())
