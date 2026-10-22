import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import headless
import tools

PNG_1X1 = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)


class FakeStore:
    def __init__(self):
        self.events = []
        self.last_seq = 0

    def read_events(self):
        return list(self.events)


class FakeRuntime:
    def __init__(self, workspace, *, content='final answer', error=None):
        self.workspace = str(workspace)
        self.content = content
        self.error = error
        self.store = FakeStore()
        self.sessions = set()
        self.calls = []
        self.set_workspace_calls = []
        self.received = None

    def create_session(self, session_id=None, *, workspace=None):
        identifier = session_id or 'generated'
        self.sessions.add(identifier)
        self.calls.append(('create', identifier, workspace))
        return identifier

    def open_session(self, session_id):
        if session_id not in self.sessions:
            raise FileNotFoundError(session_id)
        return self.store

    def get_workspace(self, session_id):
        return self.workspace

    def set_workspace(self, session_id, path):
        self.set_workspace_calls.append((session_id, path))
        self.workspace = path
        return path

    def send(self, session_id, content, on_delta=None, attachments=None, on_event=None, housekeeping=True):
        self.received = {
            'session_id': session_id,
            'content': content,
            'attachments': attachments,
            'housekeeping': housekeeping,
            'on_event': on_event,
        }
        print('tool log should go to stderr')
        if on_event:
            on_event('tool.started', {'name': 'read', 'call_id': 'call-1', 'arguments': {'path': 'README.md'}})
            on_event(
                'tool.completed', {'name': 'read', 'call_id': 'call-1', 'status': 'ok', 'output': 'hello', 'images': []}
            )
        if on_delta:
            on_delta('final ')
        self.store.events = [
            {
                'seq': 1,
                'type': 'usage',
                'prompt': 10,
                'completion': 4,
                'cached': 6,
                'total': 14,
                'cached_seen': True,
                'model': 'test-model',
            },
            {'seq': 2, 'type': 'files_changed', 'files': [{'code': 'M', 'path': 'README.md', 'old': None}]},
            {'seq': 3, 'type': 'turn_completed'},
        ]
        self.store.last_seq = 3
        if self.error is not None:
            raise self.error
        return SimpleNamespace(session_id=session_id, content=self.content)


def _args(prompt='inspect', **overrides):
    values = {'prompt': prompt, 'session': None, 'workspace': None, 'image': [], 'json': False}
    values.update(overrides)
    return SimpleNamespace(**values)


def _run(args, runtime, monkeypatch, stdin=''):
    monkeypatch.setattr(headless.sys, 'stdin', io.StringIO(stdin))
    out, err = io.StringIO(), io.StringIO()
    code = headless.run(args, runtime=runtime, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_plain_mode_stdout_contains_only_final_answer(tmp_path, monkeypatch):
    runtime = FakeRuntime(tmp_path)

    code, stdout, stderr = _run(_args(), runtime, monkeypatch)

    assert code == headless.EXIT_OK
    assert stdout == 'final answer\n'
    assert 'tool log should go to stderr' in stderr
    assert runtime.received['housekeeping'] is False


def test_json_mode_emits_jsonl_without_log_noise(tmp_path, monkeypatch):
    runtime = FakeRuntime(tmp_path)

    code, stdout, _stderr = _run(_args(json=True), runtime, monkeypatch)

    assert code == headless.EXIT_OK
    events = [json.loads(line) for line in stdout.splitlines()]
    types = [event['type'] for event in events]
    assert types[0] == 'session'
    assert types[-2:] == ['message', 'turn.completed']
    assert 'tool.started' in types and 'tool.completed' in types
    assert all(isinstance(event['type'], str) for event in events)

    started = next(event for event in events if event['type'] == 'tool.started')
    assert started['arguments'] == {'path': 'README.md'}
    completed = next(event for event in events if event['type'] == 'tool.completed')
    assert completed['status'] == 'ok'
    session = events[0]
    assert session['resumed'] is False
    assert session['workspace'] == str(tmp_path)

    turn = events[-1]
    assert turn['content'] == 'final answer'
    assert turn['usage'] == {
        'prompt': 10,
        'completion': 4,
        'cached': 6,
        'total': 14,
        'calls': 1,
        'cached_seen': True,
        'model': 'test-model',
    }
    assert turn['files_changed'] == [{'code': 'M', 'path': 'README.md', 'old': None}]


def test_new_session_uses_workspace_and_attaches_image(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    image = workspace / 'pixel.png'
    image.write_bytes(PNG_1X1)
    runtime = FakeRuntime(workspace)
    args = _args(workspace=str(workspace), image=[str(image)])

    code, stdout, _stderr = _run(args, runtime, monkeypatch)

    assert code == headless.EXIT_OK
    assert stdout == 'final answer\n'
    assert runtime.calls[0][0] == 'create'
    assert runtime.calls[0][2] == str(workspace.resolve())
    attachments = runtime.received['attachments']
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment['type'] == 'harness_image'
    assert attachment['name'] == 'pixel.png'
    assert attachment['mime'] == 'image/png'
    assert Path(attachment['path']).is_file()
    assert Path(attachment['path']).is_relative_to(tools._h().TOOL_RESULTS_DIR / 'images')


def test_image_outside_workspace_is_rejected_before_send(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    outside = tmp_path / 'outside.png'
    outside.write_bytes(PNG_1X1)
    runtime = FakeRuntime(workspace)
    args = _args(workspace=str(workspace), image=[str(outside)])

    code, stdout, stderr = _run(args, runtime, monkeypatch)

    assert code == headless.EXIT_USAGE
    assert stdout == ''
    assert runtime.received is None
    assert 'outside the workspace' in stderr


def test_resume_existing_session_switches_workspace(tmp_path, monkeypatch):
    old_workspace = tmp_path / 'old'
    new_workspace = tmp_path / 'new'
    old_workspace.mkdir()
    new_workspace.mkdir()
    runtime = FakeRuntime(old_workspace)
    runtime.sessions.add('existing')
    args = _args(session='existing', workspace=str(new_workspace), json=True)

    code, stdout, _stderr = _run(args, runtime, monkeypatch)

    assert code == headless.EXIT_OK
    events = [json.loads(line) for line in stdout.splitlines()]
    assert events[0]['session_id'] == 'existing'
    assert events[0]['resumed'] is True
    assert events[0]['workspace'] == str(new_workspace.resolve())
    assert runtime.set_workspace_calls == [('existing', str(new_workspace.resolve()))]
    assert all(call[0] != 'create' for call in runtime.calls)
    assert runtime.received['session_id'] == 'existing'


def test_missing_prompt_reads_stdin_or_fails(tmp_path, monkeypatch):
    runtime = FakeRuntime(tmp_path)

    code, stdout, _stderr = _run(_args(prompt=None), runtime, monkeypatch, stdin='piped prompt\n')
    assert code == headless.EXIT_OK
    assert runtime.received['content'] == 'piped prompt'
    assert stdout == 'final answer\n'

    code, stdout, stderr = _run(_args(prompt=None), runtime, monkeypatch, stdin='')
    assert code == headless.EXIT_USAGE
    assert stdout == ''
    assert 'prompt is required' in stderr


def test_runtime_error_emits_only_error_event(tmp_path, monkeypatch):
    runtime = FakeRuntime(tmp_path, error=RuntimeError('provider exploded'))

    code, stdout, stderr = _run(_args(json=True), runtime, monkeypatch)

    assert code == headless.EXIT_ERROR
    assert 'provider exploded' in stderr
    events = [json.loads(line) for line in stdout.splitlines()]
    assert events[-1] == {'type': 'error', 'code': headless.EXIT_ERROR, 'message': 'provider exploded'}
    assert all(event['type'] != 'turn.completed' for event in events)


def test_exec_tool_call_reports_started_and_completed():
    import harness

    events = []
    tool_call = SimpleNamespace(
        id='call-1',
        function=SimpleNamespace(name='echo', arguments='{"text": "hi"}'),
    )

    result = harness.exec_tool_call(
        tool_call,
        registry={'echo': lambda text: text},
        hooks=False,
        on_event=lambda kind, payload: events.append((kind, payload)),
    )

    assert result[0] == 'echo'
    assert [kind for kind, _payload in events] == ['tool.started', 'tool.completed']
    assert events[0][1]['arguments'] == {'text': 'hi'}
    assert events[1][1]['status'] == 'ok'
    assert events[1][1]['output'] == 'hi'


def test_exec_tool_call_reports_invalid_arguments_as_error():
    import harness

    events = []
    tool_call = SimpleNamespace(
        id='call-2',
        function=SimpleNamespace(name='echo', arguments='{not json'),
    )

    result = harness.exec_tool_call(
        tool_call,
        registry={'echo': lambda **kwargs: 'unused'},
        hooks=False,
        on_event=lambda kind, payload: events.append((kind, payload)),
    )

    assert result[2]['content'].startswith('Error: invalid tool arguments')
    assert [kind for kind, _payload in events] == ['tool.started', 'tool.completed']
    assert events[1][1]['status'] == 'error'
    assert events[1][1]['output'].startswith('Error: invalid tool arguments')


def test_owned_runtime_initializes_and_shuts_down_mcp(tmp_path, monkeypatch):
    import harness

    runtime = FakeRuntime(tmp_path)
    calls = []
    monkeypatch.setattr(harness, 'validate_runtime_config', lambda: calls.append('validate'))
    monkeypatch.setattr(harness, 'initialize_mcp_tools', lambda: calls.append('init'))

    def create_runtime(**kwargs):
        calls.append(('create_runtime', kwargs))
        return runtime

    monkeypatch.setattr(harness, 'create_runtime', create_runtime)
    monkeypatch.setattr(harness, 'shutdown_mcp', lambda: calls.append('shutdown'))

    code, stdout, _stderr = _run(_args(), None, monkeypatch)

    assert code == headless.EXIT_OK
    assert stdout == 'final answer\n'
    assert calls == ['validate', 'init', ('create_runtime', {'initialize_tools': False}), 'shutdown']


def test_config_error_is_a_usage_error(tmp_path, monkeypatch):
    import harness

    def missing_config():
        raise RuntimeError('Missing required configuration: LLM_API_KEY')

    monkeypatch.setattr(harness, 'validate_runtime_config', missing_config)
    monkeypatch.setattr(harness, 'initialize_mcp_tools', lambda: (_ for _ in ()).throw(AssertionError('must not init')))

    code, stdout, stderr = _run(_args(json=True), None, monkeypatch)

    assert code == headless.EXIT_USAGE
    assert 'Missing required configuration' in stderr
    events = [json.loads(line) for line in stdout.splitlines()]
    assert events == [
        {
            'type': 'error',
            'code': headless.EXIT_USAGE,
            'message': '[config] Missing required configuration: LLM_API_KEY',
        }
    ]


def test_real_agent_loop_end_to_end_skips_housekeeping(tmp_path, monkeypatch):
    import harness
    import test_context_manage as tcm
    from agent_runtime import AgentRuntime
    from session_store import SessionEventStore

    monkeypatch.setattr(harness, 'load_memories', lambda messages: '')
    monkeypatch.setattr(harness, 'extract_memories', lambda pre: None)
    monkeypatch.setattr(harness, 'consolidate_memories', lambda: None)
    monkeypatch.setattr(harness, 'generate_session_title', lambda messages: 't')
    monkeypatch.setattr(harness, 'write_transcript', lambda messages: None)

    def unexpected(*_args, **_kwargs):
        raise AssertionError('housekeeping must be disabled in headless mode')

    monkeypatch.setattr(harness, '_post_turn_housekeeping', unexpected)
    monkeypatch.setitem(harness.tool_registry, 'ls', lambda path='.': 'file.txt')
    fake = tcm.FakeCompletions(
        [
            tcm.make_tool_response([('c1', 'ls', '{}')]),
            tcm.make_text_response('listed'),
        ]
    )
    monkeypatch.setattr(harness.client.chat.completions, 'create', fake.create)

    runtime = AgentRuntime(
        run_turn=harness.agent_loop,
        session_store_cls=SessionEventStore,
        sessions_dir=tmp_path / 'sessions',
        checkpoints_dir=tmp_path / 'checkpoints',
        workspace=tmp_path,
    )
    code, stdout, stderr = _run(_args(prompt='list files', json=True), runtime, monkeypatch)

    assert code == headless.EXIT_OK
    events = [json.loads(line) for line in stdout.splitlines()]
    types = [event['type'] for event in events]
    assert 'tool.started' in types and 'tool.completed' in types
    completed = next(event for event in events if event['type'] == 'tool.completed')
    assert completed['name'] == 'ls'
    assert completed['output'] == 'file.txt'
    assert events[-1]['type'] == 'turn.completed'
    assert events[-1]['content'] == 'listed'
    assert 'housekeeping must be disabled' not in stderr
