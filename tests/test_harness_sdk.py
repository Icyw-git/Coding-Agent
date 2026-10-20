from dataclasses import dataclass

from harness_sdk import HarnessClient


@dataclass
class FakeResult:
    session_id: str
    content: str


class FakeRuntime:
    def __init__(self):
        self.history_by_id = {}

    def create_session(self, session_id=None):
        identifier = session_id or 'generated'
        self.history_by_id[identifier] = []
        return identifier

    def history(self, session_id):
        return list(self.history_by_id[session_id])

    def send(self, session_id, content):
        self.history_by_id[session_id].extend([
            {'role': 'user', 'content': content},
            {'role': 'assistant', 'content': 'done'},
        ])
        return FakeResult(session_id, 'done')


def test_sdk_creates_session_reads_history_and_sends_message():
    client = HarnessClient(FakeRuntime())

    session = client.sessions.create('sdk-session')
    response = client.messages.create(session.id, 'inspect project')
    updated = client.sessions.get(session.id)

    assert session == type(session)(id='sdk-session', history=[])
    assert response.session_id == 'sdk-session'
    assert response.content == 'done'
    assert updated.history == [
        {'role': 'user', 'content': 'inspect project'},
        {'role': 'assistant', 'content': 'done'},
    ]


def test_sdk_can_create_generated_session():
    session = HarnessClient(FakeRuntime()).sessions.create()

    assert session.id == 'generated'
    assert session.history == []


def test_sdk_exposes_the_workspace_registry():
    class Runtime:
        def list_workspaces(self, current=None):
            return {'current': current, 'workspaces': []}

        def add_workspace(self, path, name=None):
            return {'id': 'ws1', 'path': path, 'name': name or path}

        def remove_workspace(self, workspace_id):
            return {'id': workspace_id}

    client = HarnessClient(Runtime())
    assert client.workspaces.list('here')['current'] == 'here'
    assert client.workspaces.add('p', 'Name')['name'] == 'Name'
    assert client.workspaces.remove('ws1')['id'] == 'ws1'
