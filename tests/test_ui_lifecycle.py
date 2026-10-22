"""Regression coverage for session/task boundaries during TUI migration."""
import asyncio
import threading

import pytest

pytest.importorskip('textual')

import harness  # noqa: E402
from ui import commands  # noqa: E402
from ui.harness_app import HarnessApp  # noqa: E402
from ui.modals.cleanup import CleanupPickerModal  # noqa: E402
from ui.modals.confirm import ConfirmModal  # noqa: E402


def test_archive_scan_reports_through_callback(tmp_path, capsys):
    from session_cleanup import SessionArchive

    archive = SessionArchive(tmp_path / 'sessions', tmp_path / 'checkpoints')
    broken = archive.root / 'broken' / 'session_broken.jsonl'
    broken.parent.mkdir(parents=True)
    broken.write_text('invalid json\n{}\n', encoding='utf-8')
    warnings = []
    assert archive.list(emit=warnings.append) == []
    assert warnings and 'broken' in warnings[0]
    assert capsys.readouterr().out == ''


def test_cli_commands_and_aliases_exist_in_tui():
    from cli_commands import COMMANDS
    for command in COMMANDS:
        spec = commands.find(command.name.lstrip('/'))
        assert spec is not None and spec.ported, command.name
        for alias in command.aliases:
            assert commands.find(alias.lstrip('/')) is spec, alias


def test_busy_task_blocks_session_and_workspace_switch(tmp_path, monkeypatch):
    store = harness.SessionEventStore(tmp_path)

    def unexpected(*args, **kwargs):
        pytest.fail('must not switch session or workspace during a task')

    monkeypatch.setattr(harness, 'start_new_session', unexpected)
    monkeypatch.setattr(harness, 'set_workspace', unexpected)
    monkeypatch.setattr(harness.SessionEventStore, 'open', unexpected)

    async def scenario():
        app = HarnessApp([], session_store=store, session_id=store.session_id)
        async with app.run_test() as pilot:
            app._busy = True
            for command in ('/new', '/resume last', '/fork', '/clone', '/cd elsewhere'):
                app.submit_text(command)
                assert '尚未结束' in app.model.snapshot()[-1].payload['text']
            await app._resume_session_row({'id': 'another'})
            assert app._store is store
            assert '尚未结束' in app.model.snapshot()[-1].payload['text']
            app._busy = False
            await pilot.pause()

    asyncio.run(scenario())


@pytest.mark.parametrize('fail_cleanup', [False, True])
def test_cleanup_serializes_tasks_and_drains_followup(tmp_path, monkeypatch, fail_cleanup):
    store = harness.SessionEventStore(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    started = []
    monkeypatch.setattr(harness, 'housekeeping_busy', lambda: False)

    def cleanup(*args):
        entered.set()
        assert release.wait(5)
        if fail_cleanup:
            raise OSError('simulated cleanup failure')
        return 1, []

    monkeypatch.setattr(harness, 'apply_session_cleanup', cleanup)

    async def scenario():
        app = HarnessApp([], session_store=store, session_id=store.session_id)
        monkeypatch.setattr(app, '_start_worker', lambda: started.extend(app._pending.drain()))
        async with app.run_test() as pilot:
            try:
                app._execute_cleanup(True, object(), [{'id': 'other'}], 'archive')
                for _ in range(60):
                    await pilot.pause()
                    if entered.is_set():
                        break
                assert entered.is_set() and app._busy
                app.submit_text('run after cleanup')
                assert app._steering.snapshot() == []
                assert app._pending.snapshot() == ['run after cleanup']
                assert started == []
                app.submit_text('/new')
                assert app._store is store
                release.set()
                for _ in range(60):
                    await pilot.pause()
                    if not app._busy:
                        break
                assert not app._busy
                assert started == ['run after cleanup']
            finally:
                release.set()

    asyncio.run(scenario())


def test_local_operation_messages_are_followups(monkeypatch):
    async def scenario():
        app = HarnessApp([])
        started = []
        monkeypatch.setattr(app, '_start_worker', lambda: started.extend(app._pending.drain()))
        async with app.run_test() as pilot:
            app._busy = True
            app._operation = 'compact'
            block = app.model.append('notice', {'text': 'compacting'})
            app.submit_text('after compact')
            assert not app._steering.snapshot()
            app._compact_done(block.id, [{'role': 'user', 'content': 'summary'}])
            assert started == ['after compact']
            await pilot.pause()

    asyncio.run(scenario())


def test_stale_scan_results_do_not_open_modal(tmp_path):
    store = harness.SessionEventStore(tmp_path)

    async def scenario():
        app = HarnessApp([])
        async with app.run_test() as pilot:
            block = app.model.append('notice', {'text': 'scanning'})
            app._adopt_session(store, [])
            app._cleanup_scan_done(block.id, object(), [{'id': 'other'}], 'archive', 'all')
            app._find_done(block.id, 'needle', [{'id': 'other', 'title': 'Other'}])
            await pilot.pause()
            assert len(app.screen_stack) == 1

    asyncio.run(scenario())


def test_cleanup_selection_survives_filter_and_ctrl_a():
    rows = [{'id': str(i), 'title': f'task {i}', 'updated': 0} for i in range(3)]

    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(90, 28)) as pilot:
            modal = CleanupPickerModal(rows)
            app.push_screen(modal)
            await pilot.pause()
            await pilot.press('ctrl+a')
            await pilot.pause()
            assert modal.selected == {'0', '1', '2'}
            search = modal.query_one('#cleanup-search')
            search.value = 'task 1'
            await pilot.pause()
            assert modal.selected == {'0', '1', '2'}
            await pilot.press('ctrl+a')
            await pilot.pause()
            assert modal.selected == {'0', '2'}
            search.value = ''
            await pilot.pause()
            assert set(modal.query_one('#cleanup-list').selected) == {'0', '2'}

    asyncio.run(scenario())


def test_long_confirmation_body_scrolls():
    async def scenario():
        app = HarnessApp([])
        async with app.run_test(size=(80, 24)) as pilot:
            modal = ConfirmModal('Preview', '\n'.join(f'file {i}' for i in range(100)))
            app.push_screen(modal)
            await pilot.pause()
            body = modal.query_one('.confirmation-body')
            assert body.virtual_size.height > body.size.height
            assert modal.query_one('#confirm-box').region.bottom <= app.size.height

    asyncio.run(scenario())


def test_rewind_restores_real_files_and_persists_history(tmp_path, monkeypatch):
    from checkpoints import CheckpointManager

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    store = harness.SessionEventStore(tmp_path / 'sessions')
    original = {'role': 'user', 'content': 'before change'}
    store.append_message(original)
    manager = CheckpointManager(tmp_path / 'checkpoints', workspace, store.session_id)
    target = workspace / 'example.txt'
    target.write_text('before', encoding='utf-8')
    cp_id = manager.begin_turn([original], store)
    manager.capture_pre_image(target)
    target.write_text('agent change', encoding='utf-8')
    manager.seal()
    target.write_text('external change', encoding='utf-8')
    store.append_message({'role': 'user', 'content': 'later message'})
    monkeypatch.setattr(harness, 'get_checkpoint_manager', lambda store: manager)

    async def scenario():
        app = HarnessApp(store.rebuild_openai_history(),
                         session_id=store.session_id, session_store=store)
        async with app.run_test() as pilot:
            app.submit_text(f'/rewind {cp_id} --yes')
            await pilot.pause()
            assert target.read_text(encoding='utf-8') == 'external change'
            assert not any(event['type'] == 'rewind' for event in store.read_events())
            app.submit_text(f'/rewind {cp_id} --force --yes')
            await pilot.pause()
            assert target.read_text(encoding='utf-8') == 'before'
            assert app._messages == store.rebuild_openai_history()
            assert all(message['content'] != 'later message' for message in app._messages)
            assert any(event['type'] == 'rewind' for event in store.read_events())

    asyncio.run(scenario())


def test_directory_completion_does_not_execute_cd(monkeypatch):
    async def scenario():
        app = HarnessApp([])
        async with app.run_test() as pilot:
            monkeypatch.setattr(app, 'change_workspace', lambda value: pytest.fail('completion executed cd'))
            app._composer.text = '/cd proj'
            await pilot.pause()
            app._suggestions = [('project/', 'project/')]
            app.submit_composer()
            assert app._composer.text == '/cd project/'

    asyncio.run(scenario())
