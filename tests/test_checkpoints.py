import json

import pytest

from checkpoints import CheckpointManager, CheckpointError
from session_store import SessionEventStore


@pytest.fixture()
def ws(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    return workspace


def _mgr(ws, **kwargs):
    kwargs.setdefault('max_nodes', 20)
    return CheckpointManager(ws / '.cache' / 'checkpoints', ws, 'sid', **kwargs)


def _msgs(text='hello'):
    return [{'role': 'user', 'content': text}]


def test_turn_node_writes_messages_and_index(tmp_path, ws):
    store = SessionEventStore(ws / '.cache' / 'sessions', session_id='sid')
    store.append_message({'role': 'user', 'content': 'hello'})
    mgr = _mgr(ws)

    cp_id = mgr.begin_turn(_msgs(), store)
    node = ws / '.cache' / 'checkpoints' / 'sid' / cp_id

    assert mgr.messages_snapshot(cp_id) == _msgs()
    assert json.loads((node / 'meta.json').read_text(encoding='utf-8'))['status'] == 'open'
    assert mgr.list()[0]['status'] == 'open'
    # 事件流里记录了节点引用，但不含快照本体
    event = store.read_events()[-1]
    assert event['type'] == 'checkpoint' and event['checkpoint_id'] == cp_id
    assert 'message' not in event


def test_capture_pre_image_is_idempotent_per_node(ws):
    target = ws / 'a.txt'
    target.write_text('v1', encoding='utf-8')
    mgr = _mgr(ws)
    cp_id = mgr.begin_turn(_msgs())

    mgr.capture_pre_image(target)
    target.write_text('v2', encoding='utf-8')
    mgr.capture_pre_image(target)

    assert (ws / '.cache' / 'checkpoints' / 'sid' / cp_id / 'files' / 'a.txt').read_text(encoding='utf-8') == 'v1'
    assert len(mgr.get(cp_id)['files']) == 1


def test_two_files_in_one_round_share_node(ws):
    mgr = _mgr(ws)
    (ws / 'a.txt').write_text('a', encoding='utf-8')
    (ws / 'b.txt').write_text('b', encoding='utf-8')
    cp_id = mgr.begin_turn(_msgs())

    mgr.capture_pre_image(ws / 'a.txt')
    mgr.capture_pre_image(ws / 'b.txt')

    assert sorted(mgr.get(cp_id)['files']) == ['a.txt', 'b.txt']
    assert len(mgr.list()) == 1


def test_restore_deletes_file_created_after_checkpoint(ws):
    mgr = _mgr(ws)
    cp_id = mgr.begin_turn(_msgs())
    mgr.capture_pre_image(ws / 'new.txt')      # 保存时不存在
    (ws / 'new.txt').write_text('created', encoding='utf-8')
    mgr.seal()

    report = mgr.restore(cp_id)

    assert report['ok'] is True
    assert not (ws / 'new.txt').exists()
    assert report['applied'] == ['new.txt']


def test_restore_brings_back_pre_image(ws):
    target = ws / 'a.txt'
    target.write_text('original', encoding='utf-8')
    mgr = _mgr(ws)
    cp_id = mgr.begin_turn(_msgs())
    mgr.capture_pre_image(target)
    target.write_text('broken', encoding='utf-8')
    mgr.seal()

    report = mgr.restore(cp_id)

    assert target.read_text(encoding='utf-8') == 'original'
    assert report['ok'] is True


def test_conflict_blocks_restore_without_force(ws):
    target = ws / 'a.txt'
    target.write_text('original', encoding='utf-8')
    mgr = _mgr(ws)
    cp_id = mgr.begin_turn(_msgs())
    mgr.capture_pre_image(target)
    target.write_text('agent', encoding='utf-8')
    mgr.seal()
    target.write_text('user edited', encoding='utf-8')   # 快照后被外部改动

    report = mgr.restore(cp_id)

    assert report['ok'] is False
    assert report['conflicts'] == ['a.txt']
    assert target.read_text(encoding='utf-8') == 'user edited'   # 一个文件都没改

    forced = mgr.restore(cp_id, force=True)
    assert forced['ok'] is True
    assert target.read_text(encoding='utf-8') == 'original'


def test_plan_restore_has_no_side_effects(ws):
    target = ws / 'a.txt'
    target.write_text('original', encoding='utf-8')
    mgr = _mgr(ws)
    cp_id = mgr.begin_turn(_msgs())
    mgr.capture_pre_image(target)
    target.write_text('broken', encoding='utf-8')
    mgr.seal()

    plan = mgr.plan_restore(cp_id)

    assert target.read_text(encoding='utf-8') == 'broken'
    assert [entry['action'] for entry in plan['actions']] == ['restore']
    assert plan['side_effects']['tasks'] == 0


def test_ignored_and_sensitive_paths_are_not_snapshotted(ws):
    mgr = _mgr(ws)
    cp_id = mgr.begin_turn(_msgs())
    (ws / '.cache').mkdir(exist_ok=True)
    (ws / '.cache' / 'x.txt').write_text('cache', encoding='utf-8')
    (ws / '.env').write_text('SECRET=1', encoding='utf-8')

    mgr.capture_pre_image(ws / '.cache' / 'x.txt')
    mgr.capture_pre_image(ws / '.env')
    mgr.seal()

    files = mgr.get(cp_id)['files']
    assert '.cache/x.txt' not in files
    assert files['.env']['sensitive'] is True
    assert not (ws / '.cache' / 'checkpoints' / 'sid' / cp_id / 'files' / '.env').exists()


def test_rewind_is_idempotent(ws):
    target = ws / 'a.txt'
    target.write_text('original', encoding='utf-8')
    mgr = _mgr(ws)
    cp_id = mgr.begin_turn(_msgs())
    mgr.capture_pre_image(target)
    target.write_text('broken', encoding='utf-8')
    mgr.seal()

    mgr.restore(cp_id)
    first = target.read_text(encoding='utf-8')
    second_report = mgr.restore(cp_id)

    assert first == 'original'
    assert second_report['applied'] == []
    assert [e['action'] for e in second_report['actions']] == ['noop']


def test_rewind_to_older_node_undoes_later_rounds(ws):
    target = ws / 'a.txt'
    target.write_text('v1', encoding='utf-8')
    mgr = _mgr(ws)

    first = mgr.begin_turn(_msgs())
    mgr.capture_pre_image(target)
    target.write_text('v2', encoding='utf-8')
    mgr.seal()

    second = mgr.begin_turn(_msgs())
    mgr.capture_pre_image(target)
    target.write_text('v3', encoding='utf-8')
    mgr.seal()

    report = mgr.restore(first)

    assert report['chain'] == [second, first]
    assert target.read_text(encoding='utf-8') == 'v1'


def test_forward_restore_to_later_node(ws):
    target = ws / 'a.txt'
    target.write_text('v1', encoding='utf-8')
    mgr = _mgr(ws)
    first = mgr.begin_turn(_msgs())
    mgr.capture_pre_image(target)
    target.write_text('v2', encoding='utf-8')
    mgr.seal()
    second = mgr.begin_turn(_msgs())
    mgr.capture_pre_image(target)
    target.write_text('v3', encoding='utf-8')
    mgr.seal()

    mgr.restore(first)
    assert target.read_text(encoding='utf-8') == 'v1'

    forward = mgr.restore(second, force=True)
    assert forward['ok'] is True
    assert target.read_text(encoding='utf-8') == 'v2'


def test_session_only_restore_keeps_files(ws):
    target = ws / 'a.txt'
    target.write_text('original', encoding='utf-8')
    mgr = _mgr(ws)
    cp_id = mgr.begin_turn(_msgs())
    mgr.capture_pre_image(target)
    target.write_text('broken', encoding='utf-8')
    mgr.seal()

    report = mgr.restore(cp_id, scope='session')

    assert target.read_text(encoding='utf-8') == 'broken'
    assert report['applied'] == []
    assert mgr.head() == cp_id


def test_head_survives_restart(ws):
    mgr = _mgr(ws)
    cp_id = mgr.begin_turn(_msgs())
    mgr.seal()

    reopened = _mgr(ws)

    assert reopened.head() == cp_id
    assert reopened.resolve('last') == cp_id


def test_sessions_use_isolated_checkpoint_state(ws):
    """同一工作区的不同会话不能读取或继承彼此的 checkpoint。"""
    base_dir = ws / '.cache' / 'checkpoints'
    first = CheckpointManager(base_dir, ws, 'first')
    cp_id = first.begin_turn(_msgs('first'))
    first.seal()

    second = CheckpointManager(base_dir, ws, 'second')

    assert first.head() == cp_id
    assert second.head() is None
    assert second.list() == []
    assert (base_dir / 'first' / cp_id).is_dir()
    assert not (base_dir / 'second').exists()


def test_resolve_without_nodes_raises(ws):
    mgr = _mgr(ws)

    with pytest.raises(CheckpointError):
        mgr.resolve('last')


def test_last_skips_nodes_without_file_changes(ws):
    """复现「模型改完文件又答一句」的场景：最后一个节点没有文件改动，last 必须跳过它。"""
    target = ws / 'a.txt'
    target.write_text('v1', encoding='utf-8')
    mgr = _mgr(ws)

    changed = mgr.begin_turn(_msgs())          # 这一轮改了文件
    mgr.capture_pre_image(target)
    target.write_text('BROKEN', encoding='utf-8')
    mgr.seal()

    answer = mgr.begin_turn(_msgs())           # 这一轮只输出最终回答
    mgr.seal()

    assert mgr.head() == answer
    assert mgr.resolve('head') == answer
    assert mgr.resolve('last') == changed
    assert mgr.resolve('undo') == changed

    assert mgr.restore(mgr.resolve('last'))['ok'] is True
    assert target.read_text(encoding='utf-8') == 'v1'


def test_last_falls_back_to_newest_when_nothing_changed(ws):
    mgr = _mgr(ws)
    mgr.begin_turn(_msgs())
    mgr.seal()
    newest = mgr.begin_turn(_msgs())
    mgr.seal()

    assert mgr.resolve('last') == newest


def test_gc_keeps_head_and_labeled_and_reports(ws):
    mgr = _mgr(ws, max_nodes=2)
    nodes = [mgr.begin_turn(_msgs(f'm{i}'), label='keep' if i == 0 else '') for i in range(5)]
    for cp_id in nodes:
        mgr.seal()

    removed = mgr.gc()
    remaining = {row['cp_id'] for row in mgr.list(limit=100)}

    assert nodes[-1] in remaining          # head 保留
    assert nodes[0] in remaining           # 带 label 保留
    assert len(removed) == 3
    assert all(not (ws / '.cache' / 'checkpoints' / 'sid' / cp_id).exists() for cp_id in removed)
    assert any(line.get('gc') for line in _index_rows(ws))


def test_side_effect_report_lists_non_rollback_items(ws):
    cache = ws / '.cache'
    (cache / 'memories').mkdir(parents=True)
    (cache / 'memories' / 'a.md').write_text('x', encoding='utf-8')
    (cache / 'tasks').mkdir(parents=True)
    (cache / 'tasks' / 'task_1.json').write_text('{}', encoding='utf-8')
    mgr = _mgr(ws)
    cp_id = mgr.begin_turn(_msgs())
    mgr.seal()

    side = mgr.plan_restore(cp_id)['side_effects']

    assert side['memories'] == 1
    assert side['tasks'] == 1
    assert 'background_tasks' in side


def test_capture_is_noop_without_active_node(ws):
    mgr = _mgr(ws)
    (ws / 'a.txt').write_text('x', encoding='utf-8')

    mgr.capture_pre_image(ws / 'a.txt')

    assert mgr.list() == []


def test_capture_skips_paths_outside_workspace_and_directories(ws, tmp_path):
    mgr = _mgr(ws)
    cp_id = mgr.begin_turn(_msgs())
    (ws / 'sub').mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_text('x', encoding='utf-8')

    mgr.capture_pre_image(ws / 'sub')
    mgr.capture_pre_image(outside)

    assert mgr.get(cp_id)['files'] == {}


def _loop_env(tmp_path, monkeypatch):
    """把 harness 指向临时 workspace，并屏蔽记忆系统（否则会额外调用 LLM）。"""
    harness = pytest.importorskip('harness')
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    monkeypatch.setattr(harness, 'WORKDIR', workspace)
    monkeypatch.setattr(harness, 'CHECKPOINT_DIR', tmp_path / 'checkpoints')
    monkeypatch.setattr(harness, 'SESSIONS_DIR', tmp_path / 'sessions')
    monkeypatch.setattr(harness, 'TRANSCRIPT_DIR', tmp_path / 'transcripts')
    monkeypatch.setattr(harness, 'CHECKPOINT_MANAGER', None)
    monkeypatch.setattr(harness, 'load_memories', lambda messages: '')
    monkeypatch.setattr(harness, 'extract_memories', lambda messages: None)
    monkeypatch.setattr(harness, 'consolidate_memories', lambda: None)
    monkeypatch.setattr(harness, 'generate_session_title', lambda messages: 'test session')
    return harness, workspace


def test_agent_loop_records_nodes_and_rewind_restores_file(tmp_path, monkeypatch):
    """端到端：agent_loop 每轮打节点、write 前抓 pre-image、rewind 同时回滚历史与文件。"""
    from test_context_manage import FakeCompletions, make_text_response, make_tool_response

    harness, workspace = _loop_env(tmp_path, monkeypatch)
    (workspace / 'a.txt').write_text('v1', encoding='utf-8')

    fake = FakeCompletions([
        make_tool_response([('c1', 'write', json.dumps({'path': 'a.txt', 'content': 'v2'}))]),
        make_text_response('done'),
    ])
    monkeypatch.setattr(harness.client.chat.completions, 'create', fake.create)

    session_store = harness.SessionEventStore(harness.SESSIONS_DIR)
    messages = [{'role': 'user', 'content': 'write it'}]
    session_store.append_message(messages[0])

    assert harness.agent_loop(messages, session_store=session_store) == 'done'
    assert (workspace / 'a.txt').read_text(encoding='utf-8') == 'v2'

    manager = harness.CHECKPOINT_MANAGER
    nodes = manager.list()
    assert len(nodes) == 2, nodes                 # 两次循环迭代各一个节点
    assert {row['status'] for row in nodes} == {'sealed'}
    assert nodes[-1]['files'] == ['a.txt']        # 只有第一轮改过文件

    assert harness.rewind_to(messages, session_store, manager, nodes[-1]['cp_id']) is True

    assert (workspace / 'a.txt').read_text(encoding='utf-8') == 'v1'
    assert messages[0] == {'role': 'user', 'content': 'write it'}
    assert messages[-1]['content'].startswith('[Rewound to')
    assert any(event['type'] == 'rewind' for event in session_store.read_events())


def test_agent_loop_checkpoint_tool_labels_valid_turn_node(tmp_path, monkeypatch):
    """模型调用 checkpoint 工具：标记本回合节点，rewind 后历史仍有完整工具配对。"""
    from test_context_manage import FakeCompletions, make_text_response, make_tool_response

    harness, workspace = _loop_env(tmp_path, monkeypatch)
    fake = FakeCompletions([
        make_tool_response([('c1', 'checkpoint', json.dumps({'label': 'before refactor'}))]),
        make_text_response('noted'),
    ])
    monkeypatch.setattr(harness.client.chat.completions, 'create', fake.create)

    session_store = harness.SessionEventStore(harness.SESSIONS_DIR)
    messages = [{'role': 'user', 'content': 'refactor it'}]
    session_store.append_message(messages[0])

    assert harness.agent_loop(messages, session_store=session_store) == 'noted'

    manual = [row for row in harness.CHECKPOINT_MANAGER.list() if row['trigger'] == 'manual']
    assert len(manual) == 1 and manual[0]['label'] == 'before refactor'
    # checkpoint 工具结果以 role=tool 回填，history 里没有孤儿 tool 消息
    history = session_store.rebuild_openai_history()
    assert 'Checkpoint created: ' in json.dumps(history, ensure_ascii=False)
    harness.SessionEventStore.validate_openai_history(history)
    assert harness.rewind_to(messages, session_store, harness.CHECKPOINT_MANAGER,
                             manual[0]['cp_id']) is True
    harness.SessionEventStore.validate_openai_history(messages)


def test_handle_command_checkpoint_and_rewind(tmp_path, monkeypatch, capsys):
    """命令层：/checkpoints、/checkpoint、/rewind --yes 的动作与副作用。"""
    harness, workspace = _loop_env(tmp_path, monkeypatch)
    (workspace / 'a.txt').write_text('v1', encoding='utf-8')
    session_store = harness.SessionEventStore(harness.SESSIONS_DIR)
    messages = [{'role': 'user', 'content': 'hi'}]
    session_store.append_message(messages[0])
    manager = harness.get_checkpoint_manager(session_store)

    harness.handle_command('/checkpoints', messages, session_store)
    assert '(no checkpoints)' in capsys.readouterr().out

    harness.handle_command('/checkpoint before refactor', messages, session_store)
    manual = [row for row in manager.list() if row['trigger'] == 'manual']
    assert len(manual) == 1 and manual[0]['label'] == 'before refactor'

    manager.capture_pre_image(workspace / 'a.txt')
    (workspace / 'a.txt').write_text('broken', encoding='utf-8')
    manager.seal()

    harness.handle_command('/rewind last --yes', messages, session_store)

    assert (workspace / 'a.txt').read_text(encoding='utf-8') == 'v1'
    assert messages[-1]['content'].startswith('[Rewound to ' + manual[0]['cp_id'])
    assert any(event['type'] == 'rewind' for event in session_store.read_events())
    assert manager.head() == manual[0]['cp_id']


def _index_rows(ws):
    path = ws / '.cache' / 'checkpoints' / 'sid' / 'index.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
